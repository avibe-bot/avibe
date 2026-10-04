"""C-7 tool interface and job handle (``agent-core-contracts/tools.md``).

``Tool`` is what the loop executes; ``JobHost`` is how ``bash`` runs commands
through a portable handle so a running command can be handed to Watch without
restarting it. The adapter supplies the Watch-backed ``JobHost``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Protocol

from core.agent_core.cancel import CancelToken
from core.agent_core.messages import UserContent

MAX_LINES = 2000
MAX_BYTES = 50 * 1024


@dataclass(frozen=True)
class ToolSpec:
    """What the model sees: the name, description, and JSON Schema of the parameters."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    tool_call_id: str
    cwd: str
    env: Mapping[str, str]
    cancel: CancelToken
    on_progress: Optional[Callable[[str], None]] = None
    #: A resolved path the caller authorized (C-9's checkpoint turn): ``write`` and ``edit`` publish only
    #: while the path still resolves to it. ``None`` everywhere else, where nothing changes.
    pinned_target: Optional[str] = None


@dataclass(frozen=True)
class ToolResult:
    """``content`` is exactly what the model sees; ``details`` is for display and the transcript only."""

    content: tuple[UserContent, ...]
    is_error: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    terminate: bool = False


class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult: ...


JobState = Literal["running", "exited", "gone"]


@dataclass(frozen=True)
class JobStatus:
    """``exited`` carries the exit code; ``gone`` means the job ended without writing one."""

    state: JobState
    exit_code: Optional[int] = None


class JobHost(Protocol):
    """Portable command handles (tools.md section 7). The v1 backend is ``pipe``."""

    async def start(
        self,
        command: str,
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: Optional[float],
        session_id: str,
        tool_call_id: str,
    ) -> str: ...

    def status(self, job_id: str) -> JobStatus: ...

    async def wait(self, job_id: str, *, deadline_s: Optional[float]) -> JobStatus:
        """Wait until exit, or at most ``deadline_s`` seconds from now (``None``: until exit)."""
        ...

    def output_path(self, job_id: str) -> str:
        """Absolute path of the job's full output (``output.log``), named in truncated and handover results."""
        ...

    def output(self, job_id: str, since: int = 0) -> tuple[bytes, int]:
        """Raw output bytes after ``since`` and the new offset; callers normalize."""
        ...

    async def kill(self, job_id: str, *, reason: str = "killed") -> None:
        """Terminate the job's process tree, recording ``reason`` unless a stop reason was already recorded."""
        ...

    def stop_reason(self, job_id: str) -> Optional[str]:
        """Why the job was stopped (``timeout``, ``aborted``, ``wrapper_error``, ``killed``), or ``None``.

        The first recorded reason wins; callers report a job's end from this, never from elapsed time.
        """
        ...

    async def hand_over(self, job_id: str) -> str:
        """Give the running job to a once Watch and return the Watch id."""
        ...
