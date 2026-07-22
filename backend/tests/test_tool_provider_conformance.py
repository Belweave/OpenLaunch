import asyncio
import unittest

from openlaunch.utils.tool_executor import ToolExecutor
from openlaunch.utils.tool_protocol import (
    format_provider_results,
    normalize_provider_calls,
)


class ProviderToolConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_parallel_mixed_scenario_across_provider_protocols(self):
        async def lookup(delay=0, fail=False):
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError("internal-secret-detail")
            return {"delay": delay}

        tool = {
            "spec": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"delay": {}, "fail": {}},
                },
            },
            "callable": lookup,
            "annotations": {
                "read_only": True,
                "destructive": False,
                "idempotent": True,
            },
        }
        payloads = {
            "openai_chat": {
                "tool_calls": [
                    {
                        "id": "slow",
                        "function": {"name": "lookup", "arguments": '{"delay":0.03}'},
                    },
                    {
                        "id": "fail",
                        "function": {"name": "lookup", "arguments": '{"fail":true}'},
                    },
                ]
            },
            "openai_responses": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "slow",
                        "name": "lookup",
                        "arguments": '{"delay":0.03}',
                    },
                    {
                        "type": "function_call",
                        "call_id": "fail",
                        "name": "lookup",
                        "arguments": '{"fail":true}',
                    },
                ]
            },
            "anthropic": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "slow",
                        "name": "lookup",
                        "input": {"delay": 0.03},
                    },
                    {
                        "type": "tool_use",
                        "id": "fail",
                        "name": "lookup",
                        "input": {"fail": True},
                    },
                ]
            },
        }
        for provider, payload in payloads.items():
            with self.subTest(provider=provider):
                results = await ToolExecutor({"lookup": tool}).execute_batch(
                    normalize_provider_calls(provider, payload)
                )
                self.assertEqual([result.call_id for result in results], ["slow", "fail"])
                self.assertEqual(results[1].status, "internal_error")
                self.assertNotIn("internal-secret-detail", str(results[1].content))
                formatted = format_provider_results(provider, results)
                if provider == "anthropic":
                    self.assertEqual(len(formatted), 1)
                    self.assertEqual(
                        [item["tool_use_id"] for item in formatted[0]["content"]],
                        ["slow", "fail"],
                    )
                else:
                    self.assertEqual(len(formatted), 2)

    async def test_malformed_unknown_timeout_budget_and_multiple_iterations(self):
        async def echo(value="ok"):
            return value

        tool = {
            "spec": {
                "name": "echo",
                "parameters": {"type": "object", "properties": {"value": {}}},
            },
            "callable": echo,
            "annotations": {
                "read_only": True,
                "destructive": False,
                "idempotent": True,
                "output_budget_bytes": 256,
            },
        }
        executor = ToolExecutor({"echo": tool}, request_budget_bytes=1024)
        first = await executor.execute_batch(
            normalize_provider_calls(
                "openai_chat",
                {
                    "tool_calls": [
                        {
                            "id": "bad-json",
                            "function": {"name": "echo", "arguments": "{"},
                        },
                        {
                            "id": "unknown",
                            "function": {"name": "missing", "arguments": "{}"},
                        },
                    ]
                },
            )
        )
        second = await executor.execute_batch(
            normalize_provider_calls(
                "openai_responses",
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "large",
                            "name": "echo",
                            "arguments": '{"value":"' + ("x" * 1000) + '"}',
                        },
                    ]
                },
            )
        )
        self.assertEqual([item.status for item in first], ["malformed_arguments", "unknown_tool"])
        self.assertEqual(second[0].status, "budget_exceeded")

    def test_streaming_fragments_normalize_to_same_call(self):
        # Both Chat and Responses streaming collectors concatenate argument deltas
        # before entering the shared executor.
        fragments = ['{"value"', ":", '"ok"}']
        chat = {
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "echo", "arguments": "".join(fragments)},
                }
            ]
        }
        responses = {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "echo",
                    "arguments": "".join(fragments),
                }
            ]
        }
        self.assertEqual(
            normalize_provider_calls("openai_chat", chat),
            normalize_provider_calls("openai_responses", responses),
        )
