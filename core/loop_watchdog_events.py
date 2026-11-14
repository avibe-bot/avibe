"""Bounded watchdog event vocabulary and an opt-in, offline export boundary.

``parse_watchdog_event(line)`` accepts one application-logger-formatted warning
and returns only a validated v1 stall event, or None. It opens no files and does
not discover logs or start observation. Legacy warnings/recoveries and slow-GC
numeric lines remain separate; callers can keep their existing numeric parser.
"""
from __future__ import annotations

import json
import math
import re

# Fixed labels, NOT strings obtained from a frame, function, module or filename.
# Resolution is limited to these attributes of already-loaded modules. Missing
# entries may be retried at a stall, never by importing or scanning modules.
_CODE_TARGETS = (
    ("asyncio.base_events", ("BaseEventLoop._run_once",)),
    ("asyncio.events", ("Handle._run",)),
    ("selectors", ("SelectSelector.select", "_PollLikeSelector.select", "EpollSelector.select", "KqueueSelector.select")),
    ("threading", ("Condition.wait", "Event.wait", "Thread.join")),
    ("copy", ("deepcopy", "_deepcopy_dict", "_deepcopy_list", "_deepcopy_tuple", "_reconstruct")),
    ("subprocess", ("run", "Popen.wait", "Popen._wait", "Popen.communicate", "Popen._communicate")),
    ("concurrent.futures._base", ("Future.result",)),
    ("pathlib", ("Path.open",)),
    ("_pyio", ("open",)),
    ("core.agent_core.harness.projection", ("context_view", "_borrowed_context_view", "project")),
    ("core.agent_core.agent.loop", ("Agent._request", "Agent._built", "Agent._prepare_conversation")),
    ("storage.sessions_service", ("SQLiteSessionsService.load_state",)),
    ("vibe.runtime", ("current_process_owns_service_instance",)),
    ("core.agent_core.tools.jobs", ("_open_for_reading", "LocalJobHost.wait", "LocalJobHost.enforce_deadline", "LocalJobHost.meta", "LocalJobHost.status", "LocalJobHost.output")),
    ("core.agent_core.agent.jobs", ("TrackingJobHost.wait", "TrackingJobHost.status")),
    ("aiohttp.compression_utils", ("DecompressionBaseHandler.decompress", "ConcatDecompressionHandler._decompress_members", "ZLibDecompressor.decompress_sync", "BrotliDecompressor.decompress_sync", "ZSTDDecompressor.decompress_sync")),
    ("aiohttp.http_parser", ("DeflateBuffer.feed_data",)),
)
SAFE_CODE_LABELS = frozenset(
    (module.replace(".", "/") + ".py", attribute)
    for module, attributes in _CODE_TARGETS for attribute in attributes
)
REDACTED_LABEL = ("<redacted>", "<redacted>")
MAX_STACK_FRAMES = 30
MAX_EVENT_BYTES = 8192
EVENT_MARKER = "; loop_stall_v1="
# Match the real application envelope, not a substring in an arbitrary log body.
_LOG_LINE = re.compile(
    r"\A\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - core\.loop_watchdog - WARNING - "
    r"\[loop_watchdog\.py:\d{1,6}\] - _report_stall\(\) - "
    r"Event loop has not answered for \d{1,12}\.\ds; loop_stall_v1=(.+)\Z"
)
_EVENT_KEYS = frozenset((
    "version", "kind", "loop_id", "loop_thread_id", "watchdog_thread_id", "lag_ms",
    "gc_active", "watchdog_wake_lateness_ms", "sample_status", "frames", "truncated",
))


def _integer(value: object, maximum: int = 2**64 - 1) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _duration(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e15


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate event key")
        result[key] = value
    return result


def parse_watchdog_event(line: str) -> dict | None:
    """Export only a bounded, allowlisted structured stall; reject all else.

    Frames are innermost-first. Redacted frames still count toward the depth
    limit; ``truncated`` means the stack is incomplete. GC-active and watchdog
    wake lateness are contemporaneous signals, not claims about stall causality.
    No raw line, logger envelope, exception text or unknown field is returned.
    """
    if not isinstance(line, str) or len(line) > MAX_EVENT_BYTES + 512:
        return None
    match = _LOG_LINE.fullmatch(line.removesuffix("\n"))
    if match is None:
        return None
    payload = match[1]
    if len(payload.encode("utf-8", errors="replace")) > MAX_EVENT_BYTES:
        return None
    try:
        event = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(event, dict) or event.keys() != _EVENT_KEYS:
            return None
        if type(event["version"]) is not int or event["version"] != 1 or event["kind"] != "loop_stall":
            return None
        if not all(_integer(event[key]) for key in ("loop_id", "watchdog_thread_id")):
            return None
        if event["loop_thread_id"] is not None and not _integer(event["loop_thread_id"]):
            return None
        if not all(_duration(event[key]) for key in ("lag_ms", "watchdog_wake_lateness_ms")):
            return None
        if any(type(event[key]) is not bool for key in ("gc_active", "truncated")):
            return None
        if event["sample_status"] not in ("ok", "missing", "error"):
            return None
        frames = event["frames"]
        if not isinstance(frames, list) or len(frames) > MAX_STACK_FRAMES:
            return None
        if event["sample_status"] != "ok" and frames:
            return None
        for frame in frames:
            if not isinstance(frame, dict) or frame.keys() != {"file", "function", "line"}:
                return None
            label = (frame["file"], frame["function"])
            if label != REDACTED_LABEL and label not in SAFE_CODE_LABELS:
                return None
            if not _integer(frame["line"], 2**31 - 1):
                return None
        return event
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
