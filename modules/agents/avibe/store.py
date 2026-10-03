"""The ``TranscriptStore`` the loop writes through, over ``SQLiteTranscriptStore``.

Two adapter rules live at this boundary, where the loop consumes an input:

* the environment block (C-7 tools.md section 8) is rendered into the input as
  it is consumed, from the environment the context last recorded, so what
  ``consume_input`` stores is exactly what the model is sent;
* a steered input's ``messages`` row is inserted by the Delivery manager right
  after the backend accepts the steer. The loop can reach that input first, so
  consumption waits, bounded, for the row to exist.

Each committed response is handed to ``on_response`` once, for the run that
owns it; nothing here keeps responses after that.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import AssistantMessage, ToolResultMessage, UserMessage
from modules.agents.avibe.prompt import environment_delta, environment_state, with_environment
from storage.agent_transcript import SQLiteTranscriptStore
from storage.models import messages

EnvironmentSource = Callable[[str], Mapping[str, str]]
ResponseObserver = Callable[[str, str, AssistantMessage], None]

#: How long consumption waits for an accepted steer's row before failing the run.
INPUT_ROW_TIMEOUT_S = 10.0


class InputRowMissing(RuntimeError):
    """An accepted input's ``messages`` row did not appear in time."""


class AdapterTranscriptStore:
    def __init__(
        self,
        store: SQLiteTranscriptStore,
        engine: Engine,
        *,
        environment: EnvironmentSource,
        on_response: ResponseObserver,
        input_row_timeout_s: float = INPUT_ROW_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._engine = engine
        self._environment = environment
        self._on_response = on_response
        self._input_row_timeout_s = input_row_timeout_s
        # Per Session, the environment the context last recorded; evicted by ``forget``
        # when the adapter retires the Session's runtime.
        self._env_state: dict[str, dict[str, str]] = {}

    def forget(self, session_id: str) -> None:
        """Drop per-Session state; the next consumption reads the environment from the rows again."""
        self._env_state.pop(session_id, None)
        self._store.forget(session_id)

    # --- TranscriptStore -----------------------------------------------------

    async def load(self, session_id: str) -> Sequence[ContextEntry]:
        return await self._store.load(session_id)

    async def consume_input(self, session_id: str, message_id: str, message: UserMessage) -> ContextEntry:
        await self._await_input_row(session_id, message_id)
        previous = self._env_state.get(session_id)
        if previous is None:
            previous = environment_state(await self._store.load(session_id))
        current = dict(self._environment(session_id))
        rendered = with_environment(message, environment_delta(previous, current))
        entry = await self._store.consume_input(session_id, message_id, rendered)
        self._env_state[session_id] = {**previous, **current}
        return entry

    async def append_response(self, session_id: str, message: AssistantMessage, *, final: bool) -> ContextEntry:
        entry = await self._store.append_response(session_id, message, final=final)
        self._on_response(session_id, entry.row_id, message)
        return entry

    async def append_tool_result(
        self, session_id: str, message: ToolResultMessage, *, details: Mapping[str, Any]
    ) -> ContextEntry:
        return await self._store.append_tool_result(session_id, message, details=details)

    async def append_payload(
        self, session_id: str, kind: Literal["compaction", "context_edit", "agent_state"], payload: Mapping[str, Any]
    ) -> ContextEntry:
        return await self._store.append_payload(session_id, kind, payload)

    # --- implementation ------------------------------------------------------

    async def _await_input_row(self, session_id: str, message_id: str) -> None:
        deadline = time.monotonic() + self._input_row_timeout_s
        delay = 0.01
        while not await asyncio.to_thread(self._input_row_exists, session_id, message_id):
            if time.monotonic() >= deadline:
                raise InputRowMissing(f"input {message_id} of Session {session_id} was never materialized")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.25)

    def _input_row_exists(self, session_id: str, message_id: str) -> bool:
        with self._engine.connect() as conn:
            owner = conn.execute(select(messages.c.session_id).where(messages.c.id == message_id)).scalar()
        return owner == session_id
