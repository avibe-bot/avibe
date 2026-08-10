"""In-memory lifecycle tracking for Codex turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from modules.agents.base import AgentRequest


@dataclass
class CodexTurnState:
    turn_id: str
    request: AgentRequest
    pending_assistant: Optional[Tuple[str, Optional[str]]] = None
    terminal_error: Optional[str] = None
    terminal_error_notified: bool = False
    visible_to_user: bool = True
    indicator_cleanup_claimed: bool = False


@dataclass
class CodexPendingTurnStart:
    request: AgentRequest
    thread_id: str
    turn_id: Optional[str] = None
    completed: bool = False


class CodexTurnRegistry:
    """Owns Codex turn-scoped request routing and lifecycle state."""

    def __init__(self) -> None:
        self._turns: dict[str, CodexTurnState] = {}
        self._active_turns: dict[str, str] = {}
        self._latest_requests: dict[str, AgentRequest] = {}
        self._pending_turn_starts: dict[str, CodexPendingTurnStart] = {}

    def remember_request(self, request: AgentRequest) -> None:
        self._latest_requests[request.base_session_id] = request

    def get_latest_request(self, base_session_id: str) -> Optional[AgentRequest]:
        return self._latest_requests.get(base_session_id)

    def begin_turn_start(self, request: AgentRequest, thread_id: str) -> None:
        self.remember_request(request)
        self._pending_turn_starts[request.base_session_id] = CodexPendingTurnStart(
            request=request,
            thread_id=thread_id,
        )

    def register_turn(self, turn_id: str, request: AgentRequest) -> CodexTurnState | None:
        if not turn_id:
            return None

        self.remember_request(request)
        state = self._turns.get(turn_id)
        if state is None:
            state = CodexTurnState(turn_id=turn_id, request=request)
            self._turns[turn_id] = state
        else:
            state.request = request
            state.visible_to_user = True

        self._active_turns[request.base_session_id] = turn_id
        pending = self._pending_turn_starts.get(request.base_session_id)
        if pending and pending.request is request:
            self._pending_turn_starts.pop(request.base_session_id, None)
        return state

    def finalize_turn_start_response(self, turn_id: str, request: AgentRequest) -> CodexTurnState | None:
        pending = self._pending_turn_starts.get(request.base_session_id)
        if pending and pending.request is request:
            if pending.turn_id and pending.turn_id != turn_id:
                self.pop_turn(pending.turn_id)
                self._pending_turn_starts.pop(request.base_session_id, None)
                return self.register_turn(turn_id, request)
            if pending.completed and pending.turn_id == turn_id:
                self._pending_turn_starts.pop(request.base_session_id, None)
                return None
        return self.register_turn(turn_id, request)

    def bootstrap_turn(self, turn_id: str, base_session_id: str, thread_id: str) -> CodexTurnState | None:
        pending = self._pending_turn_starts.get(base_session_id)
        if not pending or not turn_id:
            return None
        if pending.thread_id != thread_id:
            return None
        if pending.turn_id and pending.turn_id != turn_id:
            return None

        pending.turn_id = turn_id
        state = self._turns.get(turn_id)
        if state is None:
            state = CodexTurnState(turn_id=turn_id, request=pending.request)
            self._turns[turn_id] = state
        else:
            state.request = pending.request
            state.visible_to_user = True

        self._active_turns[base_session_id] = turn_id
        return state

    def get_bootstrapped_turn_id(self, base_session_id: str, request: AgentRequest) -> Optional[str]:
        pending = self._pending_turn_starts.get(base_session_id)
        if not pending or pending.request is not request:
            return None
        return pending.turn_id

    def clear_pending_turn_start(self, base_session_id: str, request: AgentRequest | None = None) -> None:
        pending = self._pending_turn_starts.get(base_session_id)
        if not pending:
            return
        if request is not None and pending.request is not request:
            return
        self._pending_turn_starts.pop(base_session_id, None)

    def has_pending_turn_start(self, base_session_id: str) -> bool:
        return base_session_id in self._pending_turn_starts

    def get_turn(self, turn_id: str) -> Optional[CodexTurnState]:
        if not turn_id:
            return None
        return self._turns.get(turn_id)

    def pop_turn(self, turn_id: str) -> Optional[CodexTurnState]:
        if not turn_id:
            return None

        state = self._turns.pop(turn_id, None)
        if state and self._active_turns.get(state.request.base_session_id) == turn_id:
            self._active_turns.pop(state.request.base_session_id, None)
        if state:
            pending = self._pending_turn_starts.get(state.request.base_session_id)
            if pending and pending.request is state.request and pending.turn_id == turn_id:
                pending.completed = True
        return state

    def claim_indicator_cleanup(self, turn_id: str) -> Optional[AgentRequest]:
        """Return the turn request to the first indicator-cleanup owner only."""

        state = self.get_turn(turn_id)
        if state is None or state.indicator_cleanup_claimed:
            return None
        state.indicator_cleanup_claimed = True
        return state.request

    def hide_turn(self, turn_id: str) -> Optional[CodexTurnState]:
        state = self.get_turn(turn_id)
        if not state:
            return None

        state.visible_to_user = False
        state.pending_assistant = None
        state.terminal_error = None
        state.terminal_error_notified = False
        if self._active_turns.get(state.request.base_session_id) == turn_id:
            self._active_turns.pop(state.request.base_session_id, None)
        return state

    def get_request_for_turn(self, turn_id: str) -> Optional[AgentRequest]:
        state = self.get_turn(turn_id)
        return state.request if state else None

    def get_active_turn(self, base_session_id: str) -> Optional[str]:
        return self._active_turns.get(base_session_id)

    def is_active_turn(self, turn_id: str) -> bool:
        state = self.get_turn(turn_id)
        if not state:
            return False
        return self._active_turns.get(state.request.base_session_id) == turn_id

    def clear_active_turn(self, base_session_id: str) -> None:
        self._active_turns.pop(base_session_id, None)

    def should_emit_progress(self, turn_id: str) -> bool:
        state = self.get_turn(turn_id)
        return bool(state and state.visible_to_user and self.is_active_turn(turn_id))

    def should_emit_terminal_error(self, turn_id: str) -> bool:
        state = self.get_turn(turn_id)
        return bool(state and state.visible_to_user and self.is_active_turn(turn_id))

    def should_emit_result(self, turn_id: str) -> bool:
        state = self.get_turn(turn_id)
        return bool(state and state.visible_to_user and self.is_active_turn(turn_id))

    def clear_session(self, base_session_id: str) -> int:
        self._latest_requests.pop(base_session_id, None)
        self._active_turns.pop(base_session_id, None)
        self._pending_turn_starts.pop(base_session_id, None)

        removed = 0
        for turn_id, state in list(self._turns.items()):
            if state.request.base_session_id == base_session_id:
                self._turns.pop(turn_id, None)
                removed += 1
        return removed
