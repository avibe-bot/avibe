"""Exact-attribution turn provenance for process-scoped Model Hub traffic."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)

from .adapter import RawCallOutcome
from .classification import ResolutionDecision


BackendName = Literal["claude", "codex", "opencode"]
SupplyChannel = Literal["native_cli", "hub"]
SupplyState = Literal["waiting", "interrupted"]
ScopeKey = tuple[BackendName, str]


@dataclass(frozen=True)
class AttemptIdentity:
    source_id: str
    resolved_model_id: str
    channel: SupplyChannel
    via_mapping: bool

    def payload(self) -> dict:
        return {
            "source_id": self.source_id,
            "resolved_model_id": self.resolved_model_id,
            "channel": self.channel,
            "via_mapping": self.via_mapping,
        }


@dataclass
class TurnTrace:
    turn_id: str
    agent: BackendName
    requested_model_id: str
    scope_key: ScopeKey
    failed_attempts: list[dict] = field(default_factory=list)
    served: Optional[dict] = None
    terminal_error: Optional[dict] = None
    pending_attempt: Optional[AttemptIdentity] = None
    model_supply_state: Optional[SupplyState] = None
    ambiguous: bool = False


@dataclass
class ProcessScope:
    token: str
    active_turns: set[str] = field(default_factory=set)
    ambiguous_turns: set[str] = field(default_factory=set)
    untracked_use: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_reason(decision: ResolutionDecision) -> str:
    code = decision.error_code or ""
    if code == "stream_interrupted":
        return "stream_interrupted"
    if code == "upstream_request_invalid":
        return "invalid_parameter"
    if code == "tool_incompatible":
        return "tool_incompatible"
    return "protocol_error"


class BoundedProvenanceStore:
    """Atomic, bounded persistence for exact turn records."""

    def __init__(self, path: Path, *, max_entries: int = 500):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _write(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            records[-self.max_entries :],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temporary_path = tmp.name
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.path)

    def put(self, record: dict) -> None:
        turn_id = str(record.get("turn_id") or "")
        if not turn_id:
            raise ValueError("turn_id is required")
        with self._lock:
            records = [
                item
                for item in self._read()
                if str(item.get("turn_id") or "") != turn_id
            ]
            records.append(record)
            self._write(records)

    def get(self, turn_id: str) -> Optional[dict]:
        with self._lock:
            return next(
                (
                    dict(item)
                    for item in reversed(self._read())
                    if item.get("turn_id") == turn_id
                ),
                None,
            )


class TurnCorrelationRegistry:
    """Correlate process credentials to the existing Workbench turn token."""

    def __init__(self, store: BoundedProvenanceStore):
        self.store = store
        self._lock = threading.RLock()
        self._scopes: dict[ScopeKey, ProcessScope] = {}
        self._token_scopes: dict[str, ScopeKey] = {}
        self._turn_scopes: dict[str, set[ScopeKey]] = {}
        self._traces: dict[str, TurnTrace] = {}

    @staticmethod
    def _scope_key(backend: str, process_scope: str) -> ScopeKey:
        if backend not in {"claude", "codex", "opencode"}:
            raise ValueError("unsupported backend")
        normalized = str(process_scope or "").strip()
        if not normalized:
            raise ValueError("process scope is required")
        return backend, normalized  # type: ignore[return-value]

    def credentials(
        self,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
    ) -> str:
        key = self._scope_key(backend, process_scope)
        normalized_turn_id = str(turn_id or "").strip() or None
        with self._lock:
            scope = self._scopes.get(key)
            if scope is None:
                scope = ProcessScope(token=secrets.token_urlsafe(32))
                self._scopes[key] = scope
                self._token_scopes[scope.token] = key

            # Frozen v3 has no discriminator for the shared OpenCode server.
            if normalized_turn_id is None or backend == "opencode":
                scope.untracked_use = True
                for active_turn_id in scope.active_turns:
                    trace = self._traces.get(active_turn_id)
                    if trace is not None:
                        trace.ambiguous = True
                return scope.token

            if scope.active_turns - {normalized_turn_id}:
                overlapping = scope.active_turns | {normalized_turn_id}
                scope.ambiguous_turns.update(overlapping)
                for active_turn_id in overlapping:
                    trace = self._traces.get(active_turn_id)
                    if trace is not None:
                        trace.ambiguous = True
            scope.active_turns.add(normalized_turn_id)
            self._turn_scopes.setdefault(normalized_turn_id, set()).add(key)
            return scope.token

    def authenticates(self, backend: str, token: str) -> bool:
        with self._lock:
            return any(
                key[0] == backend and secrets.compare_digest(candidate, token)
                for candidate, key in self._token_scopes.items()
            )

    def _exact_turn(self, backend: str, token: str) -> tuple[str, ScopeKey] | None:
        key = self._token_scopes.get(token)
        if key is None or key[0] != backend:
            return None
        scope = self._scopes[key]
        if scope.untracked_use or len(scope.active_turns) != 1:
            for turn_id in scope.active_turns:
                trace = self._traces.get(turn_id)
                if trace is not None:
                    trace.ambiguous = True
            return None
        turn_id = next(iter(scope.active_turns))
        if turn_id in scope.ambiguous_turns:
            return None
        trace = self._traces.get(turn_id)
        if trace is not None and trace.ambiguous:
            return None
        return turn_id, key

    def begin_gateway_request(
        self,
        *,
        backend: str,
        token: str,
        requested_model_id: str,
    ) -> Optional[str]:
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None:
                return None
            turn_id, key = exact
            self._traces.setdefault(
                turn_id,
                TurnTrace(
                    turn_id=turn_id,
                    agent=key[0],
                    requested_model_id=requested_model_id,
                    scope_key=key,
                ),
            )
            return turn_id

    def begin_native_attempt(
        self,
        *,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
        requested_model_id: str,
        source_id: str,
        resolved_model_id: str,
        via_mapping: bool,
    ) -> None:
        token = self.credentials(backend, process_scope, turn_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None or exact[0] != normalized_turn_id:
                return
            trace = self._traces.setdefault(
                normalized_turn_id,
                TurnTrace(
                    turn_id=normalized_turn_id,
                    agent=exact[1][0],
                    requested_model_id=requested_model_id,
                    scope_key=exact[1],
                ),
            )
            trace.pending_attempt = AttemptIdentity(
                source_id=source_id,
                resolved_model_id=resolved_model_id,
                channel="native_cli",
                via_mapping=via_mapping,
            )

    def mark_no_candidate(
        self,
        *,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
        requested_model_id: str,
        supply_state: SupplyState,
    ) -> None:
        token = self.credentials(backend, process_scope, turn_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None or exact[0] != normalized_turn_id:
                return
            trace = self._traces.setdefault(
                normalized_turn_id,
                TurnTrace(
                    turn_id=normalized_turn_id,
                    agent=exact[1][0],
                    requested_model_id=requested_model_id,
                    scope_key=exact[1],
                ),
            )
            trace.model_supply_state = supply_state

    def mark_gateway_no_candidate(
        self,
        turn_id: Optional[str],
        supply_state: SupplyState,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None:
                trace.model_supply_state = supply_state

    def begin_attempt(
        self,
        turn_id: Optional[str],
        *,
        source_id: str,
        resolved_model_id: str,
        channel: SupplyChannel,
        via_mapping: bool,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None:
                return
            trace.pending_attempt = AttemptIdentity(
                source_id=source_id,
                resolved_model_id=resolved_model_id,
                channel=channel,
                via_mapping=via_mapping,
            )

    def finish_attempt(
        self,
        turn_id: Optional[str],
        *,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.pending_attempt is None:
                return
            identity = trace.pending_attempt
            trace.pending_attempt = None
            if decision.action == "return":
                trace.served = identity.payload()
                trace.terminal_error = None
                return
            if decision.action == "fallback" and decision.reason is not None:
                trace.failed_attempts.append(
                    {**identity.payload(), "reason": decision.reason}
                )
                return
            if decision.action == "surface":
                trace.terminal_error = {
                    **identity.payload(),
                    "reason": _terminal_reason(decision),
                    "stream_started": outcome.stream_started,
                }

    def settle(self, turn_id: str, *, settled_by: Optional[str], ts: Optional[str] = None) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            trace = self._traces.pop(normalized_turn_id, None)
            scope_keys = self._turn_scopes.pop(normalized_turn_id, set())
            poisoned = False
            for key in scope_keys:
                scope = self._scopes.get(key)
                if scope is None:
                    continue
                poisoned = poisoned or scope.untracked_use
                scope.active_turns.discard(normalized_turn_id)
                scope.ambiguous_turns.discard(normalized_turn_id)
            if trace is None or trace.ambiguous or poisoned:
                return

            served = trace.served
            terminal_error = trace.terminal_error
            canceled_attempt = None
            supply_state = None
            if settled_by == SETTLED_BY_STOPPED:
                outcome = "canceled"
                canceled_attempt = (
                    trace.pending_attempt.payload()
                    if trace.pending_attempt is not None
                    else None
                )
                served = None
                terminal_error = None
            elif trace.model_supply_state is not None:
                outcome = "no_candidate"
                served = None
                terminal_error = None
                supply_state = trace.model_supply_state
            elif settled_by in {
                SETTLED_BY_NO_TERMINAL_RESULT,
                SETTLED_BY_BACKEND_REFRESH,
            }:
                interrupted_attempt = (
                    trace.pending_attempt.payload()
                    if trace.pending_attempt is not None
                    else served
                )
                if terminal_error is None and interrupted_attempt is None:
                    return
                outcome = "failed_terminal"
                served = None
                terminal_error = terminal_error or {
                    **interrupted_attempt,
                    "reason": "stream_interrupted",
                    "stream_started": True,
                }
            elif (
                settled_by == SETTLED_BY_TERMINAL_RESULT
                and trace.pending_attempt is not None
                and trace.pending_attempt.channel == "native_cli"
            ):
                outcome = "served"
                served = trace.pending_attempt.payload()
                terminal_error = None
            elif terminal_error is not None:
                outcome = "failed_terminal"
                served = None
            elif served is not None:
                outcome = "served"
            elif trace.failed_attempts:
                outcome = "exhausted"
            else:
                return

            self.store.put(
                {
                    "contract_version": 3,
                    "turn_id": normalized_turn_id,
                    "ts": ts or _utc_now_iso(),
                    "agent": trace.agent,
                    "requested_model_id": trace.requested_model_id,
                    "outcome": outcome,
                    "failed_attempts": list(trace.failed_attempts),
                    "served": served,
                    "terminal_error": terminal_error,
                    "canceled_attempt": canceled_attempt,
                    "model_supply_state": supply_state,
                }
            )
