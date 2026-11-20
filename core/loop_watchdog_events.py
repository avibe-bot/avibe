"""Bounded watchdog event vocabulary and an opt-in, offline export boundary.

``parse_watchdog_event(line)`` accepts one application-logger-formatted warning
and returns only a validated v1 stall event, or None. It reads no logs and does
not discover logs or start observation; to accept an Avibe-owned label it consults
the exact vocabulary of the installed avibe-os build (RECORD-listed, hash-checked
source, parsed once and cached). Legacy warnings/recoveries and slow-GC
numeric lines remain separate; callers can keep their existing numeric parser.
"""
from __future__ import annotations

import ast
import base64
import functools
import hashlib
import json
import math
import re
from importlib import metadata
from pathlib import Path

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
    ("core.session_turns", ("SessionTurnManager._resume_linked_control_successor",)),
    ("core.agent_core.tools.jobs", ("LocalJobHost.start", "LocalJobHost._spawn", "_open_for_reading", "LocalJobHost.wait", "LocalJobHost.enforce_deadline", "LocalJobHost.meta", "LocalJobHost.status", "LocalJobHost.output")),
    ("core.agent_core.agent.jobs", ("TrackingJobHost.wait", "TrackingJobHost.status")),
    ("aiohttp.compression_utils", ("DecompressionBaseHandler.decompress", "ConcatDecompressionHandler._decompress_members", "ZLibDecompressor.decompress_sync", "BrotliDecompressor.decompress_sync", "ZSTDDecompressor.decompress_sync")),
    ("aiohttp.http_parser", ("DeflateBuffer.feed_data",)),
)
SAFE_CODE_LABELS = frozenset(
    (module.replace(".", "/") + ".py", attribute)
    for module, attributes in _CODE_TARGETS for attribute in attributes
)
REDACTED_LABEL = ("<redacted>", "<redacted>")
# Avibe's own packages. A label for their code is (path from the package root,
# qualified name). The vocabulary is exact and comes from the build: only files
# listed in the installed avibe-os distribution's RECORD whose bytes still match
# the recorded hash, and only names those files define (by AST, with the line the
# code starts on). A source checkout or an untracked/modified file has no
# vocabulary, so its frames stay redacted.
OWN_PACKAGES = ("vibe", "config", "core", "modules", "storage")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_OWN_FILE = re.compile(r"(?:vibe|config|core|modules|storage)(?:/[a-z_][a-z0-9_]{0,63}){0,6}\.py")
_COMPREHENSIONS = {ast.GeneratorExp: "<genexpr>", ast.ListComp: "<listcomp>", ast.SetComp: "<setcomp>", ast.DictComp: "<dictcomp>"}


@functools.lru_cache(maxsize=1)
def _shipped_hashes() -> dict[str, str]:
    """RECORD sha256 of each own-package source file of the distribution installed here."""
    try:
        dist = metadata.distribution("avibe-os")
        if Path(dist.locate_file("")).resolve() != PACKAGE_ROOT:
            return {}
        return {
            str(entry): entry.hash.value
            for entry in dist.files or ()
            if entry.hash is not None and entry.hash.mode == "sha256" and _OWN_FILE.fullmatch(str(entry))
        }
    except Exception:
        return {}


@functools.lru_cache(maxsize=512)
def shipped_vocabulary(path: str) -> dict[str, frozenset[int]]:
    """Qualified names defined in shipped source ``path``, each with its possible first lines."""
    expected = _shipped_hashes().get(path) if type(path) is str else None
    if expected is None:
        return {}
    try:
        data = (PACKAGE_ROOT / path).read_bytes()
        if base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode() != expected:
            return {}
        tree = ast.parse(data)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return {}
    names: dict[str, set[int]] = {}

    def add(qualname: str, node: ast.AST) -> None:
        lines = names.setdefault(qualname, set())
        lines.add(node.lineno)
        for decorator in getattr(node, "decorator_list", ()):
            lines.add(decorator.lineno)

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(prefix + child.name, child)
                visit(child, f"{prefix}{child.name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.Lambda):
                add(prefix + "<lambda>", child)
                visit(child, f"{prefix}<lambda>.<locals>.")
            elif type(child) in _COMPREHENSIONS:
                add(prefix + _COMPREHENSIONS[type(child)], child)
                visit(child, f"{prefix}{_COMPREHENSIONS[type(child)]}.<locals>.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return {name: frozenset(lines) for name, lines in names.items()}


def own_code_label(file: object, function: object, first_line: int | None = None) -> bool:
    lines = shipped_vocabulary(file).get(function) if type(function) is str else None
    return lines is not None and (first_line is None or first_line in lines)


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
    "sampled_at_unix", "gc_active", "watchdog_wake_lateness_ms", "sample_status", "frames", "truncated",
))


def _integer(value: object, maximum: int = 2**64 - 1) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _nonnegative_number(value: object) -> bool:
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
    ``sampled_at_unix`` can be joined to slow-GC end times; heartbeat lag is
    observed at the preceding wake, not recomputed at this sample timestamp.
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
        if not all(_nonnegative_number(event[key]) for key in ("lag_ms", "watchdog_wake_lateness_ms", "sampled_at_unix")):
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
            if label != REDACTED_LABEL and label not in SAFE_CODE_LABELS and not own_code_label(*label):
                return None
            if not _integer(frame["line"], 2**31 - 1):
                return None
        return event
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
