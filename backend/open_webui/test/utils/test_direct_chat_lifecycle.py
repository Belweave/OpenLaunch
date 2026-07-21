import asyncio
import json
from types import SimpleNamespace

import pytest


class FakeSocketServer:
    def __init__(self):
        self.handlers = {"/": {}}
        self.emitted = []

    def on(self, event, handler):
        self.handlers["/"][event] = handler

    async def emit(self, event, data, to=None):
        self.emitted.append((event, data, to))


def make_request(first_token_timeout=0.01, stream_idle_timeout=0.01):
    config = SimpleNamespace(
        DIRECT_CONNECTION_SOCKET_ACK_TIMEOUT=0.01,
        DIRECT_CONNECTION_FIRST_TOKEN_TIMEOUT=first_token_timeout,
        DIRECT_CONNECTION_STREAM_IDLE_TIMEOUT=stream_idle_timeout,
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))


def make_form_data():
    return {
        "model": "direct-model",
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {
            "user_id": "user-1",
            "session_id": "session-1",
            "chat_id": "chat-1",
            "message_id": "message-1",
            "operation_id": "operation-1",
        },
    }


def decode_sse(line):
    assert line.startswith("data: ")
    return json.loads(line.removeprefix("data: ").strip())


async def next_non_lifecycle_event(response):
    while True:
        event = decode_sse(await asyncio.wait_for(anext(response.body_iterator), 0.2))
        if "error" in event or "choices" in event:
            return event


def test_direct_stream_times_out_before_first_token_and_cleans_listener(monkeypatch):
    from open_webui.utils import chat

    async def run():
        fake_sio = FakeSocketServer()
        monkeypatch.setattr(chat, "sio", fake_sio)

        async def event_caller(_event):
            return {"status": True}

        monkeypatch.setattr(chat, "get_event_call", lambda _metadata: event_caller)

        response = await chat.generate_direct_chat_completion(
            make_request(),
            make_form_data(),
            user=SimpleNamespace(id="user-1"),
            models={"direct-model": {"id": "direct-model"}},
        )
        _channel, listener = next(iter(fake_sio.handlers["/"].items()))
        await listener(
            "session-1",
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
        )

        role_event = await next_non_lifecycle_event(response)
        assert role_event["choices"][0]["delta"]["role"] == "assistant"
        event = await next_non_lifecycle_event(response)

        assert event["error"]["code"] == "direct_first_token_timeout"
        assert event["error"]["stage"] == "provider_first_token"
        assert event["error"]["state"] == "timed_out"
        assert event["error"]["operation_id"] == "operation-1"
        assert fake_sio.handlers["/"] == {}

    asyncio.run(run())


def test_direct_stream_distinguishes_idle_timeout_after_first_event(monkeypatch):
    from open_webui.utils import chat

    async def run():
        fake_sio = FakeSocketServer()
        monkeypatch.setattr(chat, "sio", fake_sio)

        async def event_caller(_event):
            return {"status": True}

        monkeypatch.setattr(chat, "get_event_call", lambda _metadata: event_caller)

        response = await chat.generate_direct_chat_completion(
            make_request(),
            make_form_data(),
            user=SimpleNamespace(id="user-1"),
            models={"direct-model": {"id": "direct-model"}},
        )

        channel, listener = next(iter(fake_sio.handlers["/"].items()))
        await listener(
            "session-1",
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
        )

        first_event = await next_non_lifecycle_event(response)
        assert first_event["choices"][0]["delta"]["content"] == "hello"

        timeout_event = await next_non_lifecycle_event(response)
        assert timeout_event["error"]["code"] == "direct_stream_idle_timeout"
        assert timeout_event["error"]["stage"] == "provider_stream"
        assert timeout_event["error"]["state"] == "timed_out"
        assert timeout_event["error"]["operation_id"] == "operation-1"
        assert channel not in fake_sio.handlers["/"]

    asyncio.run(run())


def test_direct_stream_bounds_socket_acknowledgement(monkeypatch):
    from open_webui.utils import chat
    from open_webui.utils.operations import OperationException

    async def run():
        fake_sio = FakeSocketServer()
        monkeypatch.setattr(chat, "sio", fake_sio)

        async def event_caller(_event):
            await asyncio.Event().wait()

        monkeypatch.setattr(chat, "get_event_call", lambda _metadata: event_caller)

        with pytest.raises(OperationException) as exc_info:
            await chat.generate_direct_chat_completion(
                make_request(),
                make_form_data(),
                user=SimpleNamespace(id="user-1"),
                models={"direct-model": {"id": "direct-model"}},
            )

        assert exc_info.value.error.code == "direct_socket_ack_timeout"
        assert exc_info.value.error.state.value == "timed_out"
        assert fake_sio.handlers["/"] == {}
        assert any(
            event[1]["data"]["type"] == "request:chat:completion:cancel"
            for event in fake_sio.emitted
        )

    asyncio.run(run())


def test_non_streaming_direct_request_is_also_bounded(monkeypatch):
    from open_webui.utils import chat
    from open_webui.utils.operations import OperationException

    async def run():
        fake_sio = FakeSocketServer()
        monkeypatch.setattr(chat, "sio", fake_sio)

        async def event_caller(_event):
            await asyncio.Event().wait()

        monkeypatch.setattr(chat, "get_event_call", lambda _metadata: event_caller)
        form_data = make_form_data()
        form_data["stream"] = False

        with pytest.raises(OperationException) as exc_info:
            await chat.generate_direct_chat_completion(
                make_request(),
                form_data,
                user=SimpleNamespace(id="user-1"),
                models={"direct-model": {"id": "direct-model"}},
            )

        assert exc_info.value.error.code == "direct_socket_ack_timeout"
        assert exc_info.value.error.operation_id == "operation-1"

    asyncio.run(run())


def test_browser_disconnect_terminates_relay_immediately(monkeypatch):
    from open_webui.socket.main import cancel_direct_relays_for_session
    from open_webui.utils import chat

    async def run():
        fake_sio = FakeSocketServer()
        monkeypatch.setattr(chat, "sio", fake_sio)

        async def event_caller(_event):
            return {"status": True}

        monkeypatch.setattr(chat, "get_event_call", lambda _metadata: event_caller)

        response = await chat.generate_direct_chat_completion(
            make_request(first_token_timeout=10),
            make_form_data(),
            user=SimpleNamespace(id="user-1"),
            models={"direct-model": {"id": "direct-model"}},
        )
        channel = next(iter(fake_sio.handlers["/"]))

        cancel_direct_relays_for_session("session-1")
        event = await next_non_lifecycle_event(response)

        assert event["error"]["code"] == "direct_browser_disconnected"
        assert event["error"]["state"] == "cancelled"
        assert channel not in fake_sio.handlers["/"]

    asyncio.run(run())


def test_operation_events_are_versioned_and_sequenced():
    from open_webui.utils.operations import (
        OperationTracker,
        should_apply_operation_event,
    )

    tracker = OperationTracker("operation-1")
    first = tracker.event("operation.state", {"state": "running"})
    second = tracker.event("operation.state", {"state": "succeeded"})

    assert first["version"] == 1
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["operation_id"] == "operation-1"
    assert should_apply_operation_event(first, second)
    assert not should_apply_operation_event(second, first)

    terminal = tracker.event("operation.state", {"state": "timed_out"})
    late_running = tracker.event("operation.state", {"state": "running"})
    assert not should_apply_operation_event(terminal, late_running)
