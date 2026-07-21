import time
import logging
import sys

from aiocache import cached
from typing import Any, Optional
import random
import json
import inspect
import uuid
import asyncio

from fastapi import Request, status
from starlette.responses import Response, StreamingResponse, JSONResponse


from open_webui.models.users import UserModel

from open_webui.socket.main import (
    sio,
    get_event_call,
    get_event_emitter,
    register_direct_relay,
    unregister_direct_relay,
)
from open_webui.functions import generate_function_chat_completion

from open_webui.routers.openai import (
    generate_chat_completion as generate_openai_chat_completion,
)

from open_webui.routers.ollama import (
    generate_chat_completion as generate_ollama_chat_completion,
)

from open_webui.routers.pipelines import (
    process_pipeline_inlet_filter,
    process_pipeline_outlet_filter,
)

from open_webui.models.functions import Functions
from open_webui.models.models import Models


from open_webui.utils.plugin import load_function_module_by_id
from open_webui.utils.models import get_all_models, check_model_access
from open_webui.utils.payload import convert_payload_openai_to_ollama
from open_webui.utils.response import (
    convert_response_ollama_to_openai,
    convert_streaming_response_ollama_to_openai,
)
from open_webui.utils.filter import (
    get_sorted_filter_ids,
    process_filter_functions,
)
from open_webui.utils.operations import (
    OperationException,
    OperationState,
    OperationTracker,
    error_payload,
    operation_error,
)

from open_webui.env import SRC_LOG_LEVELS, GLOBAL_LOG_LEVEL, BYPASS_MODEL_ACCESS_CONTROL


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


def _direct_timeout(request: Request, name: str, default: float) -> float:
    value = getattr(request.app.state.config, name, default)
    return max(float(value), 0.001)


def _remove_socket_handler(channel: str) -> None:
    namespace_handlers = sio.handlers.get("/", {})
    namespace_handlers.pop(channel, None)


async def _cancel_direct_browser_request(metadata: dict, channel: str, reason: str):
    session_id = metadata.get("session_id")
    if not session_id:
        return

    await sio.emit(
        "chat-events",
        {
            "chat_id": metadata.get("chat_id"),
            "message_id": metadata.get("message_id"),
            "data": {
                "type": "request:chat:completion:cancel",
                "data": {
                    "session_id": session_id,
                    "channel": channel,
                    "reason": reason,
                },
            },
        },
        to=session_id,
    )


