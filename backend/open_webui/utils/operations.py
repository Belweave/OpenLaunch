from enum import Enum
from time import time
from typing import Any, Optional

from pydantic import BaseModel


OPERATION_EVENT_VERSION = 1
TERMINAL_OPERATION_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
}


class OperationState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class OperationError(BaseModel):
    code: str
    message: str
    stage: str
    state: OperationState
    operation_id: str
    retryable: bool = False


class OperationEvent(BaseModel):
    version: int = OPERATION_EVENT_VERSION
    operation_id: str
    sequence: int
    type: str
    timestamp: float
    payload: dict[str, Any]


class OperationTracker:
    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self.sequence = 0

    def event(self, event_type: str, payload: Optional[dict[str, Any]] = None) -> dict:
        self.sequence += 1
        return OperationEvent(
            operation_id=self.operation_id,
            sequence=self.sequence,
            type=event_type,
            timestamp=time(),
            payload=payload or {},
        ).model_dump(mode="json")


class OperationException(Exception):
    def __init__(self, error: OperationError):
        super().__init__(error.message)
        self.error = error


def operation_error(
    *,
    code: str,
    message: str,
    stage: str,
    state: OperationState,
    operation_id: str,
    retryable: bool = False,
) -> OperationError:
    return OperationError(
        code=code,
        message=message,
        stage=stage,
        state=state,
        operation_id=operation_id,
        retryable=retryable,
    )


def error_payload(error: OperationError) -> dict:
    return error.model_dump(mode="json")


def should_apply_operation_event(current: Optional[dict], incoming: dict) -> bool:
    if not current or current.get("operation_id") != incoming.get("operation_id"):
        return True

    current_state = current.get("payload", {}).get("state")
    if current_state in TERMINAL_OPERATION_STATES:
        return False

    return incoming.get("sequence", 0) > current.get("sequence", 0)
