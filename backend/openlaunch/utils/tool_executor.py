"""Provider-neutral, bounded and fail-closed execution for model-requested tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openlaunch.env import (
    TOOL_EXECUTOR_DEFAULT_TIMEOUT_SECONDS,
    TOOL_EXECUTOR_GLOBAL_CONCURRENCY,
    TOOL_EXECUTOR_MAX_CALLS_PER_REQUEST,
    TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_REQUEST,
    TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_TURN,
    TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_REQUEST,
    TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_TURN,
    TOOL_EXECUTOR_REQUEST_CONCURRENCY,
)

log = logging.getLogger(__name__)

SAFE_MESSAGES = {
    "cancelled": "Tool execution was cancelled.",
    "timeout": "Tool execution timed out.",
    "unavailable": "Tool is temporarily unavailable.",
    "malformed_arguments": "Tool call arguments were malformed or incomplete.",
    "unknown_tool": "The requested tool is unavailable.",
    "budget_exceeded": "Tool result budget was exceeded.",
    "quota_exceeded": "Tool execution quota was exceeded.",
    "internal_error": "Tool execution failed.",
}


def tool_annotations(**annotations):
    """Attach provider-neutral enforcement metadata to a tool callable."""

    def decorate(function):
        function.__openlaunch_tool_annotations__ = dict(annotations)
        return function

    return decorate


def structured_error(code: str, tool_name: str = "") -> dict[str, Any]:
    return {
        "status": "error",
        "code": code,
        "message": SAFE_MESSAGES.get(code, SAFE_MESSAGES["internal_error"]),
        **({"tool": tool_name} if tool_name else {}),
    }


def normalize_tool_annotations(value: Any = None, *, tool_type: str = "") -> dict[str, Any]:
    """Return complete internal annotations, conservatively defaulted."""
    source = value if isinstance(value, dict) else {}
    external = bool(source.get("external_network", tool_type in {"external", "mcp", "terminal", "action"}))
    destructive = bool(source.get("destructive", True))
    read_only = bool(source.get("read_only", False))
    idempotent = bool(source.get("idempotent", False))
    return {
        "read_only": read_only,
        "destructive": destructive,
        "idempotent": idempotent,
        "external_network": external,
        "approval_required": bool(source.get("approval_required", destructive)),
        "timeout_seconds": max(
            0.01,
            float(source.get("timeout_seconds", TOOL_EXECUTOR_DEFAULT_TIMEOUT_SECONDS)),
        ),
        "output_budget_bytes": max(256, int(source.get("output_budget_bytes", 64 * 1024))),
        "concurrency_class": str(source.get("concurrency_class", "external" if external else "default")),
        "concurrency_limit": max(0, int(source.get("concurrency_limit", 2 if external else 0))),
        "retry_eligible": bool(source.get("retry_eligible", False)) and idempotent and not destructive,
        "max_retries": min(2, max(0, int(source.get("max_retries", 0)))),
        "quota_per_request": max(1, int(source.get("quota_per_request", TOOL_EXECUTOR_MAX_CALLS_PER_REQUEST))),
    }


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Any


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    content: Any
    arguments: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0
    status: str = "success"


class CircuitBreaker:
    """Small in-process breaker; deployment-wide stores can replace it later."""

    def __init__(self, failure_threshold: int = 5, reset_seconds: int = 30):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._opened_at: dict[str, float] = {}

    def allow(self, name: str) -> bool:
        opened = self._opened_at.get(name)
        if opened is None:
            return True
        if time.monotonic() - opened >= self.reset_seconds:
            self._opened_at.pop(name, None)
            self._failures.pop(name, None)
            return True
        return False

    def success(self, name: str) -> None:
        self._failures.pop(name, None)
        self._opened_at.pop(name, None)

    def failure(self, name: str) -> None:
        now = time.monotonic()
        failures = self._failures[name]
        failures.append(now)
        while failures and now - failures[0] > self.reset_seconds:
            failures.popleft()
        if len(failures) >= self.failure_threshold:
            self._opened_at[name] = now


_GLOBAL_SEMAPHORE = asyncio.Semaphore(TOOL_EXECUTOR_GLOBAL_CONCURRENCY)
_CIRCUIT_BREAKER = CircuitBreaker()
_METRICS: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))


def get_tool_metrics_snapshot() -> dict[str, dict[str, float]]:
    """Return process-local counters suitable for a metrics exporter scrape hook."""
    return {tool: dict(values) for tool, values in _METRICS.items()}


def _record_metric(result: ToolResult) -> None:
    values = _METRICS[result.name]
    values["calls"] += 1
    values[f"status.{result.status}"] += 1
    values["latency_ms.total"] += result.latency_ms
    values["latency_ms.max"] = max(values["latency_ms.max"], result.latency_ms)


class ToolExecutor:
    def __init__(
        self,
        tools: dict[str, dict],
        *,
        request_concurrency: int | None = None,
        turn_budget_bytes: int | None = None,
        request_budget_bytes: int | None = None,
        turn_budget_tokens: int | None = None,
        request_budget_tokens: int | None = None,
        max_calls: int | None = None,
        direct_executor: Callable[[str, dict, dict], Awaitable[Any]] | None = None,
        callable_resolver: Callable[[dict, ToolCall], Awaitable[Callable[..., Awaitable[Any]]]] | None = None,
        metric_hook: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.tools = tools
        self.request_concurrency = min(
            TOOL_EXECUTOR_REQUEST_CONCURRENCY,
            max(1, request_concurrency or TOOL_EXECUTOR_REQUEST_CONCURRENCY),
        )
        self.turn_budget = min(
            TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_TURN,
            max(256, turn_budget_bytes or TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_TURN),
        )
        self.request_budget = min(
            TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_REQUEST,
            max(256, request_budget_bytes or TOOL_EXECUTOR_MAX_RESULT_BYTES_PER_REQUEST),
        )
        self.turn_token_budget = min(
            TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_TURN,
            max(1, turn_budget_tokens or TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_TURN),
        )
        self.request_token_budget = min(
            TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_REQUEST,
            max(1, request_budget_tokens or TOOL_EXECUTOR_MAX_RESULT_TOKENS_PER_REQUEST),
        )
        self.max_calls = min(
            TOOL_EXECUTOR_MAX_CALLS_PER_REQUEST,
            max(1, max_calls or TOOL_EXECUTOR_MAX_CALLS_PER_REQUEST),
        )
        self.direct_executor = direct_executor
        self.callable_resolver = callable_resolver
        self.metric_hook = metric_hook
        self.calls_used = 0
        self.bytes_used = 0
        self.tokens_used = 0
        self.tool_counts: dict[str, int] = defaultdict(int)
        self.class_semaphores: dict[str, asyncio.Semaphore] = {}

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict[str, Any] | None:
        if isinstance(arguments, dict):
            return dict(arguments)
        if arguments in (None, ""):
            return {}
        if not isinstance(arguments, str):
            return None
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _serialized_size(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            return len(str(type(value)).encode())

    @classmethod
    def _estimated_tokens(cls, value: Any) -> int:
        # Provider tokenizers differ; bytes/4 is a deterministic enforcement
        # estimate and the parallel exact-byte budget remains authoritative.
        return max(1, (cls._serialized_size(value) + 3) // 4)

    async def _emit_metric(self, result: ToolResult, annotations: dict) -> None:
        if not self.metric_hook:
            return
        event = {
            "tool": result.name,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "external_network": annotations["external_network"],
        }
        maybe_awaitable = self.metric_hook(event)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    async def _execute_one(self, call: ToolCall, semaphore: asyncio.Semaphore) -> ToolResult:
        started = time.monotonic()
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(
                call.call_id,
                call.name,
                structured_error("unknown_tool", call.name),
                status="unknown_tool",
            )
        annotations = normalize_tool_annotations(tool.get("annotations"), tool_type=tool.get("type", ""))
        class_name = annotations["concurrency_class"]
        class_limit = annotations["concurrency_limit"] or self.request_concurrency
        class_semaphore = self.class_semaphores.setdefault(
            class_name, asyncio.Semaphore(min(self.request_concurrency, class_limit))
        )
        if self.tool_counts[call.name] >= annotations["quota_per_request"]:
            return ToolResult(
                call.call_id,
                call.name,
                structured_error("quota_exceeded", call.name),
                status="quota_exceeded",
            )
        self.tool_counts[call.name] += 1
        params = self._parse_arguments(call.arguments)
        if params is None:
            return ToolResult(
                call.call_id,
                call.name,
                structured_error("malformed_arguments", call.name),
                status="malformed_arguments",
            )
        allowed = tool.get("spec", {}).get("parameters", {}).get("properties", {})
        if isinstance(allowed, dict):
            params = {key: value for key, value in params.items() if key in allowed}

        if annotations["external_network"] and not _CIRCUIT_BREAKER.allow(call.name):
            return ToolResult(
                call.call_id,
                call.name,
                structured_error("unavailable", call.name),
                params,
                status="unavailable",
            )

        async def invoke():
            if tool.get("direct"):
                if self.direct_executor is None:
                    raise RuntimeError("direct executor unavailable")
                return await self.direct_executor(call.name, params, tool)
            function = tool.get("callable")
            if self.callable_resolver is not None:
                function = await self.callable_resolver(tool, call)
            if function is None:
                raise RuntimeError("tool callable unavailable")
            return await function(**params)

        status = "success"
        content: Any = None
        attempts = 1 + (annotations["max_retries"] if annotations["retry_eligible"] else 0)
        async with class_semaphore, semaphore, _GLOBAL_SEMAPHORE:
            for attempt in range(attempts):
                try:
                    content = await asyncio.wait_for(invoke(), timeout=annotations["timeout_seconds"])
                    status = "success"
                    if annotations["external_network"]:
                        _CIRCUIT_BREAKER.success(call.name)
                    break
                except asyncio.TimeoutError:
                    status = "timeout"
                    content = structured_error("timeout", call.name)
                    if annotations["external_network"]:
                        _CIRCUIT_BREAKER.failure(call.name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Exception text and tracebacks may contain arguments, credentials,
                    # connection URLs, or upstream response bodies.
                    log.error(
                        "Tool execution failed (tool=%s call_id=%s error_type=%s)",
                        call.name,
                        call.call_id,
                        type(exc).__name__,
                    )
                    status = "internal_error"
                    content = structured_error("internal_error", call.name)
                    if annotations["external_network"]:
                        _CIRCUIT_BREAKER.failure(call.name)
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (2**attempt))

        size = self._serialized_size(content)
        if size > annotations["output_budget_bytes"]:
            status = "budget_exceeded"
            content = structured_error("budget_exceeded", call.name)
        result = ToolResult(
            call.call_id,
            call.name,
            content,
            params,
            (time.monotonic() - started) * 1000,
            status,
        )
        return result

    async def execute_batch(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Execute concurrently and return results in the model-emitted order."""
        if not calls:
            return []
        remaining = max(0, self.max_calls - self.calls_used)
        accepted = calls[:remaining]
        semaphore = asyncio.Semaphore(self.request_concurrency)
        try:
            results = await asyncio.gather(*(self._execute_one(call, semaphore) for call in accepted))
        except asyncio.CancelledError:
            # gather cancels unfinished children; preserving cancellation lets disconnects
            # propagate through the enclosing generation.
            raise
        self.calls_used += len(accepted)

        if len(accepted) < len(calls):
            results.extend(
                ToolResult(
                    call.call_id,
                    call.name,
                    structured_error("quota_exceeded", call.name),
                    status="quota_exceeded",
                )
                for call in calls[len(accepted) :]
            )

        turn_used = 0
        turn_tokens = 0
        for result in results:
            size = self._serialized_size(result.content)
            tokens = self._estimated_tokens(result.content)
            if (
                turn_used + size > self.turn_budget
                or self.bytes_used + size > self.request_budget
                or turn_tokens + tokens > self.turn_token_budget
                or self.tokens_used + tokens > self.request_token_budget
            ):
                result.content = structured_error("budget_exceeded", result.name)
                result.status = "budget_exceeded"
                size = self._serialized_size(result.content)
                tokens = self._estimated_tokens(result.content)
            turn_used += size
            self.bytes_used += size
            turn_tokens += tokens
            self.tokens_used += tokens
            _record_metric(result)
            tool = self.tools.get(result.name) or {}
            await self._emit_metric(
                result,
                normalize_tool_annotations(tool.get("annotations"), tool_type=tool.get("type", "")),
            )
        return results