def _sse_data(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _direct_event_has_content(data: Any) -> bool:
    if isinstance(data, str):
        stripped = data.strip()
        if not stripped.startswith("data:"):
            return bool(stripped)
        try:
            data = json.loads(stripped.removeprefix("data:").strip())
        except (TypeError, json.JSONDecodeError):
            return False

    if not isinstance(data, dict):
        return False

    for choice in data.get("choices", []):
        delta = choice.get("delta", {})
        message = choice.get("message", {})
        if delta.get("content") or message.get("content"):
            return True
    return False


def _log_direct_terminal(
    *,
    metadata: dict,
    operation_id: str,
    model_id: str,
    started_at: float,
    stage: str,
    state: str,
    code: str = "",
    level: int = logging.INFO,
):
    log.log(
        level,
        "direct_relay_terminal operation_id=%s user_id=%s chat_id=%s message_id=%s "
        "provider=direct_browser model_id=%s stage=%s duration_ms=%s terminal_state=%s code=%s",
        operation_id,
        metadata.get("user_id"),
        metadata.get("chat_id"),
        metadata.get("message_id"),
        model_id,
        stage,
        int((time.monotonic() - started_at) * 1000),
        state,
        code,
    )


async def generate_direct_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    models: dict,
):
    log.info("generate_direct_chat_completion")

    metadata = form_data.pop("metadata", {})

    user_id = metadata.get("user_id")
    session_id = metadata.get("session_id")
    operation_id = metadata.get("operation_id") or str(uuid.uuid4())
    metadata["operation_id"] = operation_id
    request_id = operation_id
    tracker = OperationTracker(operation_id)
    started_at = time.monotonic()
    model_id = form_data["model"]

    event_caller = get_event_call(metadata)

    channel = f"{user_id}:{session_id}:{request_id}"

    if form_data.get("stream"):
        q = asyncio.Queue()

        async def message_listener(sid, data):
            """
            Handle received socket messages and push them into the queue.
            """
            if sid == session_id:
                await q.put(data)

        # Register the listener
        sio.on(channel, message_listener)
        register_direct_relay(session_id, channel, q)

        # Start processing chat completion in background
        try:
            res = await asyncio.wait_for(
                event_caller(
                    {
                        "type": "request:chat:completion",
                        "data": {
                            "form_data": form_data,
                            "model": models[form_data["model"]],
                            "channel": channel,
                            "session_id": session_id,
                            "operation_id": operation_id,
                        },
                    }
                ),
                timeout=_direct_timeout(
                    request, "DIRECT_CONNECTION_SOCKET_ACK_TIMEOUT", 15
                ),
            )
        except asyncio.TimeoutError as exc:
            error = operation_error(
                code="direct_socket_ack_timeout",
                message="The browser did not acknowledge the direct model request in time.",
                stage="direct_relay_acknowledgement",
                state=OperationState.TIMED_OUT,
                operation_id=operation_id,
                retryable=True,
            )
            _remove_socket_handler(channel)
            unregister_direct_relay(session_id, channel)
            await _cancel_direct_browser_request(metadata, channel, error.code)
            _log_direct_terminal(
                metadata=metadata,
                operation_id=operation_id,
                model_id=model_id,
                started_at=started_at,
                stage=error.stage,
                state=error.state.value,
                code=error.code,
                level=logging.WARNING,
            )
            raise OperationException(error) from exc
        except Exception:
            _remove_socket_handler(channel)
            unregister_direct_relay(session_id, channel)
            raise

        log.info(f"res: {res}")

        if isinstance(res, dict) and res.get("status", False):
            # Define a generator to stream responses
            async def event_generator():
                nonlocal q
                received_content = False
                try:
                    yield _sse_data(
                        {
                            "event": {
                                "type": "operation",
                                "data": tracker.event(
                                    "operation.state",
                                    {
                                        "state": OperationState.RUNNING.value,
                                        "stage": "provider_first_token",
                                    },
                                ),
                            }
                        }
                    )
                    while True:
                        timeout = _direct_timeout(
                            request,
                            (
                                "DIRECT_CONNECTION_STREAM_IDLE_TIMEOUT"
                                if received_content
                                else "DIRECT_CONNECTION_FIRST_TOKEN_TIMEOUT"
                            ),
                            60 if received_content else 45,
                        )
                        try:
                            data = await asyncio.wait_for(q.get(), timeout=timeout)
                        except asyncio.TimeoutError:
                            error = operation_error(
                                code=(
                                    "direct_stream_idle_timeout"
                                    if received_content
                                    else "direct_first_token_timeout"
                                ),
                                message=(
                                    "The direct model stream stopped responding. You can retry the response."
                                    if received_content
                                    else "The direct model did not begin responding in time. You can retry the response."
                                ),
                                stage=(
                                    "provider_stream"
                                    if received_content
                                    else "provider_first_token"
                                ),
                                state=OperationState.TIMED_OUT,
                                operation_id=operation_id,
                                retryable=True,
                            )
                            await _cancel_direct_browser_request(
                                metadata, channel, error.code
                            )
                            _log_direct_terminal(
                                metadata=metadata,
                                operation_id=operation_id,
                                model_id=model_id,
                                started_at=started_at,
                                stage=error.stage,
                                state=error.state.value,
                                code=error.code,
                                level=logging.WARNING,
                            )
                            _remove_socket_handler(channel)
                            unregister_direct_relay(session_id, channel)
                            yield _sse_data(
                                {
                                    "event": {
                                        "type": "operation",
                                        "data": tracker.event(
                                            "operation.state",
                                            {
                                                "state": error.state.value,
                                                "stage": error.stage,
                                                "error": error_payload(error),
                                            },
                                        ),
                                    },
                                    "error": error_payload(error),
                                }
                            )
                            break

                        if isinstance(data, dict):
                            if data.get("relay_disconnect"):
                                error = operation_error(
                                    code="direct_browser_disconnected",
                                    message="The browser disconnected while the direct model was responding.",
                                    stage="direct_relay",
                                    state=OperationState.CANCELLED,
                                    operation_id=operation_id,
                                    retryable=True,
                                )
                                _remove_socket_handler(channel)
                                unregister_direct_relay(session_id, channel)
                                _log_direct_terminal(
                                    metadata=metadata,
                                    operation_id=operation_id,
                                    model_id=model_id,
                                    started_at=started_at,
                                    stage=error.stage,
                                    state=error.state.value,
                                    code=error.code,
                                )
                                yield _sse_data(
                                    {
                                        "event": {
                                            "type": "operation",
                                            "data": tracker.event(
                                                "operation.state",
                                                {
                                                    "state": error.state.value,
                                                    "stage": error.stage,
                                                    "error": error_payload(error),
                                                },
                                            ),
                                        },
                                        "error": error_payload(error),
                                    }
                                )
                                break

                            if "done" in data and data["done"]:
                                _remove_socket_handler(channel)
                                unregister_direct_relay(session_id, channel)
                                _log_direct_terminal(
                                    metadata=metadata,
                                    operation_id=operation_id,
                                    model_id=model_id,
                                    started_at=started_at,
                                    stage="completion",
                                    state=OperationState.SUCCEEDED.value,
                                )
                                yield _sse_data(
                                    {
                                        "event": {
                                            "type": "operation",
                                            "data": tracker.event(
                                                "operation.state",
                                                {
                                                    "state": OperationState.SUCCEEDED.value,
                                                    "stage": "completion",
                                                },
                                            ),
                                        }
                                    }
                                )
                                break

                            received_content = (
                                received_content or _direct_event_has_content(data)
                            )
                            yield _sse_data(data)
                        elif isinstance(data, str):
                            received_content = (
                                received_content or _direct_event_has_content(data)
                            )
                            yield data
                except asyncio.CancelledError:
                    await _cancel_direct_browser_request(
                        metadata, channel, "request_cancelled"
                    )
                    _log_direct_terminal(
                        metadata=metadata,
                        operation_id=operation_id,
                        model_id=model_id,
                        started_at=started_at,
                        stage="provider_stream",
                        state=OperationState.CANCELLED.value,
                        code="request_cancelled",
                    )
                    raise
                except Exception as e:
                    log.exception(
                        "direct_relay_failed operation_id=%s stage=provider_stream",
                        operation_id,
                    )
                    raise
                finally:
                    _remove_socket_handler(channel)
                    unregister_direct_relay(session_id, channel)

            # Define a background task to run the event generator
            async def background():
                _remove_socket_handler(channel)
                unregister_direct_relay(session_id, channel)

            # Return the streaming response
            return StreamingResponse(
                event_generator(), media_type="text/event-stream", background=background
            )
        else:
            _remove_socket_handler(channel)
            unregister_direct_relay(session_id, channel)
            error = operation_error(
                code="direct_browser_request_failed",
                message="The browser could not start the direct model request.",
                stage="direct_relay_acknowledgement",
                state=OperationState.FAILED,
                operation_id=operation_id,
                retryable=True,
            )
            _log_direct_terminal(
                metadata=metadata,
                operation_id=operation_id,
                model_id=model_id,
                started_at=started_at,
                stage=error.stage,
                state=error.state.value,
                code=error.code,
                level=logging.WARNING,
            )
            raise OperationException(error)
    else:
        try:
            res = await asyncio.wait_for(
                event_caller(
                    {
                        "type": "request:chat:completion",
                        "data": {
                            "form_data": form_data,
                            "model": models[form_data["model"]],
                            "channel": channel,
                            "session_id": session_id,
                            "operation_id": operation_id,
                        },
                    }
                ),
                timeout=_direct_timeout(
                    request, "DIRECT_CONNECTION_SOCKET_ACK_TIMEOUT", 15
                ),
            )
        except asyncio.TimeoutError as exc:
            error = operation_error(
                code="direct_socket_ack_timeout",
                message="The browser did not complete the direct model request in time.",
                stage="direct_relay_acknowledgement",
                state=OperationState.TIMED_OUT,
                operation_id=operation_id,
                retryable=True,
            )
            await _cancel_direct_browser_request(metadata, channel, error.code)
            _log_direct_terminal(
                metadata=metadata,
                operation_id=operation_id,
                model_id=model_id,
                started_at=started_at,
                stage=error.stage,
                state=error.state.value,
                code=error.code,
                level=logging.WARNING,
            )
            raise OperationException(error) from exc

        if not isinstance(res, dict) or res.get("error"):
            error = operation_error(
                code="direct_browser_request_failed",
                message="The browser could not complete the direct model request.",
                stage="direct_relay",
                state=OperationState.FAILED,
                operation_id=operation_id,
                retryable=True,
            )
            _log_direct_terminal(
                metadata=metadata,
                operation_id=operation_id,
                model_id=model_id,
                started_at=started_at,
                stage=error.stage,
                state=error.state.value,
                code=error.code,
                level=logging.WARNING,
            )
            raise OperationException(error)

        _log_direct_terminal(
            metadata=metadata,
            operation_id=operation_id,
            model_id=model_id,
            started_at=started_at,
            stage="completion",
            state=OperationState.SUCCEEDED.value,
        )
        return res


