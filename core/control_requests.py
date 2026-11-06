"""Versioned requests over Avibe's existing private control channel.

Caller fields preserve CLI behavior; they are not an identity/security boundary.
A deadline bounds admission, not an already-started operation. No request is
replayed and there is deliberately no new durable operation-id protocol.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from core.caller_context import CALLER_CONTEXT_ENV_NAMES

VERSION = 1
ADMISSION_SECONDS = 30.0


@dataclass(frozen=True)
class ControlRequest:
    operation: str
    arguments: dict[str, Any]
    caller_env: dict[str, str]
    cwd: str
    deadline: float

    def to_payload(self) -> dict[str, Any]:
        return {"version": VERSION, **vars(self)}

    @classmethod
    def from_payload(cls, payload: object) -> ControlRequest:
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != VERSION:
            raise ValueError("Unsupported control request version")
        operation = payload.get("operation")
        arguments = payload.get("arguments")
        caller_env = payload.get("caller_env")
        cwd = payload.get("cwd")
        deadline = payload.get("deadline")
        if (
            not isinstance(operation, str)
            or not isinstance(arguments, dict)
            or not isinstance(caller_env, dict)
            or any(k not in CALLER_CONTEXT_ENV_NAMES or not isinstance(v, str) for k, v in caller_env.items())
            or not isinstance(cwd, str)
            or not os.path.isabs(cwd)
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("Invalid control request")
        return cls(operation, arguments, caller_env, cwd, float(deadline))

    @property
    def expired(self) -> bool:
        return time.time() >= self.deadline


def new_request(operation: str, arguments: Mapping[str, Any]) -> ControlRequest:
    return ControlRequest(
        operation=operation,
        arguments=dict(arguments),
        caller_env={key: os.environ[key] for key in CALLER_CONTEXT_ENV_NAMES if key in os.environ},
        cwd=os.getcwd(),
        deadline=time.time() + ADMISSION_SECONDS,
    )


def result(payload: dict[str, Any], *, execution: str = "completed") -> dict[str, Any]:
    return {"version": VERSION, "execution": execution, "exit_code": 0 if payload.get("ok") else 1, "payload": payload}


def failure(code: str, message: str, *, execution: str = "not_executed") -> dict[str, Any]:
    hint = (
        "The operation was not executed. Start Avibe explicitly before trying again."
        if code == "service_unavailable"
        else "Do not retry blindly; inspect the resulting state before submitting new work."
        if execution == "execution_unknown"
        else "The operation was not executed."
    )
    return result(
        {"schema_version": 1, "ok": False, "kind": "error", "code": code,
         "error": message, "execution": execution, "hint": hint},
        execution=execution,
    )


def validate_result(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VERSION
        or value.get("execution") not in {"completed", "not_executed", "execution_unknown"}
        or type(value.get("exit_code")) is not int
        or value["exit_code"] not in {0, 1}
        or not isinstance(value.get("payload"), dict)
        or type(value["payload"].get("ok")) is not bool
        or (value["exit_code"] == 0) != value["payload"]["ok"]
    ):
        raise ValueError("Invalid control response; execution is unknown")
    return value
