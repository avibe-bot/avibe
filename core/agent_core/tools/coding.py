"""The coding tool set: Pi's ``read``, ``write``, ``edit``, and ``bash`` with the Avibe additions (C-7)."""

from __future__ import annotations

from typing import Optional

from core.agent_core.tools.base import JobHost, Tool
from core.agent_core.tools.bash import DEFAULT_FOREGROUND_WINDOW_S, BashTool
from core.agent_core.tools.edit import EditTool
from core.agent_core.tools.read import ImageSink, ReadTool
from core.agent_core.tools.write import WriteTool


def create_coding_tools(
    jobs: JobHost,
    *,
    image_sink: Optional[ImageSink] = None,
    foreground_window_s: float = DEFAULT_FOREGROUND_WINDOW_S,
) -> tuple[Tool, ...]:
    """One tool set for every model; each tool's ``spec`` is what the model sees.

    ``jobs`` is the loop's ``Agent.jobs`` wrapper, so an abort can kill a
    foreground command; ``image_sink`` stores images ``read`` attaches.
    """
    return (
        ReadTool(image_sink=image_sink),
        WriteTool(),
        EditTool(),
        BashTool(jobs, foreground_window_s=foreground_window_s),
    )