async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user: Any,
    bypass_filter: bool = False,
):
    log.debug(f"generate_chat_completion: {form_data}")
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    if hasattr(request.state, "metadata"):
        if "metadata" not in form_data:
            form_data["metadata"] = request.state.metadata
        else:
            form_data["metadata"] = {
                **form_data["metadata"],
                **request.state.metadata,
            }

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
        log.debug(f"direct connection to model: {models}")
    else:
        models = request.app.state.MODELS

    model_id = form_data["model"]
    if model_id not in models:
        raise Exception("Model not found")

    model = models[model_id]

    if getattr(request.state, "direct", False):
        return await generate_direct_chat_completion(
            request, form_data, user=user, models=models
        )
    else:
        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            try:
                check_model_access(user, model)
            except Exception as e:
                raise e

        if model.get("owned_by") == "arena":
            model_ids = model.get("info", {}).get("meta", {}).get("model_ids")
            filter_mode = model.get("info", {}).get("meta", {}).get("filter_mode")
            if model_ids and filter_mode == "exclude":
                model_ids = [
                    model["id"]
                    for model in list(request.app.state.MODELS.values())
                    if model.get("owned_by") != "arena" and model["id"] not in model_ids
                ]

            selected_model_id = None
            if isinstance(model_ids, list) and model_ids:
                selected_model_id = random.choice(model_ids)
            else:
                model_ids = [
                    model["id"]
                    for model in list(request.app.state.MODELS.values())
                    if model.get("owned_by") != "arena"
                ]
                selected_model_id = random.choice(model_ids)

            form_data["model"] = selected_model_id

            if form_data.get("stream") == True:

                async def stream_wrapper(stream):
                    yield f"data: {json.dumps({'selected_model_id': selected_model_id})}\n\n"
                    async for chunk in stream:
                        yield chunk

                response = await generate_chat_completion(
                    request, form_data, user, bypass_filter=True
                )
                return StreamingResponse(
                    stream_wrapper(response.body_iterator),
                    media_type="text/event-stream",
                    background=response.background,
                )
            else:
                return {
                    **(
                        await generate_chat_completion(
                            request, form_data, user, bypass_filter=True
                        )
                    ),
                    "selected_model_id": selected_model_id,
                }

        if model.get("pipe"):
            # Below does not require bypass_filter because this is the only route the uses this function and it is already bypassing the filter
            return await generate_function_chat_completion(
                request, form_data, user=user, models=models
            )
        if model.get("owned_by") == "ollama":
            # Using /ollama/api/chat endpoint
            form_data = convert_payload_openai_to_ollama(form_data)
            response = await generate_ollama_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
                bypass_filter=bypass_filter,
            )
            if form_data.get("stream"):
                response.headers["content-type"] = "text/event-stream"
                return StreamingResponse(
                    convert_streaming_response_ollama_to_openai(response),
                    headers=dict(response.headers),
                    background=response.background,
                )
            else:
                return convert_response_ollama_to_openai(response)
        else:
            return await generate_openai_chat_completion(
                request=request,
                form_data=form_data,
                user=user,
                bypass_filter=bypass_filter,
            )


