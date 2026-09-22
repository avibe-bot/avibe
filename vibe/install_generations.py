"""Best-effort collection of positively owned Avibe install generations.

An activation receipt is ownership evidence, not a lease or a rollback plan.
Unreceipted directories include released installs and concurrently staged
candidates, so age and directory names never authorize their removal.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import TYPE_CHECKING

import psutil

from config.atomic_io import write_atomic

if TYPE_CHECKING:
    from vibe.upgrade import AtomicActivation

logger = logging.getLogger(__name__)
RECEIPT = ".avibe-install.json"
INSTALLER_PID = ".avibe-installing"


def _receipt_launchers(generation: Path) -> set[Path] | None:
    try:
        payload = json.loads((generation / RECEIPT).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError(f"unknown install receipt in {generation}")
    launchers = payload.get("launchers")
    if (
        not isinstance(launchers, list)
        or not launchers
        or any(
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or Path(value).name.lower() not in {"vibe", "vibe.exe"}
            for value in launchers
        )
    ):
        raise ValueError(f"invalid install receipt in {generation}")
    return {Path(value) for value in launchers}


def record_activation(launcher: Path, target: Path) -> None:
    """Record only a committed activation; failure must never discard its target."""
    from vibe import upgrade

    try:
        root = upgrade.atomic_uv_install_root().expanduser().resolve()
        generation = upgrade._generation_for_path(target, root)
        if generation is None:
            return
        launchers = _receipt_launchers(generation) or set()
        # Resolve the directory, not the entrypoint: the latter is replaced.
        launchers.add(launcher.expanduser().absolute().parent.resolve() / launcher.name)
        write_atomic(
            generation / RECEIPT,
            json.dumps({"version": 1, "launchers": sorted(map(str, launchers))}),
        )
    except Exception:
        # This is an explicit post-commit boundary: no bookkeeping error may
        # escape to callers that discard candidates on activation failure.
        # Without durable positive ownership, later collection cannot distinguish
        # this generation from pre-protocol history. It must remain unowned.
        logger.warning(
            "Install activation succeeded; retention receipt unavailable, "
            "unowned generation will be retained",
            exc_info=True,
        )


def _installer_is_live(generation: Path) -> bool:
    marker = generation / INSTALLER_PID
    try:
        pid = int(marker.read_text(encoding="utf-8-sig").strip())
        written_at = marker.stat().st_mtime
    except FileNotFoundError:
        return False
    if pid <= 0:
        raise ValueError(f"invalid installer owner in {generation}")
    try:
        process = psutil.Process(pid)
        # A reused PID is not the installer. Round for filesystems with coarse
        # timestamps; uncertainty retains data instead of deleting it.
        return process.status() != psutil.STATUS_ZOMBIE and int(process.create_time()) <= int(written_at)
    except psutil.NoSuchProcess:
        return False


def _running_paths() -> set[Path]:
    """Keep logical argv as well as the image; uv Python resolves outside its venv."""
    from config import paths as config_paths
    from vibe import runtime

    paths = {Path(sys.executable), Path(sys.prefix), Path(__file__)}
    username = psutil.Process().username()
    managed_pids = set()
    for pid_path in (config_paths.get_runtime_pid_path(), config_paths.get_runtime_ui_pid_path()):
        try:
            managed_pids.add(int(pid_path.read_text(encoding="utf-8").strip()))
        except FileNotFoundError:
            pass
    for process in psutil.process_iter():
        try:
            # An administrator can upgrade a home whose service runs as a
            # different user. Its recorded interpreters remain live references.
            if process.pid not in managed_pids and process.username() != username:
                continue
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            try:
                arguments = process.cmdline()
            except psutil.AccessDenied:
                # macOS denies KERN_PROCARGS2 for some ordinary user-owned
                # processes (e.g. login), although ps can still read them.
                command = runtime.get_process_command(process.pid)
                if not command:
                    raise
                arguments = [command, *shlex.split(command, posix=(os.name != "nt"))]
            if not arguments:
                raise RuntimeError(f"cannot inspect install references for pid {process.pid}")
            values = arguments
            try:
                values.append(process.exe())
            except psutil.AccessDenied:
                # argv already identifies the logical interpreter; exe is
                # supplemental and often points outside uv's environment.
                pass
            paths.update(
                Path(value) for value in values
                if value and "\x00" not in value and Path(value).is_absolute()
            )
        except psutil.NoSuchProcess:
            continue
        # AccessDenied and other inspection failures propagate to the
        # non-fatal collection boundary. An empty keep set is never inferred.
    return paths


def collect_before_activation(activation: AtomicActivation) -> list[Path]:
    """Keep current/source/candidate + every live reference; remove only owned history.

    The caller holds the shared install lock. Collection precedes the launcher
    switch, so the previous selected generation survives this operation. With
    one launcher and no extra references, subsequent operations retain two
    receipt-owned generations regardless of whether shell, CLI, or API created
    them. Unowned history, including failed new receipts, is never adopted.
    """
    from vibe import upgrade

    try:
        root = upgrade.atomic_uv_install_root().expanduser().resolve()
        if not root.is_dir():
            return []
        generations = [
            child for child in root.iterdir()
            if not child.is_symlink() and child.is_dir() and child.resolve().parent == root
        ]
        owned: dict[Path, set[Path]] = {}
        for generation in generations:
            launchers = _receipt_launchers(generation)
            if launchers is not None:
                owned[generation] = launchers
        logger.info(
            "Install generation retention: %d owned, %d unowned history/staging retained",
            len(owned), len(generations) - len(owned),
        )
        if not owned:
            return []
        restart_path = upgrade.runtime_mod.get_restart_status_path()
        try:
            restart = json.loads(restart_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            try:
                state = upgrade.RestartState(restart.get("state")) if isinstance(restart, dict) else None
            except (TypeError, ValueError):
                state = None
            # The ordinary restart admission policy treats unknown states as
            # stale. Destructive collection needs positive terminal/pending
            # interpretation; a future writer's state cannot authorize deletion.
            if (
                state is None
                or state is upgrade.RestartState.UNKNOWN
                or upgrade.restart_record_is_pending(restart, restart_path)
            ):
                logger.info("Install generation collection deferred: restart ownership")
                return []
        candidate = upgrade._generation_for_path(activation.candidate_launcher, root)
        if any(generation != candidate and _installer_is_live(generation) for generation in generations):
            logger.info("Install generation collection deferred: another installer owns a handoff")
            return []

        paths = _running_paths()
        paths.add(activation.candidate_launcher)
        if activation.source_generation is not None:
            paths.add(activation.source_generation)
        kept = {
            generation for path in paths
            if (generation := upgrade._generation_for_path(path, root)) is not None
        }
        launchers = {activation.launcher}
        for references in owned.values():
            launchers.update(references)
        for launcher in launchers:
            generation = upgrade._launcher_generation(launcher, root)
            if generation is not None:
                kept.add(generation)
            try:
                launcher_bytes = launcher.read_bytes()
            except FileNotFoundError:
                continue
            try:
                marker = launcher.parent / f".{launcher.name}.avibe-generation"
                marked = Path(marker.read_text(encoding="utf-8-sig").strip())
                if (generation := upgrade._generation_for_path(marked, root)) is not None:
                    kept.add(generation)
            except FileNotFoundError:
                pass
            # A copied launcher can outlive a failed/stale marker write.
            # Read fresh bytes: filecmp's stat-based cache cannot prove identity
            # after a same-size/same-mtime replacement.
            for generation in owned:
                target = generation / "bin" / launcher.name
                try:
                    target_bytes = target.read_bytes()
                except FileNotFoundError:
                    if any(reference.name == launcher.name for reference in owned[generation]):
                        kept.add(generation)  # damaged export: identity is unknown
                    continue
                if launcher_bytes == target_bytes:
                    kept.add(generation)
        removed = []
        for generation in owned.keys() - kept:
            try:
                shutil.rmtree(generation)
                removed.append(generation)
            except OSError:
                logger.warning("Could not collect owned install generation %s", generation, exc_info=True)
        if removed:
            logger.info("Collected %d superseded owned install generations", len(removed))
        return removed
    except Exception:
        # Collection is optional. Unknown ownership/visibility means defer;
        # callers must still be able to install and activate a valid candidate.
        logger.warning("Install generation collection deferred; ownership could not be established", exc_info=True)
        return []
