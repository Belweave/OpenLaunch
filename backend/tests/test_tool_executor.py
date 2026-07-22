import asyncio
import json
import time
import unittest

from openlaunch.utils.tool_executor import (
    ToolCall,
    ToolExecutor,
    get_tool_metrics_snapshot,
    normalize_tool_annotations,
)
from openlaunch.utils.tools import register_tool


def tool(name, function, *, annotations=None, tool_type="builtin"):
    return {
        "spec": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {"delay": {"type": "number"}},
            },
        },
        "callable": function,
        "type": tool_type,
        "annotations": annotations
        or {
            "read_only": True,
            "destructive": False,
            "idempotent": True,
            "approval_required": False,
        },
    }


class ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_finish_near_longest_and_preserve_order_and_ids(self):
        async def slow(delay=0):
            await asyncio.sleep(delay)
            return {"delay": delay}

        executor = ToolExecutor({"slow": tool("slow", slow)}, request_concurrency=4)
        started = time.monotonic()
        results = await executor.execute_batch(
            [
                ToolCall("call_slow", "slow", {"delay": 0.20}),
                ToolCall("call_fast", "slow", {"delay": 0.05}),
            ]
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.32)
        self.assertEqual([result.call_id for result in results], ["call_slow", "call_fast"])
        self.assertEqual([result.content["delay"] for result in results], [0.20, 0.05])

    async def test_internal_exception_is_sanitized(self):
        async def failing(**_kwargs):
            raise RuntimeError("postgresql://admin:super-secret@internal/db")

        with self.assertLogs("openlaunch.utils.tool_executor", level="ERROR") as captured:
            result = (
                await ToolExecutor({"fail": tool("fail", failing)}).execute_batch([ToolCall("id-1", "fail", {})])
            )[0]
        encoded = json.dumps(result.content)
        self.assertEqual(result.call_id, "id-1")
        self.assertEqual(result.status, "internal_error")
        self.assertNotIn("super-secret", encoded)
        self.assertNotIn("postgresql", encoded)
        self.assertNotIn("super-secret", "\n".join(captured.output))

    async def test_unknown_malformed_timeout_and_budget_results_preserve_ids(self):
        async def slow(**_kwargs):
            await asyncio.sleep(0.1)
            return "late"

        async def large(**_kwargs):
            return "x" * 5000

        tools = {
            "slow": tool("slow", slow, annotations={"timeout_seconds": 0.01}),
            "large": tool("large", large, annotations={"output_budget_bytes": 256}),
        }
        results = await ToolExecutor(tools).execute_batch(
            [
                ToolCall("unknown-id", "missing", "{}"),
                ToolCall("json-id", "slow", "{bad"),
                ToolCall("timeout-id", "slow", "{}"),
                ToolCall("budget-id", "large", "{}"),
            ]
        )
        self.assertEqual(
            [result.call_id for result in results],
            ["unknown-id", "json-id", "timeout-id", "budget-id"],
        )
        self.assertEqual(
            [result.status for result in results],
            ["unknown_tool", "malformed_arguments", "timeout", "budget_exceeded"],
        )

    async def test_cancellation_propagates_to_running_tool(self):
        cancelled = asyncio.Event()

        async def blocked(**_kwargs):
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()

        task = asyncio.create_task(
            ToolExecutor({"blocked": tool("blocked", blocked)}).execute_batch([ToolCall("cancel-id", "blocked", {})])
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_latency_and_status_metrics_are_recorded(self):
        async def ok(**_kwargs):
            return "ok"

        await ToolExecutor({"metric_tool": tool("metric_tool", ok)}).execute_batch(
            [ToolCall("metric-id", "metric_tool", {})]
        )
        metrics = get_tool_metrics_snapshot()["metric_tool"]
        self.assertGreaterEqual(metrics["calls"], 1)
        self.assertGreaterEqual(metrics["status.success"], 1)
        self.assertIn("latency_ms.max", metrics)

    async def test_callable_resolver_receives_call_context_for_auditing(self):
        observed = []

        async def ok(**_kwargs):
            return "ok"

        async def resolver(tool_definition, call):
            observed.append((tool_definition["spec"]["name"], call.call_id))
            return tool_definition["callable"]

        await ToolExecutor({"audited": tool("audited", ok)}, callable_resolver=resolver).execute_batch(
            [ToolCall("audit-call-id", "audited", {})]
        )
        self.assertEqual(observed, [("audited", "audit-call-id")])

    async def test_retries_only_explicit_safe_idempotent_tools(self):
        attempts = {"safe": 0, "destructive": 0}

        async def safe(**_kwargs):
            attempts["safe"] += 1
            if attempts["safe"] == 1:
                raise RuntimeError("transient")
            return "ok"

        async def destructive(**_kwargs):
            attempts["destructive"] += 1
            raise RuntimeError("do not retry")

        tools = {
            "safe": tool(
                "safe",
                safe,
                annotations={
                    "read_only": True,
                    "destructive": False,
                    "idempotent": True,
                    "retry_eligible": True,
                    "max_retries": 1,
                },
            ),
            "destructive": tool(
                "destructive",
                destructive,
                annotations={
                    "destructive": True,
                    "idempotent": False,
                    "retry_eligible": True,
                    "max_retries": 2,
                },
            ),
        }
        results = await ToolExecutor(tools).execute_batch(
            [ToolCall("safe-id", "safe", {}), ToolCall("destroy-id", "destructive", {})]
        )
        self.assertEqual(results[0].status, "success")
        self.assertEqual(results[1].status, "internal_error")
        self.assertEqual(attempts, {"safe": 2, "destructive": 1})

    def test_missing_annotations_are_conservative(self):
        annotations = normalize_tool_annotations()
        self.assertTrue(annotations["destructive"])
        self.assertTrue(annotations["approval_required"])
        self.assertFalse(annotations["idempotent"])
        self.assertFalse(annotations["retry_eligible"])


class ToolRegistryTests(unittest.TestCase):
    def test_collisions_update_key_schema_and_routing_without_mutating_source(self):
        for tool_type, namespace in (
            ("local", "local-id"),
            ("external", "openapi-id"),
            ("mcp", "mcp-id"),
            ("builtin", "builtin"),
        ):
            with self.subTest(tool_type=tool_type):
                registry = {}
                original_spec = {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                }

                async def lookup():
                    return "ok"

                register_tool(
                    registry,
                    "lookup",
                    {"spec": original_spec, "callable": lookup, "type": tool_type},
                )
                final = register_tool(
                    registry,
                    "lookup",
                    {"spec": original_spec, "callable": lookup, "type": tool_type},
                    namespace=namespace,
                )
                self.assertNotEqual(final, "lookup")
                self.assertEqual(registry[final]["spec"]["name"], final)
                self.assertEqual(registry[final]["routing_name"], final)
                self.assertEqual(registry[final]["name"], final)
                self.assertEqual(original_spec["name"], "lookup")
                self.assertIn("annotations", registry[final])