chat_completion = generate_chat_completion


async def chat_completed(request: Request, form_data: dict, user: Any):
    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    data = form_data
    model_id = data["model"]
    if model_id not in models:
        raise Exception("Model not found")

    model = models[model_id]

    try:
        data = await process_pipeline_outlet_filter(request, data, user, models)
    except Exception as e:
        return Exception(f"Error: {e}")

    metadata = {
        "chat_id": data["chat_id"],
        "message_id": data["id"],
        "session_id": data["session_id"],
        "user_id": user.id,
    }

    extra_params = {
        "__event_emitter__": get_event_emitter(metadata),
        "__event_call__": get_event_call(metadata),
        "__user__": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
        "__metadata__": metadata,
        "__request__": request,
        "__model__": model,
    }

    try:
        filter_functions = [
            Functions.get_function_by_id(filter_id)
            for filter_id in get_sorted_filter_ids(model)
        ]

        result, _ = await process_filter_functions(
            request=request,
            filter_functions=filter_functions,
            filter_type="outlet",
            form_data=data,
            extra_params=extra_params,
        )
        return result
    except Exception as e:
        return Exception(f"Error: {e}")


async def chat_action(request: Request, action_id: str, form_data: dict, user: Any):
    if "." in action_id:
        action_id, sub_action_id = action_id.split(".")
    else:
        sub_action_id = None

    action = Functions.get_function_by_id(action_id)
    if not action:
        raise Exception(f"Action not found: {action_id}")

    if not request.app.state.MODELS:
        await get_all_models(request, user=user)

    if getattr(request.state, "direct", False) and hasattr(request.state, "model"):
        models = {
            request.state.model["id"]: request.state.model,
        }
    else:
        models = request.app.state.MODELS

    data = form_data
    model_id = data["model"]

    if model_id not in models:
        raise Exception("Model not found")
    model = models[model_id]

    __event_emitter__ = get_event_emitter(
        {
            "chat_id": data["chat_id"],
            "message_id": data["id"],
            "session_id": data["session_id"],
            "user_id": user.id,
        }
    )
    __event_call__ = get_event_call(
        {
            "chat_id": data["chat_id"],
            "message_id": data["id"],
            "session_id": data["session_id"],
            "user_id": user.id,
        }
    )

    if action_id in request.app.state.FUNCTIONS:
        function_module = request.app.state.FUNCTIONS[action_id]
    else:
        function_module, _, _ = load_function_module_by_id(action_id)
        request.app.state.FUNCTIONS[action_id] = function_module

    if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
        valves = Functions.get_function_valves_by_id(action_id)
        function_module.valves = function_module.Valves(**(valves if valves else {}))

    if hasattr(function_module, "action"):
        try:
            action = function_module.action

            # Get the signature of the function
            sig = inspect.signature(action)
            params = {"body": data}

            # Extra parameters to be passed to the function
            extra_params = {
                "__model__": model,
                "__id__": sub_action_id if sub_action_id is not None else action_id,
                "__event_emitter__": __event_emitter__,
                "__event_call__": __event_call__,
                "__request__": request,
            }

            # Add extra params in contained in function signature
            for key, value in extra_params.items():
                if key in sig.parameters:
                    params[key] = value

            if "__user__" in sig.parameters:
                __user__ = {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                }

                try:
                    if hasattr(function_module, "UserValves"):
                        __user__["valves"] = function_module.UserValves(
                            **Functions.get_user_valves_by_id_and_user_id(
                                action_id, user.id
                            )
                        )
                except Exception as e:
                    log.exception(f"Failed to get user values: {e}")

                params = {**params, "__user__": __user__}

            if inspect.iscoroutinefunction(action):
                data = await action(**params)
            else:
                data = action(**params)

        except Exception as e:
            return Exception(f"Error: {e}")

    return data
