"""Bounded tail of a job's output, with Pi's ``bash`` truncation notices (C-7 section 5).

``OutputAccumulator`` is ported from Pi
``packages/coding-agent/src/core/tools/output-accumulator.ts`` and the notices
from ``bash.ts`` (MIT, Copyright (c) 2025 Mario Zechner). Pi spills to a temp
file once output passes the caps; a job's ``output.log`` already is the full
output, so only the rolling tail and the counters remain.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from core.agent_core.tools.base import MAX_BYTES, MAX_LINES, JobHost
from core.agent_core.tools import jobs as job_host
from core.agent_core.tools.normalize import OutputNormalizer
from core.agent_core.tools.truncate import TruncationResult, format_size, truncate_tail, utf8_len


#: Raw output one progress poll reads; the next poll continues where it stopped.
POLL_BYTES = 1024 * 1024
#: Raw output a final read takes: everything the log keeps on disk, and one more chunk.
FINISH_BYTES = job_host.OUTPUT_HEAD_BYTES + job_host.OUTPUT_TAIL_BYTES + job_host.OUTPUT_CHUNK_BYTES


class OutputAccumulator:
    """Counts every line and byte of normalized output and keeps a tail of at most ``4 * max_bytes``."""

    def __init__(self, *, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> None:
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._max_rolling = max(max_bytes * 2, 1)
        self._tail = ""
        self._tail_bytes = 0
        self._tail_starts_at_line_boundary = True
        self._total_bytes = 0
        self._completed_lines = 0
        self._has_open_line = False

    def append(self, text: str) -> None:
        if not text:
            return
        size = utf8_len(text)
        self._total_bytes += size
        self._tail += text
        self._tail_bytes += size
        if self._tail_bytes > self._max_rolling * 2:
            self._trim()
        newlines = text.count("\n")
        if newlines == 0:
            self._has_open_line = True
            return
        self._completed_lines += newlines
        self._has_open_line = not text.endswith("\n")

    def snapshot(self, open_line: str = "") -> TruncationResult:
        """The tail as the model would see it, with ``open_line`` (not yet final) appended."""
        extra = utf8_len(open_line)
        total_bytes = self._total_bytes + extra
        total_lines = self._completed_lines + (1 if self._has_open_line or open_line else 0)
        tail = truncate_tail(self._snapshot_text() + open_line, max_lines=self._max_lines, max_bytes=self._max_bytes)
        truncated = total_lines > self._max_lines or total_bytes > self._max_bytes
        truncated_by = None
        if truncated:
            truncated_by = tail.truncated_by or ("bytes" if total_bytes > self._max_bytes else "lines")
        return replace(
            tail, truncated=truncated, truncated_by=truncated_by, total_lines=total_lines, total_bytes=total_bytes
        )

    def _trim(self) -> None:
        data = self._tail.encode("utf-8", "surrogatepass")
        if len(data) <= self._max_rolling:
            self._tail_bytes = len(data)
            return
        start = len(data) - self._max_rolling
        while start < len(data) and (data[start] & 0xC0) == 0x80:
            start += 1
        if start:
            self._tail_starts_at_line_boundary = data[start - 1] == 0x0A
        self._tail = data[start:].decode("utf-8", "replace")
        self._tail_bytes = utf8_len(self._tail)

    def _snapshot_text(self) -> str:
        if self._tail_starts_at_line_boundary:
            return self._tail
        newline = self._tail.find("\n")
        return self._tail if newline == -1 else self._tail[newline + 1 :]


class JobOutput:
    """Follows a job's raw output through the normalizer into an accumulator."""

    def __init__(self, jobs: JobHost, job_id: str) -> None:
        self._jobs = jobs
        self._job_id = job_id
        self._offset = 0
        self._normalizer = OutputNormalizer()
        self._accumulator = OutputAccumulator()
        self._finished = False
        self._omitted = 0

    @property
    def path(self) -> str:
        return self._jobs.output_path(self._job_id)

    def poll(self, max_bytes: int = POLL_BYTES) -> bool:
        """Read what was written since the last call, at most ``max_bytes`` of it; ``True`` if anything was new.

        The budget bounds one call however fast the job writes (a reader that falls behind the kept
        tail jumps forward, J4). Callers run it off the event loop, which every Session shares.
        """
        changed = False
        budget = max_bytes
        while budget > 0:
            data, offset = self._jobs.output(self._job_id, self._offset)
            skipped = offset - len(data) - self._offset
            self._offset = offset
            if skipped > 0:
                # The host dropped this part of a long output from disk.
                self._omitted += skipped
                self._accumulator.append(self._normalizer.feed(f"\n[... {skipped} bytes omitted ...]\n".encode()))
            if not data:
                return changed or skipped > 0
            changed = True
            budget -= len(data)
            self._accumulator.append(self._normalizer.feed(data))
        return True

    def finish(self) -> None:
        """Read the rest and end the stream; call once the job is no longer running.

        "The rest" is bounded by what the log keeps on disk: a background child still writing after the
        shell exited (ledger 11) cannot keep this reading forever.
        """
        if self._finished:
            return
        self.poll(FINISH_BYTES)
        self._accumulator.append(self._normalizer.flush())
        self._finished = True

    def snapshot(self) -> TruncationResult:
        return self._accumulator.snapshot("" if self._finished else self._normalizer.peek())

    def render(self, empty_text: str) -> tuple[str, Optional[TruncationResult]]:
        """Pi's ``formatOutput``: the tail (or ``empty_text``) and, when truncated, the notice naming the output log.

        The truncation is returned whenever a notice was added, which a log bounded on disk always gets.
        """
        truncation = self.snapshot()
        text = truncation.content or empty_text
        if not truncation.truncated and not self._omitted:
            return text, None
        where = self.where()
        start = truncation.total_lines - truncation.output_lines + 1
        end = truncation.total_lines
        if not truncation.truncated:
            notice = f"[{where}]"
        elif truncation.last_line_partial:
            # Avibe fix to Pi, whose count drops to 0 once the line ends: the normalizer knows the whole
            # line's size, including what it dropped from the start of a line too long to keep.
            line_size = format_size(self._normalizer.last_line_bytes())
            notice = (
                f"[Showing last {format_size(truncation.output_bytes)} of line {end} (line is {line_size}). {where}]"
            )
        elif truncation.truncated_by == "lines":
            notice = f"[Showing lines {start}-{end} of {truncation.total_lines}. {where}]"
        else:
            notice = (
                f"[Showing lines {start}-{end} of {truncation.total_lines} ({format_size(MAX_BYTES)} limit). {where}]"
            )
        if self._omitted:
            # Only LocalJobHost drops output: the middle, between output.log and tail.log.
            notice += f"\n[{format_size(self._omitted)} between output.log and tail.log were omitted.]"
        return f"{text}\n\n{notice}", truncation

    def where(self) -> str:
        """Where the log is, in one wording for every state (C-7 section 5).

        ``output.log`` keeps the first ``OUTPUT_HEAD_BYTES``; past that, ``tail.log`` beside it keeps the
        latest ``OUTPUT_TAIL_BYTES``. A job can keep appending after its result was built (a handed-over
        job, a background child holding the pipe), so the label states that policy rather than the
        log's current state: it can never become untrue.
        """
        head, tail = format_size(job_host.OUTPUT_HEAD_BYTES), format_size(job_host.OUTPUT_TAIL_BYTES)
        return f"Output log (first {head}, then the last {tail} in tail.log beside it): {self.path}"

    def details(self, truncation: Optional[TruncationResult]) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": self._job_id, "output_path": self.path}
        if self._omitted:
            out["omitted_bytes"] = self._omitted
        if truncation is not None:
            out["truncation"] = truncation.to_details()
        return out
