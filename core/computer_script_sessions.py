"""Controller-owned Computer Use script connections.

A script connection is a capability for one exact live Agent Turn. It is not a
Session capability: once that Turn ends, a later Turn in the same Session
cannot reuse the connection token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
from typing import TYPE_CHECKING, Any, Callable

from core.computer_cli import COMPUTER_CLI_TOOLS

if TYPE_CHECKING:
    from core.session_turns import SessionTurnManager, Turn


_MAX_SCRIPT_TIMEOUT_SECONDS = 3600.0
_INVALID_BINDING_RETENTION_SECONDS = 60.0
_SCRIPT_TOOLS = frozenset({*COMPUTER_CLI_TOOLS.values(), "double_click"})


class ComputerScriptSessionError(RuntimeError):
    """A script connection request cannot be admitted."""

    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass
class _ScriptBinding:
    token: str
    session_id: str
    turn: "Turn"
    logical_turn_id: str | None
    deadline: float
    timeout_handle: asyncio.TimerHandle
    turn_done_callback: Any
    invalid_code: str | None = None
    cleanup_handle: asyncio.TimerHandle | None = None


class ComputerScriptSessionManager:
    """Bind script calls to the exact Turn that opened the connection."""

    def __init__(self, turn_manager: "SessionTurnManager") -> None:
        self._turn_manager = turn_manager
        self._bindings: dict[str, _ScriptBinding] = {}
        self._invalidation_listeners: list[Callable[[str, str], None]] = []

    def add_invalidation_listener(
        self,
        listener: Callable[[str, str], None],
    ) -> None:
        """Register one owner for state bound to a script connection."""

        if listener not in self._invalidation_listeners:
            self._invalidation_listeners.append(listener)

    def open(self, caller_session_id: str, timeout_seconds: float) -> dict[str, Any]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_SCRIPT_TIMEOUT_SECONDS
        ):
            raise ComputerScriptSessionError(
                "invalid_computer_script_timeout",
                status_code=400,
            )

        admission, admission_error = self._turn_manager.begin_computer_use_request(
            caller_session_id
        )
        if admission is None:
            raise ComputerScriptSessionError(admission_error)

        loop = asyncio.get_running_loop()
        token = secrets.token_urlsafe(32)

        def turn_done(_task: asyncio.Task[Any]) -> None:
            self._invalidate(token, "script_turn_ended")

        timeout_handle = loop.call_later(
            float(timeout_seconds),
            self._invalidate,
            token,
            "script_timeout",
        )
        binding = _ScriptBinding(
            token=token,
            session_id=admission.session_id,
            turn=admission.turn,
            logical_turn_id=admission.logical_turn_id,
            deadline=loop.time() + float(timeout_seconds),
            timeout_handle=timeout_handle,
            turn_done_callback=turn_done,
        )
        self._bindings[token] = binding
        admission.turn.task.add_done_callback(turn_done)
        return {
            "connection_token": token,
            "session_id": admission.session_id,
            "timeout_ms": int(float(timeout_seconds) * 1000),
        }

    def check_caller(self, caller_session_id: str) -> str:
        """Return the caller's Session when it has a live active Turn."""

        admission, admission_error = self._turn_manager.begin_computer_use_request(
            caller_session_id
        )
        if admission is None:
            raise ComputerScriptSessionError(admission_error)
        return admission.session_id

    def admit(self, token: str) -> _ScriptBinding:
        binding = self._bindings.get(token)
        if binding is None:
            raise ComputerScriptSessionError("script_connection_invalid")
        if binding.invalid_code is not None:
            raise ComputerScriptSessionError(binding.invalid_code)

        loop = asyncio.get_running_loop()
        if loop.time() >= binding.deadline:
            self._invalidate(token, "script_timeout")
            raise ComputerScriptSessionError("script_timeout")

        admission, _error = self._turn_manager.begin_computer_use_request(
            binding.session_id
        )
        if (
            admission is None
            or admission.turn is not binding.turn
            or admission.logical_turn_id != binding.logical_turn_id
        ):
            self._invalidate(token, "script_turn_ended")
            raise ComputerScriptSessionError("script_turn_ended")
        return binding

    def prepare_tool_request(
        self,
        token: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        binding = self.admit(token)
        if tool not in _SCRIPT_TOOLS:
            raise ComputerScriptSessionError(
                "invalid_computer_script_tool",
                status_code=400,
            )
        if "session" in arguments:
            raise ComputerScriptSessionError(
                "session_argument_forbidden",
                status_code=400,
            )
        return binding.session_id, {**arguments, "session": binding.session_id}

    def close(self, token: str) -> bool:
        binding = self._bindings.pop(token, None)
        if binding is None:
            return False
        self._cancel_binding_handles(binding)
        if binding.invalid_code is None:
            # A normal close ends the connection too: state bound to it, such as
            # a pending confirm, must not outlive the script.
            self._notify_invalidation(token, "script_connection_invalid")
        return True

    def close_all(self) -> None:
        for token in list(self._bindings):
            self.close(token)

    def invalidate_turn(self, turn: "Turn") -> None:
        """Invalidate every script capability bound to one exact Turn."""

        for token, binding in list(self._bindings.items()):
            if binding.turn is turn:
                self._invalidate(token, "script_turn_ended")

    def _invalidate(self, token: str, code: str) -> None:
        binding = self._bindings.get(token)
        if binding is None or binding.invalid_code is not None:
            return
        binding.invalid_code = code
        binding.timeout_handle.cancel()
        binding.turn.task.remove_done_callback(binding.turn_done_callback)
        binding.cleanup_handle = asyncio.get_running_loop().call_later(
            _INVALID_BINDING_RETENTION_SECONDS,
            self.close,
            token,
        )
        self._notify_invalidation(token, code)

    def _notify_invalidation(self, token: str, code: str) -> None:
        for listener in tuple(self._invalidation_listeners):
            try:
                listener(token, code)
            except Exception:
                # A secondary owner must not prevent the connection itself from
                # becoming invalid. Its own tests and logs own that failure.
                continue

    @staticmethod
    def _cancel_binding_handles(binding: _ScriptBinding) -> None:
        binding.timeout_handle.cancel()
        if binding.cleanup_handle is not None:
            binding.cleanup_handle.cancel()
        binding.turn.task.remove_done_callback(binding.turn_done_callback)
