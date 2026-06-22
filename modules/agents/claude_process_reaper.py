"""Best-effort cleanup for duplicate Claude Code resume processes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
NODE_EXECUTABLES = {"node", "nodejs", "node.exe"}
AVIBE_CLAUDE_PROCESS_OWNER_ENV = "AVIBE_CLAUDE_PROCESS_OWNER"
AVIBE_CLAUDE_SESSION_OWNER = "session"
AVIBE_CLAUDE_AUTH_OWNER = "auth"
CLAUDE_PROCESS_REGISTRY_NAME = "claude_processes.json"


@dataclass(frozen=True)
class ClaudeProcessRow:
    pid: int
    ppid: int
    command: str


@dataclass(frozen=True)
class ClaudeOwnedProcess:
    pid: int
    native_session_id: str | None = None
    owner: str = AVIBE_CLAUDE_SESSION_OWNER
    started_at: float | None = None


def get_claude_client_pid(client: object | None) -> int | None:
    """Return the SDK-managed Claude CLI pid when the current SDK exposes it."""
    transport = getattr(client, "_transport", None)
    process = getattr(transport, "_process", None)
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def _process_start_time(pid: int) -> float | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text).timestamp()
    except Exception:
        return None


def _process_start_times(pids: set[int]) -> dict[int, float]:
    starts: dict[int, float] = {}
    for pid in pids:
        started_at = _process_start_time(pid)
        if started_at is not None:
            starts[pid] = started_at
    return starts


def _process_identity_matches(
    record: ClaudeOwnedProcess,
    row: ClaudeProcessRow,
    current_started_at: float | None,
) -> bool:
    if record.pid != row.pid:
        return False
    if record.started_at is None:
        return True
    if current_started_at is None:
        return False
    return abs(current_started_at - record.started_at) < 1.0


def _registry_path(runtime_dir: Path | None = None) -> Path:
    if runtime_dir is not None:
        return runtime_dir / CLAUDE_PROCESS_REGISTRY_NAME
    from config import paths

    return paths.get_runtime_dir() / CLAUDE_PROCESS_REGISTRY_NAME


def _load_owned_process_registry(runtime_dir: Path | None = None) -> list[ClaudeOwnedProcess]:
    path = _registry_path(runtime_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("processes") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    records: list[ClaudeOwnedProcess] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        native_session_id = item.get("native_session_id")
        owner = item.get("owner") or AVIBE_CLAUDE_SESSION_OWNER
        started_at = item.get("started_at")
        records.append(
            ClaudeOwnedProcess(
                pid=pid,
                native_session_id=str(native_session_id) if native_session_id else None,
                owner=str(owner),
                started_at=float(started_at) if isinstance(started_at, (int, float)) else None,
            )
        )
    return records


def register_claude_owned_process(
    client: object | None,
    *,
    native_session_id: str | None = None,
    owner: str = AVIBE_CLAUDE_SESSION_OWNER,
    runtime_dir: Path | None = None,
) -> None:
    pid = get_claude_client_pid(client)
    if not pid or os.name == "nt":
        return
    path = _registry_path(runtime_dir)
    records = [record for record in _load_owned_process_registry(runtime_dir) if record.pid != pid]
    records.append(
        ClaudeOwnedProcess(
            pid=pid,
            native_session_id=str(native_session_id) if native_session_id else None,
            owner=owner,
            started_at=_process_start_time(pid),
        )
    )
    payload = {
        "processes": [
            {
                "pid": record.pid,
                "native_session_id": record.native_session_id,
                "owner": record.owner,
                "started_at": record.started_at,
            }
            for record in records
        ]
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        logger = logging.getLogger(__name__)
        logger.debug("Failed to persist Claude process registry", exc_info=True)


def _run_ps() -> str:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
    )
    return result.stdout or ""


def _parse_ps_rows(output: str) -> list[ClaudeProcessRow]:
    rows: list[ClaudeProcessRow] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows.append(ClaudeProcessRow(pid=pid, ppid=ppid, command=parts[2]))
    return rows


def _command_has_flag_value(command: str, flag: str, value: str) -> bool:
    """True if ``command`` passes ``flag value`` or ``flag=value`` (token-exact)."""
    parts = command.split()
    for index, part in enumerate(parts):
        if part == flag and index + 1 < len(parts) and parts[index + 1] == value:
            return True
        if part == f"{flag}={value}":
            return True
    return False


def _command_has_resume(command: str, native_session_id: str) -> bool:
    return _command_has_flag_value(command, "--resume", native_session_id)


def _command_has_stream_json_input(command: str) -> bool:
    """True if the command runs the Claude CLI in bidirectional streaming mode.

    The Claude Agent SDK always launches the session CLI with
    ``--input-format stream-json`` (see the SDK's subprocess transport). This
    marker is public CLI surface; it is only a subprocess-shape check, not an
    ownership proof.
    """
    return _command_has_flag_value(command, "--input-format", "stream-json")


def _command_is_claude(command: str, cli_path: str | None = None) -> bool:
    parts = command.split()
    if not parts:
        return False
    accepted_names = {"claude", "claude.exe"}
    if cli_path:
        configured_name = os.path.basename(cli_path)
        if configured_name:
            accepted_names.add(configured_name)
    executable = os.path.basename(parts[0])
    if executable in accepted_names:
        return True
    if executable in NODE_EXECUTABLES and len(parts) > 1:
        return os.path.basename(parts[1]) in accepted_names
    return False


def find_claude_resume_processes(native_session_id: str, *, cli_path: str | None = None) -> list[ClaudeProcessRow]:
    """Find Claude Code CLI processes for one native ``--resume`` id."""
    if os.name == "nt" or not native_session_id:
        return []
    try:
        rows = _parse_ps_rows(_run_ps())
    except Exception:
        return []
    return [
        row
        for row in rows
        if row.pid != os.getpid()
        and _command_is_claude(row.command, cli_path=cli_path)
        and _command_has_resume(row.command, native_session_id)
    ]


def _build_children_map(rows: list[ClaudeProcessRow]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    return children


def _descendant_pids(
    rows: list[ClaudeProcessRow],
    root_pid: int,
    children: dict[int, list[int]] | None = None,
) -> set[int]:
    if children is None:
        children = _build_children_map(rows)

    descendants: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        stack.extend(children.get(pid, []))
    return descendants


def _runtime_related_pids(rows: list[ClaudeProcessRow], root_pid: int | None) -> set[int]:
    if root_pid is None:
        return set()
    return {root_pid} | _descendant_pids(rows, root_pid)


def _same_runtime_rows(
    rows: list[ClaudeProcessRow],
    matches: list[ClaudeProcessRow],
    *,
    keep_pid: int | None,
) -> list[ClaudeProcessRow]:
    runtime_pids = _runtime_related_pids(rows, os.getpid())
    if keep_pid is not None:
        runtime_pids.update(_runtime_related_pids(rows, keep_pid))
        keep_row = next((row for row in rows if row.pid == keep_pid), None)
        if keep_row is not None:
            runtime_pids.add(keep_row.ppid)
            runtime_pids.update(_runtime_related_pids(rows, keep_row.ppid))
    return [row for row in matches if row.pid in runtime_pids or row.ppid in runtime_pids]


def _signal_pid(pid: int, sig: int, logger: logging.Logger) -> bool:
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        logger.debug("Failed to signal Claude duplicate pid=%s signal=%s", pid, sig, exc_info=True)
        return False


async def reap_duplicate_claude_resume_processes(
    native_session_id: str | None,
    *,
    keep_pid: int | None = None,
    cli_path: str | None = None,
    logger: logging.Logger,
    terminate_timeout: float = 2.0,
) -> int:
    """Terminate duplicate Claude Code CLI processes for one native session.

    This is intentionally conservative: it only matches a full ``--resume`` id
    and only reaps when more than one matching process exists, or when the
    caller no longer has a tracked PID to keep.
    """
    if not native_session_id or os.name == "nt":
        return 0

    try:
        all_rows = _parse_ps_rows(_run_ps())
    except Exception:
        logger.debug("Failed to read process table for Claude duplicate cleanup", exc_info=True)
        return 0

    matches = [
        row
        for row in all_rows
        if row.pid != os.getpid()
        and _command_is_claude(row.command, cli_path=cli_path)
        and _command_has_resume(row.command, native_session_id)
    ]
    if not matches:
        return 0

    keep_pid = keep_pid if isinstance(keep_pid, int) and keep_pid > 0 else None
    scoped_matches = _same_runtime_rows(all_rows, matches, keep_pid=keep_pid)
    target_rows = [row for row in scoped_matches if row.pid != keep_pid]
    if keep_pid is not None and len(scoped_matches) <= 1:
        return 0
    if not target_rows:
        return 0

    target_pids = {row.pid for row in target_rows}
    for row in target_rows:
        target_pids.update(_descendant_pids(all_rows, row.pid))
    target_pids.discard(os.getpid())
    if keep_pid is not None:
        target_pids.discard(keep_pid)

    if not target_pids:
        return 0

    logger.warning(
        "Reaping %d duplicate Claude resume process(es) for native session %s (keep_pid=%s)",
        len(target_pids),
        native_session_id,
        keep_pid,
    )
    return await _reap_pid_set(target_pids, terminate_timeout=terminate_timeout, logger=logger)


async def _reap_pid_set(
    target_pids: set[int],
    *,
    terminate_timeout: float,
    logger: logging.Logger,
) -> int:
    """SIGTERM a set of pids, wait briefly, then SIGKILL the survivors.

    Returns the number of pids that were targeted.
    """
    if not target_pids:
        return 0

    for pid in sorted(target_pids):
        _signal_pid(pid, signal.SIGTERM, logger)

    deadline = asyncio.get_running_loop().time() + terminate_timeout
    remaining = set(target_pids)
    while remaining and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except Exception:
                remaining.discard(pid)

    for pid in sorted(remaining):
        _signal_pid(pid, KILL_SIGNAL, logger)

    return len(target_pids)


def _process_ages(pids: set[int]) -> dict[int, float]:
    """Best-effort elapsed-seconds-since-start for the given pids via ``ps``.

    Returns a ``{pid: age_seconds}`` map. Pids whose age cannot be determined
    are simply absent (callers treat unknown age conservatively: do not reap).
    ``etimes`` (elapsed seconds) is available on Linux procps-ng and macOS; on
    busybox ``ps`` (Alpine) it is unsupported, so this returns ``{}`` and the
    caller reaps nothing — the safe conservative fallback.
    """
    if not pids:
        return {}
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,etimes=", "-p", ",".join(str(p) for p in sorted(pids))],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return {}
    ages: dict[int, float] = {}
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ages[int(parts[0])] = float(parts[1])
        except ValueError:
            continue
    return ages


async def reap_orphaned_claude_processes(
    *,
    owned_pids: set[int],
    tracked_resume_ids: dict[str, int],
    cli_path: str | None = None,
    logger: logging.Logger,
    min_age_seconds: float = 60.0,
    terminate_timeout: float = 2.0,
    reap_in_tree: bool = True,
    exclude_pids: set[int] | None = None,
    owned_processes: list[ClaudeOwnedProcess] | None = None,
) -> int:
    """Reap leaked Claude CLI processes (defense-in-depth orphan reaper).

    Only processes with a persisted Avibe ownership record are eligible. Public
    Claude CLI flags such as ``--resume`` and ``--input-format stream-json`` are
    not sufficient proof: user watches, other backend turns, and external SDK
    clients can legitimately use the same flags. The caller may still disable
    the current-tree sweep via ``reap_in_tree`` whenever the live owner set is
    incomplete.

    Safety guards:
    - ``owned_pids`` and their descendants are never reaped.
    - ``exclude_pids`` and their descendants are never reaped. Callers use this
      for managed watch/automation commands and in-flight auth/session startup
      roots that are service descendants but not Claude runtime sessions.
    - The current service pid is never reaped.
    - A ``min_age_seconds`` grace window protects a freshly spawned process
      that has not yet been registered into the tracked set (TOCTOU). A
      candidate whose age cannot be determined is **not** reaped.

    Returns the number of pids that were signalled, which includes descendant
    helper processes — not the number of orphan roots. The blocking ``ps``
    reads are offloaded to a thread so this never stalls the event loop (this
    runs on every periodic cleanup sweep).
    """
    if os.name == "nt":
        return 0

    loop = asyncio.get_running_loop()
    try:
        all_rows = _parse_ps_rows(await loop.run_in_executor(None, _run_ps))
    except Exception:
        logger.debug("Failed to read process table for Claude orphan cleanup", exc_info=True)
        return 0

    # Build the parent->children index once and reuse it for every descendant
    # walk below (service tree, owned descendants, orphan descendants).
    children = _build_children_map(all_rows)

    service_pid = os.getpid()
    service_tree = {service_pid} | _descendant_pids(all_rows, service_pid, children)

    owned_all: set[int] = {pid for pid in owned_pids if isinstance(pid, int) and pid > 0}
    for pid in list(owned_all):
        owned_all.update(_descendant_pids(all_rows, pid, children))

    excluded_all: set[int] = {
        pid for pid in (exclude_pids or set()) if isinstance(pid, int) and pid > 0
    }
    for pid in list(excluded_all):
        excluded_all.update(_descendant_pids(all_rows, pid, children))

    claude_rows = [
        row
        for row in all_rows
        if row.pid != service_pid and _command_is_claude(row.command, cli_path=cli_path)
    ]
    row_by_pid = {row.pid: row for row in claude_rows}
    registry = owned_processes if owned_processes is not None else _load_owned_process_registry()
    registry_pids = {
        record.pid
        for record in registry
        if record.started_at is not None and record.pid in row_by_pid
    }
    current_start_times = await loop.run_in_executor(None, _process_start_times, registry_pids)

    candidates: set[int] = set()

    for record in registry:
        row = row_by_pid.get(record.pid)
        if row is None or not _process_identity_matches(record, row, current_start_times.get(record.pid)):
            continue
        if record.started_at is None:
            continue
        in_service_tree = row.pid in service_tree
        if record.owner != AVIBE_CLAUDE_SESSION_OWNER:
            continue
        if not _command_has_stream_json_input(row.command):
            continue
        if row.pid in owned_all or row.pid in excluded_all:
            continue
        if in_service_tree:
            if reap_in_tree:
                candidates.add(row.pid)
            continue
        if record.native_session_id and record.native_session_id in tracked_resume_ids:
            candidates.add(row.pid)

    candidates -= owned_all
    candidates -= excluded_all
    candidates.discard(service_pid)
    if not candidates:
        return 0

    # TOCTOU grace window: never reap a process younger than the cutoff, and
    # never reap one whose age we cannot establish. Offload the ``ps`` read.
    ages = await loop.run_in_executor(None, _process_ages, candidates)
    aged_candidates = {pid for pid in candidates if ages.get(pid, 0.0) >= min_age_seconds}
    if not aged_candidates:
        return 0

    # Reap each orphan together with its descendants (e.g. node helpers).
    target_pids: set[int] = set(aged_candidates)
    for pid in aged_candidates:
        target_pids.update(_descendant_pids(all_rows, pid, children))
    target_pids -= owned_all
    target_pids -= excluded_all
    target_pids.discard(service_pid)
    if not target_pids:
        return 0

    logger.warning(
        "Reaping %d orphaned Claude process(es) with no owning session "
        "(%d pids incl. descendants): %s",
        len(aged_candidates),
        len(target_pids),
        sorted(target_pids),
    )
    return await _reap_pid_set(target_pids, terminate_timeout=terminate_timeout, logger=logger)
