"""Retire managed installations without promising arbitrary old-process longevity.

uv's existing tool receipt identifies an installation, including released
pre-retention layouts. The selected launcher, this invocation, official runtime
and install/restart handoffs are protected. Unregistered stale processes and
commands naming retired directories are outside this recovery-oriented contract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import shutil
import sys

import psutil
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ImportError:  # Python 3.10; already a project dependency.
    import tomli as tomllib

from config import paths
from config.atomic_io import write_atomic

logger = logging.getLogger(__name__)
INSTALLER_PID = ".avibe-installing"


def _uv_installation(generation: Path) -> tuple[Path, set[Path]] | None:
    """Recognize released Avibe tool layouts without trusting directory names."""
    environments = [
        generation / layout / package
        for layout in ("tools", "uv/tools")
        for package in ("avibe-os", "vibe-remote")
        if (generation / layout / package).is_dir()
    ]
    if len(environments) != 1:
        return None
    environment = environments[0]
    if environment.resolve() != environment:
        return None
    layout_root = environment.relative_to(generation).parts[0]
    if any(
        child.name not in {layout_root, "bin", INSTALLER_PID}
        for child in generation.iterdir()
    ):
        return None
    if any(
        child != environment and (
            child.name not in {".lock", ".gitignore", "CACHEDIR.TAG"}
            or not child.is_file() or child.is_symlink()
        )
        for child in environment.parent.iterdir()
    ):
        return None
    if layout_root == "uv" and any(
        child.name != "tools" for child in environment.parent.parent.iterdir()
    ):
        return None
    if (
        not (environment / "pyvenv.cfg").is_file()
        or (environment / "pyvenv.cfg").is_symlink()
        or (environment / "uv-receipt.toml").is_symlink()
    ):
        return None
    try:
        with (environment / "uv-receipt.toml").open("rb") as stream:
            tool = tomllib.load(stream)["tool"]
        requirement = tool["requirements"][0]
        name = requirement["name"] if isinstance(requirement, dict) else Requirement(requirement).name
        if canonicalize_name(name) != environment.name:
            return None
        exports: set[Path] = set()
        for entry in tool["entrypoints"]:
            if entry["name"].lower() not in {"vibe", "vibe.exe"}:
                return None
            exported = Path(entry["install-path"])
            if (
                exported.is_absolute()
                # uv records the logical name without Windows' EXE_SUFFIX.
                and (entry["name"].lower(), exported.name.lower()) in {
                    ("vibe", "vibe"), ("vibe", "vibe.exe"), ("vibe.exe", "vibe.exe"),
                }
                and exported.parent.resolve() == generation / "bin"
            ):
                exports.add(exported)
            else:
                return None
        if not exports:
            return None
        export_names = {export.name for export in exports}
        # A partial retirement may already have removed bin or a symlink's
        # destination. Existing extra files/directories are never our payload.
        bin_dir = generation / "bin"
        if bin_dir.exists() and any(
            child.name not in export_names or child.is_dir()
            for child in bin_dir.iterdir()
        ):
            return None
        return environment, exports
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        # Unknown/incomplete artifacts do not become deletion targets.
        return None


def _retire_installation(generation: Path, environment: Path) -> None:
    """Keep uv's ownership evidence until all sizeable content has been removed.

    Windows may reject deletion of a loaded DLL midway through rmtree. Removing
    the receipt first would leave a full, permanently unrecognizable environment.
    Do not rename/adopt tombstones by name or invent another receipt owner.
    """
    evidence = {environment / "pyvenv.cfg", environment / "uv-receipt.toml"}

    def remove_content(directory: Path) -> None:
        for child in directory.iterdir():
            if child in evidence:
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
            elif child == environment or child in environment.parents:
                remove_content(child)
            else:
                shutil.rmtree(child)

    remove_content(generation)
    # Only the two small proof files and their empty parents remain. A failure
    # here can leave a few bytes, not an unidentifiable full Python environment.
    for proof in evidence:
        proof.unlink()
    directory = environment
    while directory != generation:
        directory.rmdir()
        directory = directory.parent
    generation.rmdir()


def mark_install_generation(candidate: Path, pid: int) -> None:
    """Protect staging before publication; callers hold the install lock."""
    from vibe import upgrade

    generation = upgrade._generation_for_path(candidate, upgrade.atomic_uv_install_root())
    if generation is None or pid <= 0:
        raise ValueError("cannot establish staged installation ownership")
    generation.mkdir(parents=True, exist_ok=True)
    write_atomic(generation / INSTALLER_PID, str(pid))


def finish_install_generation(candidate: Path) -> None:
    """Withdraw staging after activation without invalidating its commit."""
    from vibe import upgrade

    try:
        generation = upgrade._generation_for_path(candidate, upgrade.atomic_uv_install_root())
        if generation is not None:
            (generation / INSTALLER_PID).unlink(missing_ok=True)
    except Exception:
        logger.warning("Activated installation retained its staging marker", exc_info=True)


def _installer_is_live(generation: Path) -> bool:
    marker = generation / INSTALLER_PID
    try:
        pid = int(marker.read_text(encoding="utf-8-sig").strip())
        created = marker.stat().st_mtime
    except FileNotFoundError:
        # Released shell installers encoded their own $$ before staging.
        # This only protects a handoff; it NEVER establishes deletion ownership.
        legacy = re.fullmatch(r"(\d+)-(\d+)-\d+", generation.name)
        if legacy is None:
            return False
        created, pid = int(legacy[1]), int(legacy[2])
    if pid <= 0:
        raise ValueError("invalid installer PID")
    try:
        process = psutil.Process(pid)
        return (
            process.status() != psutil.STATUS_ZOMBIE
            and int(process.create_time()) <= int(created)
        )
    except psutil.NoSuchProcess:
        return False


def _invocation_paths() -> set[Path]:
    # Keep logical sys.executable as well as the actual module location: uv's
    # Python symlink may resolve to a shared interpreter outside its environment.
    return {Path(sys.executable).absolute(), Path(sys.prefix).absolute(), Path(__file__).absolute()}


def _runtime_paths() -> set[Path]:
    """Inspect only official service/UI records, never the user's process table."""
    from vibe import runtime, upgrade

    root = upgrade.atomic_uv_install_root().expanduser().resolve()
    result: set[Path] = set()
    pids: set[int] = set()
    for pid_path in (paths.get_runtime_pid_path(), paths.get_runtime_ui_pid_path()):
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            continue
        if pid <= 0:
            raise ValueError("invalid official runtime PID")
        pids.add(pid)
    try:
        status = json.loads(paths.get_runtime_status_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        status = {}
    if not isinstance(status, dict):
        raise ValueError("official runtime status could not be read")
    for key in ("service_pid", "ui_pid"):
        pid = status.get(key)
        if pid is not None:
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid official status PID")
            pids.add(pid)
    available, owner = runtime.service_instance_lock_available()
    if not available:
        if not owner:
            raise ValueError("official service lock owner could not be identified")
        pids.add(owner)
    for pid in pids:
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            argv = [argument.strip('"') for argument in process.cmdline()]
            executable = process.exe()
            if not argv or not Path(argv[0]).is_absolute() or not Path(executable).is_absolute():
                raise ValueError("official runtime interpreter could not be identified")
            process_paths = {Path(executable)}
            process_paths.update(Path(value) for value in argv if value and Path(value).is_absolute())
            if sys.platform == "win32" and not any(
                upgrade._generation_for_path(path, root) is not None for path in process_paths
            ):
                # Windows venv redirectors spawn a base-Python child. The UI's
                # -c command may need its parent to identify the environment.
                # Losing that parent is not proof the official child has exited.
                try:
                    parent = process.parent()
                    parent_executable = parent.exe() if parent is not None else ""
                except psutil.NoSuchProcess:
                    parent_executable = ""
                if not parent_executable:
                    if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                        continue
                    raise ValueError("live official runtime lost its redirector")
                if not Path(parent_executable).is_absolute():
                    raise ValueError("official runtime redirector could not be identified")
                process_paths.add(Path(parent_executable))
            result.update(process_paths)
        except psutil.NoSuchProcess:
            continue
        # Access denial or an unknown official launch shape defers this pass.
    return result


def _restart_is_unsettled() -> bool:
    from vibe import upgrade

    path = upgrade.runtime_mod.get_restart_status_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if not isinstance(payload, dict):
        return True
    try:
        state = upgrade.RestartState(payload.get("state"))
    except (ValueError, TypeError):
        return True
    if state is upgrade.RestartState.UNKNOWN or upgrade.restart_record_is_pending(payload, path):
        return True
    if state is not upgrade.RestartState.SUCCEEDED:
        return False
    try:
        followup = json.loads((paths.get_runtime_dir() / "pending_restart.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if not isinstance(followup, dict):
        return True
    job_id = followup.get("restart_job_id")
    if job_id and job_id != payload.get("job_id"):
        return False
    # Success is written before the supervisor consumes its follow-up. Reuse
    # the existing process identity/seed grace policy for that unfinished tail,
    # without letting failed, superseded or abandoned markers veto forever.
    # A released unscoped marker is consumable by any successful supervisor.
    return upgrade.restart_record_is_pending(
        {**payload, "state": upgrade.RestartState.RUNNING.value}, path,
    )


def _launcher_targets(
    launcher: Path, root: Path, managed: dict[Path, tuple[Path, set[Path]]],
) -> set[Path]:
    from vibe import upgrade

    generation = upgrade._launcher_generation(launcher, root)
    if generation is not None:
        if generation not in managed:
            # An in-root wrapper can depend on another generation. Neither the
            # primary command nor PATH may authorize deleting that dependency.
            raise ValueError("selected in-root installation could not be recognized")
        return {generation}
    if launcher.is_symlink():
        # An outside alias proves nothing about which managed generation the
        # normal command selects. Wait for a managed activation instead.
        return set()
    current_bytes = launcher.read_bytes()
    return {
        generation for generation, (_, exports) in managed.items()
        if any(exported.is_file() and exported.read_bytes() == current_bytes for exported in exports)
    }


def collect_install_generations(launcher: str | Path | None = None) -> list[Path]:
    """Best-effort retirement after activation or successful runtime startup.

    Normally only the selected environment remains. Current invocation/official
    runtime and concurrent handoffs may retain more temporarily. Old activation
    receipts and arbitrary old processes are not prerequisites for collection.
    """
    from vibe import upgrade

    try:
        root = upgrade.atomic_uv_install_root().expanduser().resolve()
        if not root.is_dir():
            return []
        selected = str(launcher) if launcher is not None else upgrade.get_running_vibe_path()
        if not selected or not upgrade._is_stable_launcher_path(Path(selected)):
            return []
        launcher_path = Path(selected).expanduser().absolute()
        if not launcher_path.is_file():
            return []
        # Same lock/path as installation, but optional maintenance must not make
        # startup wait behind a long package download.
        with upgrade.atomic_upgrade_lock(timeout_seconds=0):
            if _restart_is_unsettled():
                logger.info("Install generation collection deferred: restart in progress")
                return []
            generations = [
                path for path in root.iterdir()
                if not path.is_symlink() and path.is_dir() and path.resolve().parent == root
            ]
            selected_generation = upgrade._launcher_generation(launcher_path, root)
            if any(
                generation != selected_generation and _installer_is_live(generation)
                for generation in generations
            ):
                logger.info("Install generation collection deferred: installer handoff")
                return []
            managed = {
                generation: installation for generation in generations
                if (installation := _uv_installation(generation)) is not None
            }
            logger.info(
                "Install generation retention: %d managed, %d unrecognized retained",
                len(managed), len(generations) - len(managed),
            )
            if not managed:
                return []
            kept = _launcher_targets(launcher_path, root, managed)
            if not kept:
                logger.info("Install generation collection deferred: unknown launcher target")
                return []
            # Invoking an explicit alias is not permission to break the command
            # selected by PATH. This is one ordinary command lookup, not an
            # alias registry or filesystem scan. Only confirmed managed targets
            # add protection; an unrelated command cannot veto retirement.
            path_command = shutil.which("vibe")
            if path_command and Path(path_command).absolute() != launcher_path:
                kept.update(_launcher_targets(Path(path_command).absolute(), root, managed))
            references = _invocation_paths() | _runtime_paths()
            kept.update(
                generation for path in references
                if (generation := upgrade._generation_for_path(path, root)) is not None
            )
            removed = []
            for generation in managed.keys() - kept:
                try:
                    _retire_installation(generation, managed[generation][0])
                    removed.append(generation)
                except OSError:
                    logger.warning("Could not retire installation %s", generation, exc_info=True)
            if removed:
                logger.info("Collected %d retired install generations", len(removed))
            return removed
    except Exception:
        # This includes nonblocking lock acquisition and inspection failures.
        # It must never escape to activation callers that discard a failed target.
        logger.warning("Install generation collection deferred", exc_info=True)
        return []
