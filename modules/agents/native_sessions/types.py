from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


AgentName = Literal["opencode", "claude", "codex"]
AgentPrefix = Literal["oc", "cc", "cx"]


@dataclass(slots=True)
class NativeResumeSession:
    agent: AgentName
    agent_prefix: AgentPrefix
    native_session_id: str
    working_path: str
    created_at: datetime | None
    updated_at: datetime | None
    sort_ts: float
    last_agent_message: str = ""
    last_agent_tail: str = ""
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendSessionTitle:
    title: str
    source: Literal["backend", "derived_first_prompt"]
    confidence: Literal["high", "low"]
