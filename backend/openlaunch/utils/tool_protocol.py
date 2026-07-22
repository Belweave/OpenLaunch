"""Provider call/result normalization used by the tool conformance suite."""

from __future__ import annotations

import json
from typing import Any

from openlaunch.utils.tool_executor import ToolCall, ToolResult


def normalize_provider_calls(provider: str, payload: dict[str, Any]) -> list[ToolCall]:
    if provider == "openai_chat":
        calls = payload.get("tool_calls") or payload.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
        return [
            ToolCall(
                call.get("id", ""),
                call.get("function", {}).get("name", ""),
                call.get("function", {}).get("arguments", "{}"),
            )
            for call in calls
        ]
    if provider == "openai_responses":
        return [
            ToolCall(
                item.get("call_id") or item.get("id", ""),
                item.get("name", ""),
                item.get("arguments", "{}"),
            )
            for item in payload.get("output", [])
            if item.get("type") == "function_call"
        ]
    if provider == "anthropic":
        return [
            ToolCall(item.get("id", ""), item.get("name", ""), item.get("input", {}))
            for item in payload.get("content", [])
            if item.get("type") == "tool_use"
        ]
    raise ValueError("Unsupported tool protocol.")


def format_provider_results(provider: str, results: list[ToolResult]) -> list[dict[str, Any]]:
    if provider == "openai_chat":
        return [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": json.dumps(result.content, default=str),
            }
            for result in results
        ]
    if provider == "openai_responses":
        return [
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": json.dumps(result.content, default=str),
                "status": "completed",
            }
            for result in results
        ]
    if provider == "anthropic":
        # Anthropic requires sibling results in one user content block.
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": json.dumps(result.content, default=str),
                        "is_error": result.status != "success",
                    }
                    for result in results
                ],
            }
        ]
    raise ValueError("Unsupported tool protocol.")
