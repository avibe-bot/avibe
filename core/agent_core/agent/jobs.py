"""Loop ownership of foreground jobs using only the C-7 JobHost protocol."""

from __future__ import annotations

import asyncio
from typing import Mapping, Optional

from core.agent_core.tools.base import JobHost, JobStatus


class ForegroundCleanupError(RuntimeError):
    """Every owned handle was attempted, but some could not be released."""


class TrackingJobHost:
    """Pass this same host to job-backed tools and their owning Agent.

    A successful handover removes a handle from foreground ownership. Shielding
    start/handover lets their ownership transition finish before cancellation
    cleanup decides which jobs it must kill.
    """

    def __init__(self, host: JobHost) -> None:
        self.host = host
        self._foreground: dict[str, tuple[str, str]] = {}

    async def start(
        self,
        command: str,
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: Optional[float],
        session_id: str,
        tool_call_id: str,
    ) -> str:
        task = asyncio.create_task(
            self.host.start(
                command,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
                session_id=session_id,
                tool_call_id=tool_call_id,
            )
        )
        try:
            job_id = await asyncio.shield(task)
        except asyncio.CancelledError:
            job_id = await task
            self._foreground[job_id] = (session_id, tool_call_id)
            raise
        self._foreground[job_id] = (session_id, tool_call_id)
        return job_id

    def status(self, job_id: str) -> JobStatus:
        status = self.host.status(job_id)
        if status.state != "running":
            self._foreground.pop(job_id, None)
        return status

    async def wait(self, job_id: str, *, deadline_s: Optional[float]) -> JobStatus:
        status = await self.host.wait(job_id, deadline_s=deadline_s)
        if status.state != "running":
            self._foreground.pop(job_id, None)
        return status

    def output(self, job_id: str, since: int = 0) -> tuple[bytes, int]:
        return self.host.output(job_id, since)

    def output_path(self, job_id: str) -> str:
        return self.host.output_path(job_id)

    async def kill(self, job_id: str) -> None:
        await self.host.kill(job_id)
        self._foreground.pop(job_id, None)

    async def hand_over(self, job_id: str) -> str:
        task = asyncio.create_task(self.host.hand_over(job_id))
        try:
            watch_id = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            self._foreground.pop(job_id, None)
            raise
        self._foreground.pop(job_id, None)
        return watch_id

    async def kill_foreground(self, session_id: str, *, tool_call_id: Optional[str] = None) -> None:
        failures = []
        for job_id, (owner, call) in list(self._foreground.items()):
            if owner != session_id or (tool_call_id is not None and call != tool_call_id):
                continue
            try:
                if self.status(job_id).state == "running":
                    await self.kill(job_id)
                else:
                    self._foreground.pop(job_id, None)
            except (Exception, asyncio.CancelledError) as error:
                # Keep failed handles owned for the final run sweep, and do
                # not let one failed kill prevent release of the other jobs.
                failures.append(f"{job_id}: {type(error).__name__}: {error}")
        if failures:
            raise ForegroundCleanupError("Failed to release foreground jobs: " + "; ".join(failures))
