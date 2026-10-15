"""Explicit, credential-free full-runtime trial configuration."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import os
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Profile:
    model: str
    base_url: str
    credential_env: str = "DEEPSEEK_API_KEY"
    protocol: str = "openai_chat"
    reasoning_effort: str = "high"
    context_window: int = 128000
    max_output_tokens: int = 32000
    wall_seconds: float = 1200
    setup_seconds: float = 600
    cleanup_seconds: float = 30
    poll_seconds: float = 1
    offline: bool = False

    def __post_init__(self):
        if type(self.offline) is not bool:
            raise ValueError("offline must be boolean")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("base_url must be an HTTP(S) API root")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("plaintext HTTP is restricted to loopback fixtures")
        if self.protocol not in {"openai_chat", "openai_responses", "anthropic"}:
            raise ValueError("unsupported protocol")
        if not self.model or not self.reasoning_effort:
            raise ValueError("explicit model and effort required")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.credential_env):
            raise ValueError("credential_env must be an environment variable name")
        for value in (self.context_window, self.max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("token limits must be positive integers")
        if self.max_output_tokens > self.context_window // 4:
            raise ValueError("output limit exceeds Vibey's quarter-window policy")
        for value in (self.wall_seconds, self.setup_seconds, self.cleanup_seconds, self.poll_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("time limits must be finite and positive")

    def payload(self):
        return asdict(self)

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict) or set(payload) - {f.name for f in fields(cls)}:
            raise ValueError("unknown profile fields")
        return cls(**payload)


def validate_layout(workdir: Path, state: Path, output: Path) -> None:
    paths = [p.resolve() for p in (workdir, state, output)]
    if not paths[0].is_dir():
        raise ValueError("task workdir must exist")
    if any(p == Path("/") for p in paths):
        raise ValueError("filesystem root is not a trial directory")
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError("task, runtime state, and exported artifacts must be disjoint")
    # Reject production roots even if a caller forgets the container-only gate.
    home = Path.home().resolve()
    for path in paths:
        configured = os.environ.get("AVIBE_HOME")
        forbidden_roots = [home / ".avibe", home / ".vibe_remote"]
        if configured:
            forbidden_roots.append(Path(configured).expanduser())
        for forbidden in forbidden_roots:
            forbidden = forbidden.resolve()
            if path == forbidden or forbidden in path.parents or path in forbidden.parents:
                raise ValueError("refusing the current/default Avibe home or its ancestors")
