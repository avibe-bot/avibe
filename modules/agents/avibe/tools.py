"""The Avibe Agent's tools: Pi's coding tools over a Watch-backed job host (C-7).

``ToolSuite`` is the adapter's whole dependency on ``core.agent_core.tools``:
the job host, the tools built over the loop's tracking wrapper (``Agent.jobs``,
so an abort kills foreground commands), the bash renderer recovery uses for open
calls (``recovery.md`` T2), and the lookup from a tool call to its job.

Handover goes through ``WatchJobs``: adopt-or-create the once Watch with target
kind ``job`` for a job's ``meta.json``, keyed by ``job_id`` (J6). Raising means no
Watch owns the job; ``bash`` then keeps the command in the foreground.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from core.agent_core.agent.recovery import RecoveryRenderer
from core.agent_core.tools.base import JobHost, Tool

ImageSink = Callable[[bytes, str, str], Awaitable[str]]


class WatchJobs(Protocol):
    async def adopt_or_create(self, meta: Mapping[str, Any]) -> str:
        """Return the id of the one Watch that owns this job, creating it if none exists."""
        ...


class WatchJobsUnavailable(RuntimeError):
    """No Watch can own a job in this installation yet."""


@dataclass(frozen=True)
class ToolSuite:
    jobs: JobHost
    create_tools: Callable[[JobHost, Optional[ImageSink]], Sequence[Tool]]
    render_recovered: RecoveryRenderer
    find_job: Callable[[str, str], Optional[str]]


def hand_over_callback(watches: Optional[WatchJobs]) -> Callable[[Mapping[str, Any]], Awaitable[str]]:
    async def hand_over(meta: Mapping[str, Any]) -> str:
        if watches is None:
            raise WatchJobsUnavailable("Watch target kind 'job' is not available")
        return await watches.adopt_or_create(meta)

    return hand_over


def local_tool_suite(jobs_dir: str, *, watches: Optional[WatchJobs] = None) -> ToolSuite:
    """Pi's ``read``, ``write``, ``edit``, and ``bash`` over the ``pipe`` job host."""
    from core.agent_core.tools.bash import final_result, handover_result
    from core.agent_core.tools.coding import create_coding_tools
    from core.agent_core.tools.jobs import LocalJobHost
    from core.agent_core.tools.output import JobOutput

    host = LocalJobHost(jobs_dir, on_hand_over=hand_over_callback(watches))

    def create_tools(jobs: JobHost, image_sink: Optional[ImageSink]) -> Sequence[Tool]:
        return create_coding_tools(jobs, image_sink=image_sink)

    def render(call: Any, job_id: str, status: Any, watch_id: Optional[str]) -> Any:
        output = JobOutput(host, job_id)
        return handover_result(output, watch_id) if watch_id else final_result(output, status)

    return ToolSuite(jobs=host, create_tools=create_tools, render_recovered=render, find_job=host.find_job)
