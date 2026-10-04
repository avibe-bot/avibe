"""Small hermetic C-2/C-5/C-7 fakes shared by agent-core lanes.

The provider records detached requests before running each script. The store
implements only committed entries and fork ancestry, not loop/projection logic.
Jobs have explicit test-controlled state and never spawn processes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from typing import AsyncIterator, Callable, Mapping, Optional, Sequence

from core.agent_core.agent.hooks import AgentInput, Snapshot
from core.agent_core.agent.models import ModelSelection
from core.agent_core.ai.provider import ModelCapabilities, ModelEndpoint, ModelRequest, ProviderEvent
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import AssistantMessage, Origin, ToolResultMessage, UserMessage, text
from core.agent_core.tools.base import JobStatus, ToolContext, ToolResult, ToolSpec

ORIGIN = Origin("test-provider", "anthropic", "test-model")
ENDPOINT = ModelEndpoint("anthropic", "http://model.invalid", "test-model", "", provider="test-provider")
SELECTION = ModelSelection(
    ENDPOINT, ModelCapabilities(context_window=32000, max_output_tokens=4096, supports_tools=True, supports_images=True)
)
Script = Sequence[ProviderEvent] | Callable[[ModelRequest, CancelToken], AsyncIterator[ProviderEvent]]


def user(value: str) -> UserMessage:
    return UserMessage((text(value),))


def input_row(row_id: str, value: str) -> AgentInput:
    return AgentInput(row_id, user(value))


def assistant(value: str = "done", *, calls=(), stop_reason=None) -> AssistantMessage:
    content = ((text(value),) if value else ()) + tuple(calls)
    return AssistantMessage(content, ORIGIN, stop_reason or ("tool_use" if calls else "stop"))


class ScriptedProvider:
    protocol = "anthropic"

    def __init__(self, scripts: Sequence[Script]) -> None:
        self.scripts = deque(scripts)
        self.requests: list[ModelRequest] = []
        self.closed_streams = 0

    async def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]:
        self.requests.append(deepcopy(request))
        if not self.scripts:
            raise AssertionError("the loop made an unscripted provider call")
        script = self.scripts.popleft()
        try:
            if callable(script):
                async for event in script(request, cancel):
                    yield event
            else:
                for event in script:
                    yield deepcopy(event)
        finally:
            self.closed_streams += 1


class FakeModelRouter:
    def __init__(self, provider: ScriptedProvider, selections: Sequence[ModelSelection] = (SELECTION,)) -> None:
        self.provider = provider
        self.selections = tuple(selections)
        self.resolutions = 0
        self.protocols: list[str] = []

    async def resolve(self) -> ModelSelection:
        index = min(self.resolutions, len(self.selections) - 1)
        self.resolutions += 1
        return self.selections[index]

    def provider_for(self, protocol):
        self.protocols.append(protocol)
        return self.provider


class InMemoryTranscriptStore:
    def __init__(self, *, clock: Optional[Callable[[], float]] = None) -> None:
        self.rows: dict[str, list[ContextEntry]] = {}
        self.forks: dict[str, Snapshot] = {}
        self.final: dict[str, bool] = {}
        # C-9 checkpoint-turn audit rows: (session_id, row id, payload), never context.
        self.audits: list[tuple[str, str, dict]] = []
        self._clock = clock
        self._next_id = 0

    def fork(self, snapshot: Snapshot, *, session_id: str) -> None:
        if session_id in self.rows or session_id in self.forks:
            raise ValueError("child already exists")
        self.forks[session_id] = deepcopy(snapshot)

    async def load(self, session_id: str) -> Sequence[ContextEntry]:
        prefix = []
        anchor = self.forks.get(session_id)
        if anchor is not None:
            prefix = [row for row in await self.load(anchor.session_id) if row.context_seq <= anchor.context_seq]
        return deepcopy(prefix + self.rows.get(session_id, []))

    async def _append(self, session_id, kind, *, message=None, payload=None, row_id=None) -> ContextEntry:
        entries = await self.load(session_id)
        seq = max((entry.context_seq for entry in entries), default=0) + 1
        if row_id is None:
            self._next_id += 1
            row_id = f"row_{self._next_id}"
        if any(entry.row_id == row_id for entry in entries):
            raise ValueError(f"input already consumed: {row_id}")
        created_at = self._clock() if self._clock is not None else None
        row = ContextEntry(session_id, seq, kind, row_id, deepcopy(message), deepcopy(payload or {}), created_at)
        self.rows.setdefault(session_id, []).append(row)
        return deepcopy(row)

    async def consume_input(self, session_id, message_id, message: UserMessage) -> ContextEntry:
        return await self._append(session_id, "input", row_id=message_id, message=message)

    async def append_response(self, session_id, message: AssistantMessage, *, final: bool) -> ContextEntry:
        row = await self._append(session_id, "response", message=message)
        self.final[row.row_id] = final
        return row

    async def append_tool_result(self, session_id, message: ToolResultMessage, *, details: Mapping) -> ContextEntry:
        return await self._append(session_id, "tool_result", message=message, payload={"details": dict(details)})

    async def append_payload(self, session_id, kind, payload: Mapping) -> ContextEntry:
        return await self._append(session_id, kind, payload=payload)

    async def append_checkpoint_turn(self, session_id, payload: Mapping) -> str:
        self._next_id += 1
        row_id = f"audit_{self._next_id}"
        self.audits.append((session_id, row_id, deepcopy(dict(payload))))
        return row_id


class FakeTool:
    def __init__(self, name="echo", execute=None, result: Optional[ToolResult] = None) -> None:
        self.spec = ToolSpec(name, "Test tool", {"type": "object"})
        self.calls: list[tuple[dict, ToolContext]] = []
        self._execute = execute
        self.result = result or ToolResult((text("tool result"),))

    async def execute(self, arguments, ctx: ToolContext) -> ToolResult:
        self.calls.append((deepcopy(dict(arguments)), ctx))
        if self._execute is not None:
            return await self._execute(arguments, ctx)
        return deepcopy(self.result)


class FakeJobHost:
    def __init__(self) -> None:
        self.states: dict[str, JobStatus] = {}
        self.outputs: dict[str, bytes] = {}
        self.starts: list[dict] = []
        self.killed: list[str] = []
        self.stop_reasons: dict[str, str] = {}
        self.watches: dict[str, str] = {}
        self.changed = asyncio.Event()

    async def start(self, command, *, cwd, env, timeout_s, session_id, tool_call_id) -> str:
        job_id = f"job_{len(self.starts) + 1}"
        self.starts.append(
            dict(
                command=command,
                cwd=cwd,
                env=dict(env),
                timeout_s=timeout_s,
                session_id=session_id,
                tool_call_id=tool_call_id,
                job_id=job_id,
            )
        )
        self.states[job_id] = JobStatus("running")
        self.outputs[job_id] = b""
        return job_id

    def status(self, job_id) -> JobStatus:
        return self.states.get(job_id, JobStatus("gone"))

    async def wait(self, job_id, *, deadline_s) -> JobStatus:
        while self.status(job_id).state == "running":
            self.changed.clear()
            await self.changed.wait()
        return self.status(job_id)

    def output(self, job_id, since=0) -> tuple[bytes, int]:
        output = self.outputs[job_id]
        return output[since:], len(output)

    def output_path(self, job_id) -> str:
        return f"/test-owned/jobs/{job_id}/output.log"

    async def kill(self, job_id, *, reason="killed") -> None:
        self.killed.append(job_id)
        self.stop_reasons.setdefault(job_id, reason)  # the first recorded reason wins
        self.states[job_id] = JobStatus("gone")
        self.changed.set()

    def stop_reason(self, job_id):
        return self.stop_reasons.get(job_id)

    async def hand_over(self, job_id) -> str:
        watch_id = f"watch_{job_id}"
        self.watches[job_id] = watch_id
        return watch_id
