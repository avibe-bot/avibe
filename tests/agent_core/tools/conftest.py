"""Hermetic context for the coding tools: temp directories, a minimal environment, no live service."""

from __future__ import annotations

import os
from typing import Callable, Optional

import pytest

from core.agent_core.cancel import CancelToken
from core.agent_core.tools.base import ToolContext


@pytest.fixture
def make_ctx(tmp_path) -> Callable[..., ToolContext]:
    def make(
        *,
        cwd: Optional[str] = None,
        tool_call_id: str = "toolu_1",
        cancel: Optional[CancelToken] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> ToolContext:
        return ToolContext(
            session_id="ses_test",
            tool_call_id=tool_call_id,
            cwd=cwd or str(tmp_path),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cancel=cancel or CancelToken(),
            on_progress=on_progress,
        )

    return make


def result_text(result) -> str:
    return "".join(block.text for block in result.content if getattr(block, "text", None) is not None)
