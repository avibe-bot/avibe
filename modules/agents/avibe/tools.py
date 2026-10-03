"""The Avibe Agent's tools: Pi's coding tools over a Watch-backed job host (C-7).

``ToolSuite`` is the adapter's whole dependency on ``core.agent_core.tools``:
the job host, the tools built over the loop's tracking wrapper (``Agent.jobs``,
so an abort kills foreground commands), the bash renderer recovery uses for open
calls (``recovery.md`` T2), the lookup from a tool call to its job, and job
pruning.

The job host lives in Watch's ``agent_jobs_dir()``, where ``vibe stop`` and the
job-Watch sweep look, and hands a job over through Watch's own
``hand_over_job``: adopt-or-create the once Watch with target kind ``job``,
keyed by ``job_id`` (J6). Raising means no Watch owns the job; ``bash`` then
keeps the command in the foreground.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from core.agent_core.agent.recovery import RecoveryRenderer
from core.agent_core.tools.base import JobHost, Tool

ImageSink = Callable[[bytes, str, str], Awaitable[str]]
#: Whether a job's tool call has a durable ``tool_result`` (the adapter's half of J5).
CallSettled = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class ToolSuite:
    jobs: JobHost
    create_tools: Callable[[JobHost, Optional[ImageSink]], Sequence[Tool]]
    render_recovered: RecoveryRenderer
    # ``find_job(session_id, tool_call_id, *, created_since)``: the newest job the call started.
    find_job: Callable[..., Optional[str]]
    prune: Optional[Callable[[CallSettled], list[str]]] = None


#: The hand-over route for a job (``hand_over_job``'s keywords), from its owning Turn.
HandOverRoute = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def local_tool_suite(jobs_dir: Optional[str] = None, *, route: Optional[HandOverRoute] = None) -> ToolSuite:
    """Pi's ``read``, ``write``, ``edit``, and ``bash`` over the ``pipe`` job host.

    Every path that creates a job Watch (``watch=true``, the foreground-window hand-over,
    and T2 recovery) goes through the host's ``on_hand_over``, which takes the Watch's
    Agent and authorization from ``route``: the job's owning Turn.
    """
    from core.agent_core.tools.bash import final_result, handover_result
    from core.agent_core.tools.coding import create_coding_tools
    from core.agent_core.tools.jobs import LocalJobHost
    from core.agent_core.tools.output import JobOutput
    from core.watches import ManagedWatchStore, agent_jobs_dir

    async def hand_over(meta: Mapping[str, Any]) -> str:
        import core.watches as watches
        from core.vibe_agents import VibeAgentAccessError
        from storage.resource_access_service import ResourceAccessError

        owner = dict(await asyncio.to_thread(route, meta)) if route is not None else {}
        try:
            return await watches.hand_over_job(meta, **owner)
        except (ResourceAccessError, VibeAgentAccessError, ValueError):
            # The Turn's principal may not create this Watch (no Harness edit right, or
            # no access to the Agent): the Watch still owns the job, and its follow-up
            # stays denied.
            fallback = {key: value for key, value in owner.items() if key != "user_context"}
            return await watches.hand_over_job(meta, **fallback, unverifiable_remote=True)

    host = LocalJobHost(str(jobs_dir or agent_jobs_dir()), on_hand_over=hand_over)

    def create_tools(jobs: JobHost, image_sink: Optional[ImageSink]) -> Sequence[Tool]:
        return create_coding_tools(jobs, image_sink=image_sink)

    async def render(call: Any, job_id: str, status: Any, watch_id: Optional[str]) -> Any:
        output = JobOutput(host, job_id)
        return await handover_result(output, watch_id) if watch_id else await final_result(output, status)

    def prune(call_settled: CallSettled) -> list[str]:
        # J5: a job's files stay until its call has a durable result and no Watch still manages it.
        watches = ManagedWatchStore()
        return host.prune(lambda meta: call_settled(meta) and watches.job_watch_settled(str(meta.get("job_id"))))

    return ToolSuite(
        jobs=host, create_tools=create_tools, render_recovered=render, find_job=host.find_job, prune=prune
    )
