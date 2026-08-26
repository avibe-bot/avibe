from __future__ import annotations

import argparse
import email.parser
import json
import logging
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from config import paths

# Imported here, at the top, rather than where the rollback path uses it. This
# process runs the version being rolled back FROM, and a pinned reinstall
# replaces the source tree underneath it, so a first import taken after that
# install reads the OTHER release's source into this process. Importing at module
# load makes every later use a `sys.modules` hit that touches no file. The module
# is pure stdlib, so paying for it on every restart costs nothing.
from storage.backups import find_restorable_backup, next_backup_sequence, restore_sqlite_backup
from vibe import runtime
from vibe.build_identity import get_build_identity
from vibe.upgrade import (
    LEGACY_PACKAGE_NAME,
    PACKAGE_NAME,
    RollbackTarget,
    activate_launcher_target,
    atomic_uv_install_root,
    atomic_upgrade_lock,
    _launcher_generation,
    get_cli_launcher_path,
    _names_a_published_release,
    build_upgrade_plan,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_safe_cwd,
    installed_package_name,
)


logger = logging.getLogger(__name__)
_RESTART_LOG_RETENTION = 10
_SERVICE_LOCK_RELEASE_TIMEOUT_SECONDS = 30.0
# Long enough for a package index to be slow, short enough that a hung download
# does not leave the machine sitting with no service forever. A rollback that
# times out is recorded as failed, which is a diagnosable state; a rollback that
# waits without a bound is not.
_ROLLBACK_INSTALL_TIMEOUT_SECONDS = 600.0
# A cold UI process has an interpreter to start and an ASGI app to import before
# it answers, so the 5s default of `wait_for_ui_server` -- sized for a UI started
# next to an already-warm CLI -- is not the right bound here. This one is only
# ever paid in full when the UI is genuinely not coming.
_ROLLBACK_UI_READY_TIMEOUT_SECONDS = 60.0


class StartedRuntime(NamedTuple):
    """What a start actually launched, and where its UI can be checked.

    The health target travels with the pids because `_start_runtime_processes` is
    the only place that resolves it -- it loads the config to decide the bind host
    and port -- and a caller that recomputed it would become a second answer to
    "where is the UI", free to drift from the one the UI was actually started on.
    It is `None` when this job does not own the UI, which is also the answer to
    whether this job is in a position to judge the UI at all.
    """

    service_pid: int
    ui_pid: int | None
    ui_health_target: tuple[str, int] | None


def _live_ui_pid(candidate: object) -> int | None:
    """The UI pid a `running` status may carry, or None.

    Publishing the pid of a UI that has already exited makes the status file say
    the Web UI is serving when nothing is listening, and every reader of that
    file -- doctor, the dashboard, the CLI -- repeats it. Liveness is checked at
    the moment of the claim rather than assumed from the moment of the spawn.
    """

    if not isinstance(candidate, int) or candidate <= 0:
        return None
    return candidate if runtime.pid_alive(candidate) else None


def _ui_is_serving(started: StartedRuntime) -> bool:
    """Whether the UI this job started is answering its health endpoint."""

    if not started.ui_pid or started.ui_health_target is None:
        return False
    if not runtime.pid_alive(started.ui_pid):
        return False
    host, port = started.ui_health_target
    return runtime.wait_for_ui_server(host, port, timeout=_ROLLBACK_UI_READY_TIMEOUT_SECONDS)


def _running_ui_version() -> str | None:
    """Read the version that is still serving while an upgrade is being staged.

    Releases before the rollback protocol cannot pass a target into the detached
    supervisor. The old service is still alive when that supervisor starts, so
    its own version endpoint is the last authoritative answer before the old
    install is stopped and replaced.
    """

    from core.services import settings as settings_service

    config = settings_service.load_config(default_factory=settings_service.default_config)
    host = runtime.effective_ui_bind_host(config)
    if host in {"0.0.0.0", ""}:
        host = "127.0.0.1"
    elif host in {"::", "::0"}:
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    base_url = f"http://{host}:{config.ui.setup_port}"
    for endpoint in ("/api/version/local", "/api/version"):
        try:
            with urllib.request.urlopen(f"{base_url}{endpoint}", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            continue
        version = payload.get("current") if isinstance(payload, dict) else None
        if isinstance(version, str) and _names_a_published_release(version):
            return version
    return None


def _launcher_from_python(python: str) -> runtime.ServiceLauncher | None:
    """Pair an interpreter with the service entry point in its install."""

    python_path = Path(python)
    package_root = python_path.parent.parent / "lib"
    main_candidates = sorted(package_root.glob("python*/site-packages/vibe/service_main.py"))
    if not main_candidates:
        package_root = python_path.parent.parent / "Lib" / "site-packages"
        main_candidates = sorted(package_root.glob("vibe/service_main.py"))
    if not main_candidates:
        return None
    return runtime.ServiceLauncher(python=python, main=str(main_candidates[0]))


def _service_launcher_from_process(pid: int | None) -> runtime.ServiceLauncher | None:
    """Read the old launcher from a still-running service or UI command line."""

    if not pid:
        return None
    command = runtime.get_process_command(pid)
    if not command:
        return None
    try:
        argv = [arg.strip("\"'") for arg in shlex.split(command, posix=(os.name != "nt"))]
        if Path(argv[0]).name.lower() == "systemd-run" and "--" in argv:
            argv = argv[argv.index("--") + 1 :]
        if not argv or not Path(argv[0]).name.lower().startswith("python"):
            return None
        entry = next(
            (arg for arg in argv[1:] if not arg.startswith("-") and Path(arg).name in {"main.py", "service_main.py"}),
            None,
        )
        if entry is not None:
            return runtime.ServiceLauncher(python=argv[0], main=entry)
        # The UI is launched with ``python -c`` rather than service_main.py.
        # Its interpreter still identifies the install that owns the old code.
        return _launcher_from_python(argv[0])
    except (IndexError, ValueError):
        return None


def _legacy_service_launcher(
    vibe_path: str | None, *, service_pid: int | None = None, ui_pid: int | None = None
) -> runtime.ServiceLauncher:
    """Recover the launcher that existed before the package-manager replace.

    The service command is authoritative: the normal ``~/.local/bin/vibe``
    symlink may already point at the replacement by the time this process starts.
    """

    for pid in (service_pid, ui_pid):
        process_launcher = _service_launcher_from_process(pid)
        if process_launcher is not None:
            return process_launcher

    fallback = runtime.current_service_launcher()
    if not vibe_path:
        return fallback

    try:
        script = Path(vibe_path).resolve()
        shebang = script.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        python = shebang[2:].strip().split()[0] if shebang.startswith("#!") else ""
        python_path = Path(python)
        if not python or not python_path.is_file():
            return fallback
        return _launcher_from_python(str(python_path)) or fallback
    except (OSError, IndexError, ValueError):
        return fallback


def _launcher_dist_metadata(launcher: runtime.ServiceLauncher) -> list[tuple[str, str]]:
    """Read all supported distributions from the launcher's site-packages."""

    site_packages = Path(launcher.main).resolve().parent.parent
    entries: list[tuple[str, str]] = []
    try:
        for metadata_path in sorted(site_packages.glob("*.dist-info/METADATA")):
            payload = email.parser.Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
            name = str(payload.get("Name") or "").strip()
            version = str(payload.get("Version") or "").strip()
            if name in {PACKAGE_NAME, LEGACY_PACKAGE_NAME} and version:
                entries.append((name, version))
    except (OSError, UnicodeError, ValueError):
        return []
    return entries


def _launcher_package_name(launcher: runtime.ServiceLauncher, *, version: str | None = None) -> str:
    """Infer the distribution name that owns a launcher when metadata is stale."""

    metadata = _launcher_dist_metadata(launcher)
    if version:
        exact = [name for name, candidate in metadata if candidate == version]
        if exact:
            return exact[0]

    executable = launcher.python.replace("\\", "/")
    for package in (PACKAGE_NAME, LEGACY_PACKAGE_NAME):
        if f"/uv/tools/{package}/" in executable:
            return package
    names = {name for name, _version in metadata}
    if PACKAGE_NAME in names:
        return PACKAGE_NAME
    if LEGACY_PACKAGE_NAME in names:
        return LEGACY_PACKAGE_NAME
    return installed_package_name(python_executable=launcher.python) or PACKAGE_NAME


def _legacy_install_metadata(
    launcher: runtime.ServiceLauncher, *, package_name: str
) -> tuple[str, str] | None:
    """Read the old release metadata from the launcher-owned site-packages.

    A renamed distribution can leave an older ``vibe-remote`` dist-info beside
    the current ``avibe-os`` install. The launcher identifies which side was
    running, so unrelated metadata must never win by directory order.
    """

    try:
        from vibe import __version__ as replacement_version

        for name, version in _launcher_dist_metadata(launcher):
            if (
                name == package_name
                and version
                and version != replacement_version
                and _names_a_published_release(version)
            ):
                return version, name
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def _discover_legacy_upgrade_target(*, trigger: str, vibe_path: str | None) -> RollbackTarget | None:
    """Build a rollback target for an upgrade initiated by an older release."""

    if trigger != "upgrade":
        return None
    service_pid = _read_recorded_pid()
    ui_pid = _read_recorded_ui_pid()
    launcher = _legacy_service_launcher(vibe_path, service_pid=service_pid, ui_pid=ui_pid)
    version = _running_ui_version()
    package = _launcher_package_name(launcher, version=version)
    if version is None:
        metadata = _legacy_install_metadata(launcher, package_name=package)
        if metadata is None:
            return None
        version, package = metadata
    return RollbackTarget(version=version, package=package, launcher=launcher)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _restart_log_path(job_id: str) -> Path:
    paths.get_logs_dir().mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return paths.get_logs_dir() / f"restart-{timestamp}-{job_id}.log"


def _pending_restart_path() -> Path:
    return paths.get_runtime_dir() / "pending_restart.json"


def mark_pending_restart(
    *,
    trigger: str,
    scope: str = "service",
    reason: str = "restart_in_progress",
    restart_job_id: str | None = None,
) -> dict:
    payload = {
        "trigger": trigger,
        "scope": scope,
        "reason": reason,
        "restart_job_id": restart_job_id,
        "created_at": _now_iso(),
        "created_at_epoch": time.time(),
    }
    path = _pending_restart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, payload)
    return payload


def _consume_pending_restart_for_job(job_id: str) -> dict | None:
    path = _pending_restart_path()
    payload = runtime.read_json(path)
    if not isinstance(payload, dict):
        return None
    restart_job_id = payload.get("restart_job_id")
    if restart_job_id and restart_job_id != job_id:
        return None
    try:
        path.unlink()
    except OSError:
        logger.debug("Failed to remove pending restart marker", exc_info=True)
    return payload


def _prune_restart_logs(limit: int = _RESTART_LOG_RETENTION) -> None:
    try:
        logs = sorted(
            paths.get_logs_dir().glob("restart-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        logger.debug("Failed to list restart audit logs", exc_info=True)
        return
    for path in logs[limit:]:
        try:
            path.unlink()
        except OSError:
            logger.debug("Failed to prune restart audit log %s", path, exc_info=True)


def _write_status(payload: dict) -> None:
    status = {**payload, "updated_at": _now_iso()}
    path = runtime.get_restart_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, status)


def _read_recorded_pid() -> int | None:
    pid_path = paths.get_runtime_pid_path()
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid > 0:
        return pid
    return runtime.resolve_service_owner_pid()


def _read_recorded_ui_pid() -> int | None:
    pid_path = paths.get_runtime_ui_pid_path()
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _remaining_service_pids_after_stop() -> list[int]:
    owner_pid = runtime.resolve_service_owner_pid(include_starting=True)
    pids: list[int] = []
    if owner_pid:
        pids.append(owner_pid)
    pids.extend(runtime.extra_service_process_pids(owner_pid=owner_pid))
    return sorted(set(pids))


def _read_starting_service_status() -> dict | None:
    status = runtime.read_status()
    if status.get("state") != "starting":
        return None
    return status


def _service_pid_from_status(status: dict | None) -> int | None:
    if status is None:
        return None
    pid = status.get("service_pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _rounded_seconds(seconds: float) -> float:
    return round(max(0.0, seconds), 3)


def _fail(payload: dict, error: str, log, return_code: int, *, started_at: float | None = None) -> int:
    if started_at is not None:
        durations = dict(payload.get("stage_durations") or {})
        durations["restart_total_seconds"] = _rounded_seconds(time.monotonic() - started_at)
        payload["stage_durations"] = durations
    payload.update(ok=False, state="failed", error=error)
    _write_status(payload)
    log.write(f"{_now_iso()} {error}\n")
    log.flush()
    return return_code


def _runtime_ready_for_config(config) -> bool:
    has_configured_platform_credentials = getattr(config, "has_configured_platform_credentials", None)
    if callable(has_configured_platform_credentials):
        return bool(has_configured_platform_credentials())
    return bool(getattr(getattr(config, "slack", None), "bot_token", ""))


def _start_runtime_processes(
    start_ui: bool = True,
    launcher: runtime.ServiceLauncher | None = None,
) -> StartedRuntime:
    """Start the service, and the UI when this job owns it.

    `launcher` is the install to start them from, and `None` means this one --
    which is the right answer for every restart except a rollback, where this
    process is running the release being replaced and so is the one install that
    must not be started. It is taken once here and handed to both spawns, because
    the service and the UI are two halves of one generation: starting them from
    different installs is a state neither release was ever tested in.
    """

    from core.memory.ui_access import generate_ui_read_secret, process_ui_read_secret
    from core.services import settings as settings_service

    paths.ensure_data_dirs()
    config = settings_service.load_config(default_factory=settings_service.default_config)
    memory_ui_secret = process_ui_read_secret()
    if memory_ui_secret is None and start_ui:
        memory_ui_secret = generate_ui_read_secret()

    # Service-only restart: the UI process was never stopped, so carry its
    # existing pid through EVERY status write — including the early
    # starting/setup writes and any failure path — so a crash mid-start can't
    # leave the status reporting ui_pid=None while the UI is still serving.
    preserved_ui_pid = None if start_ui else _read_recorded_ui_pid()

    if _runtime_ready_for_config(config):
        runtime.write_status("starting", None, None, preserved_ui_pid)
    else:
        runtime.write_status("setup", "missing platform credentials", None, preserved_ui_pid)

    service_pid = runtime.start_service(
        wait_for_ready=False,
        initial_ready_timeout=0,
        memory_ui_secret=memory_ui_secret,
        launcher=launcher,
    )
    ui_health_target: tuple[str, int] | None = None
    if start_ui:
        bind_host = runtime.effective_ui_bind_host(config)
        ui_health_target = (bind_host, config.ui.setup_port)
        ui_pid = runtime.start_ui(
            bind_host,
            config.ui.setup_port,
            wait_for_ready=False,
            memory_ui_secret=memory_ui_secret,
            launcher=launcher,
        )
    else:
        ui_pid = preserved_ui_pid

    # Provisional, and never "running": holding the lock is not having started, so
    # this helper is not in a position to claim it. Both callers wait for the
    # service's own report and promote the status themselves. Claiming it here
    # published `running` to anyone reading the status file -- doctor, the Web UI --
    # for a process still migrating, which is the same wrong answer one layer down.
    if runtime.pid_alive(service_pid):
        runtime.write_status("starting", "waiting for service to finish starting", service_pid, ui_pid)
    else:
        runtime.write_status("error", "service process exited before startup completed", service_pid, ui_pid)
        raise RuntimeError(f"Vibe service process pid={service_pid} exited before acquiring the service lock")

    return StartedRuntime(service_pid, ui_pid, ui_health_target)


def _stop_ui_for_restart() -> tuple[bool, dict[str, float | bool], float, int | None]:
    timings: dict[str, float | bool] = {}
    started_at = time.monotonic()
    stopped = runtime.stop_ui(timings, stop_remote_access=False)
    return bool(stopped), timings, _rounded_seconds(time.monotonic() - started_at), _read_recorded_ui_pid()


def _stop_service_for_restart() -> tuple[bool, float]:
    started_at = time.monotonic()
    stopped = runtime.stop_service()
    return bool(stopped), _rounded_seconds(time.monotonic() - started_at)


def _stop_runtime_for_restart(stop_ui: bool = True) -> tuple[bool, dict[str, float | bool], float, int | None, bool, float]:
    if not stop_ui:
        # Service-only restart: leave the UI process untouched so the open Web
        # UI survives. Report its still-recorded pid; ``ui_stopped`` is True only
        # to satisfy the "did the UI stop" guard (we deliberately did not stop it).
        service_stopped, stop_service_seconds = _stop_service_for_restart()
        return True, {}, 0.0, _read_recorded_ui_pid(), service_stopped, stop_service_seconds
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="avibe-restart-stop") as executor:
        ui_future = executor.submit(_stop_ui_for_restart)
        service_future = executor.submit(_stop_service_for_restart)
        ui_stopped, ui_timings, stop_ui_seconds, ui_pid = ui_future.result()
        service_stopped, stop_service_seconds = service_future.result()
    return ui_stopped, ui_timings, stop_ui_seconds, ui_pid, service_stopped, stop_service_seconds


def _failed_generation_still_running(*, include_ui: bool) -> bool:
    """Whether anything the failed generation left behind is still alive.

    Measured -- from the lock, the pid file and the process table -- and never
    read off whether the stop helpers reported killing something. That report
    answers a different question and answers it backwards on the ordinary case: a
    version that died in its migration leaves nothing to kill, so `stop_service`
    says it stopped nothing, exactly as it does for a process that refused to
    die. The restart path above already knew this and asks
    `_remaining_service_pids_after_stop` rather than believe the report; asking
    it here too is what keeps one fact from having two answers.
    """

    if _remaining_service_pids_after_stop():
        return True
    return bool(include_ui and runtime.ui_pid_file_points_to_running_ui())


def _restore_database_for_rollback(backup_watermark: int | None, write) -> dict:
    """Put back the database the version being rolled back from migrated, if it did.

    Whether a migration happened is decided from a number this job read out of
    the backup window itself before the new version was allowed to start. Any
    rollback point recorded at or above it was written during that window, so it
    was written by the version now being rolled back from -- measured by our own
    code, before and after, with nothing in between that the failing version got
    to report about itself.

    Every cheaper test compares labels and every one of them is wrong on a real
    case: the revision stamp and the schema both stay put when a migration
    commits rows and then fails, and the wall clock reverses the order of two
    attempts if it is corrected backwards between them. `storage.backups` refuses
    to answer from those for the same reason.

    No watermark means the job never reached the point of starting the new
    version, so no migration of its doing can exist and there is nothing to put
    back. That is a normal outcome, not a degraded one.
    """

    if backup_watermark is None:
        return {"restored": False, "reason": "no_migration_window"}

    backups_dir = paths.get_state_backups_dir()
    rollback_point = find_restorable_backup(backups_dir, written_at_or_after=backup_watermark)
    if rollback_point is None:
        write(f"no rollback point was written at or after backup sequence {backup_watermark}; database left as it is")
        return {"restored": False, "reason": "no_rollback_point"}

    db_path = paths.get_sqlite_state_path()
    write(f"restoring the database from {rollback_point}")
    replaced = restore_sqlite_backup(rollback_point, db_path)
    write(f"database restored; the one it replaced is at {replaced}" if replaced else "database restored")
    return {
        "restored": True,
        "restored_from": str(rollback_point),
        "replaced_database": str(replaced) if replaced else None,
    }


def _roll_back_failed_upgrade(
    *,
    rollback_to: RollbackTarget,
    vibe_path: str | None,
    start_ui: bool,
    backup_watermark: int | None,
    write,
    record,
) -> dict:
    """Put the machine back on the version it was upgrading from.

    Reached only when a restart has failed AND nothing holds the service lock.
    That second half is the whole condition, and it is deliberately not a list of
    the failure branches that ought to qualify: such a list is complete only
    until the next branch is added, and the branch nobody remembered is exactly
    the one that leaves an instance dark. "No service is running" is the property
    the upgrade must not be able to produce, so it is what gets asked -- and it
    answers correctly for free on the failures that changed nothing, like a stop
    that did not stop, where the old service is still serving and rolling back
    underneath it would be the damage.

    Nothing of the failed generation is left running first. "No service is
    running" is a statement about the lock, and a process can be alive without it
    -- one that died before reaching it, or one still on its way there. Left
    alone that process is not merely litter: `start_service` adopts a live
    recorded pid instead of launching what was just reinstalled, so the rollback
    would report success while the failed version is what is running, and the
    restore would rewrite the database file under a process holding it open.

    Install, then restore, then start. The install is the step that needs a
    package index and so the step most likely to fail, and it is the only one
    with no effect on the data: failing there leaves the database exactly as the
    upgrade left it, and a rollback that changed nothing is a far better thing to
    find than one that half-changed the schema. Restoring before starting is what
    makes the started version able to read what it finds.

    Each step is recorded before the next begins, because this process can be
    killed mid-rollback and what it has already done to the database has to be
    readable afterwards from the status record rather than inferred from the
    disk.

    Start comes from `rollback_to.launcher`, never from this process. This
    process is the release the rollback is undoing: an upgrade spawns the restart
    job through the `vibe` on PATH, which the install has already replaced, so by
    the time this runs `sys.executable` names the failed generation. Reading it
    here was how a rollback across the `vibe-remote` -> `avibe-os` rename
    reinstalled the right release into the right directory, started the wrong one
    out of the other directory, and recorded success.
    """

    version = rollback_to.version
    restore_stable_launcher = bool(
        vibe_path and _launcher_generation(Path(vibe_path).expanduser(), atomic_uv_install_root()) is not None
    )
    rollback: dict = {"target_version": version, "state": "running", "started_at": _now_iso()}
    record(rollback)
    write(f"rolling back to {version}: no service is running after the failed restart")

    # `start_ui` is also the answer to whether the UI is this job's to manage: a
    # service-only restart left the running UI alone on purpose, and quiescing
    # must not take it down when starting will not bring it back.
    #
    # The outcome is then measured rather than taken from what the stop helpers
    # report, because a process that resists termination is the entire reason
    # this step exists and "did the stop kill something" is not that question.
    _stop_runtime_for_restart(stop_ui=start_ui)
    quiesced = not _failed_generation_still_running(include_ui=start_ui)
    rollback["quiesced"] = quiesced
    record(rollback)
    if not quiesced:
        # Nothing further is safe. The restore rewrites a database file this
        # process holds open, and the final start adopts its pid instead of
        # launching what was reinstalled -- so a rollback that continued here
        # would report success for the version it was rolling back from.
        rollback.update(
            state="failed",
            error=f"the failed generation is still running; not rolling back to {version}",
        )
        record(rollback)
        write(f"cannot roll back to {version}: the failed generation did not stop")
        return rollback

    # A staged upgrade leaves the previous tool generation untouched. Reuse it
    # directly during rollback; reinstalling the old wheel would reintroduce a
    # long, mutable operation into the one path that exists to recover quickly.
    rollback_cli_launcher = get_cli_launcher_path(rollback_to.launcher) if restore_stable_launcher else None
    if restore_stable_launcher and vibe_path and rollback_cli_launcher is not None:
        rollback["install"] = {"method": "atomic", "ok": True, "reused": True}
        record(rollback)
        write(f"reusing the previous {version} generation")
    else:
        try:
            plan = build_upgrade_plan(vibe_path=vibe_path, version=version, package_name=rollback_to.package)
        except Exception as exc:
            rollback.update(state="failed", error=f"cannot build a pinned install for {version}: {exc}")
            record(rollback)
            return rollback

        rollback["install"] = {"method": plan.method, "ok": None}
        record(rollback)
        try:
            result = subprocess.run(
                plan.command,
                capture_output=True,
                text=True,
                env=plan.env,
                cwd=get_safe_cwd(),
                timeout=_ROLLBACK_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            rollback["install"] = {"method": plan.method, "ok": False, "error": str(exc)}
            rollback.update(state="failed", error=f"installing {version} failed: {exc}")
            record(rollback)
            return rollback
        if result.returncode != 0:
            # The installer's own stderr, trimmed: it is the only account of why the
            # rollback could not proceed, and the full text can be megabytes of
            # resolver output.
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            rollback["install"] = {"method": plan.method, "ok": False, "error": detail or f"exit code {result.returncode}"}
            rollback.update(state="failed", error=f"installing {version} failed with exit code {result.returncode}")
            record(rollback)
            write(f"pinned install of {version} failed: {detail}")
            return rollback
        rollback["install"] = {"method": plan.method, "ok": True}
        record(rollback)
        write(f"installed {version} using {plan.method}")

    if restore_stable_launcher and vibe_path and rollback_cli_launcher is not None:
        try:
            with atomic_upgrade_lock():
                activate_launcher_target(vibe_path, rollback_cli_launcher)
        except Exception as exc:  # noqa: BLE001
            rollback.update(state="failed", error=f"restoring the active launcher failed: {exc}")
            record(rollback)
            return rollback
        rollback["launcher"] = {"restored": True, "path": str(rollback_cli_launcher)}
        record(rollback)

    try:
        rollback["database"] = _restore_database_for_rollback(backup_watermark, write)
    except Exception as exc:
        rollback["database"] = {"restored": False, "error": str(exc)}
        rollback.update(state="failed", error=f"restoring the database failed: {exc}")
        record(rollback)
        return rollback
    record(rollback)

    try:
        started = _start_runtime_processes(start_ui=start_ui, launcher=rollback_to.launcher)
    except Exception as exc:
        rollback.update(state="failed", error=f"starting {version} failed: {exc}")
        record(rollback)
        return rollback
    ready_pid = runtime.wait_for_service_ready(started.service_pid, timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS)
    if ready_pid is None:
        rollback.update(
            state="failed",
            service_pid=started.service_pid,
            error=f"{version} started but service pid {started.service_pid} did not finish starting",
        )
        record(rollback)
        return rollback

    # The UI is checked too, and only here. It is started with
    # `wait_for_ready=False` so a slow asset load cannot stall the service's own
    # readiness, but not waiting for it is not the same as not checking it: this
    # rollback is unattended and is the last line of defence, so "the machine is
    # back" has to mean every process this job took down came back. An ordinary
    # `vibe restart` deliberately does not gate on this -- a human is watching it,
    # and failing a restart on a slow UI would be a worse answer than reporting
    # it (see the PR's known-by-design ledger).
    ui_serving: bool | None = None
    if start_ui:
        ui_serving = _ui_is_serving(started)
        rollback["ui"] = {"pid": started.ui_pid, "serving": ui_serving}
    runtime.write_status("running", f"pid={ready_pid}", ready_pid, _live_ui_pid(started.ui_pid))
    if ui_serving is False:
        rollback.update(
            state="failed",
            service_pid=ready_pid,
            error=(
                f"rolled back to {version} and the service is running as pid {ready_pid}, "
                f"but the Web UI this rollback restarted never started serving"
            ),
        )
        record(rollback)
        write(f"rolled back to {version} but the Web UI did not come back; service pid={ready_pid}")
        return rollback
    rollback.update(state="succeeded", service_pid=ready_pid, error=None)
    record(rollback)
    write(f"rolled back to {version}; service pid={ready_pid}")
    return rollback


def _wait_for_service_lock_release(timeout: float = _SERVICE_LOCK_RELEASE_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        available, _holder_pid = runtime.service_instance_lock_available()
        if available:
            return True
        time.sleep(0.2)
    available, _holder_pid = runtime.service_instance_lock_available()
    return available


def _run_restart_job(
    *,
    job_id: str,
    delay_seconds: float,
    vibe_path: str | None,
    trigger: str,
    scope: str = "all",
    prepare_show_runtime: bool = False,
    rollback_to: RollbackTarget | None = None,
) -> int:
    # "service": restart only the service process, leaving the Web UI process
    # running (a config change shouldn't tear down the open Web UI). "all"
    # (default, e.g. CLI `vibe restart` / upgrades) restarts both.
    restart_ui = scope != "service"
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
    log_path = _restart_log_path(job_id)
    safe_cwd = get_safe_cwd()
    _prune_restart_logs()

    rollback_target_source = "explicit" if rollback_to else None
    rollback_discovery_error: str | None = None
    legacy_target_required = trigger == "upgrade" and get_build_identity().kind == "package"
    if rollback_to is None and trigger == "upgrade":
        try:
            rollback_to = _discover_legacy_upgrade_target(trigger=trigger, vibe_path=vibe_path)
            if rollback_to is not None:
                rollback_target_source = "running_service"
        except Exception as exc:
            # Older releases do not know how to carry a target. Discovery is a
            # compatibility aid; if it cannot identify one, the job fails closed
            # before stopping the old runtime rather than creating a dark one.
            rollback_discovery_error = str(exc)

    with log_path.open("a", encoding="utf-8") as log:
        def write(message: str) -> None:
            log.write(f"{_now_iso()} {message}\n")
            log.flush()

        stage_durations: dict[str, float | bool] = {}

        def record_duration(name: str, duration: float) -> float:
            stage_durations[name] = duration
            payload["stage_durations"] = dict(stage_durations)
            _write_status(payload)
            write(f"{name} completed in {duration:.3f}s")
            return duration

        def mark_duration(name: str, started_at: float) -> float:
            return record_duration(name, _rounded_seconds(time.monotonic() - started_at))

        # The backup sequence read just before the new version was allowed to
        # start, which is what tells a rollback whether that version migrated the
        # database. None until then, and None is the right answer for a failure
        # that never got that far: nothing it could have migrated exists.
        backup_watermark: int | None = None

        def record_rollback(rollback: dict) -> None:
            payload["rollback"] = dict(rollback)
            _write_status(payload)

        def fail(error: str, return_code: int, *, started_at: float | None = None) -> int:
            """End this job as failed, rolling back first when nothing is running.

            Every failure in this job goes through here rather than each branch
            deciding whether it is the kind that warrants a rollback. Which
            branches those are is not knowable as a list -- the next branch added
            would not be on it -- so the decision is made once, from the state the
            upgrade must never be able to produce: no service holding the lock.
            """

            if rollback_to and not runtime.verified_service_running():
                try:
                    _roll_back_failed_upgrade(
                        rollback_to=rollback_to,
                        vibe_path=vibe_path,
                        start_ui=restart_ui,
                        backup_watermark=backup_watermark,
                        write=write,
                        record=record_rollback,
                    )
                except Exception as exc:
                    # A rollback that raises must not swallow the failure that
                    # caused it: the original error is what the operator needs,
                    # and the rollback's own is recorded beside it.
                    record_rollback({"target_version": rollback_to.version, "state": "failed", "error": str(exc)})
                    write(f"rollback to {rollback_to.version} raised: {exc}")
            elif rollback_to:
                record_rollback(
                    {"target_version": rollback_to.version, "state": "skipped", "reason": "service_running"}
                )
            return _fail(payload, error, log, return_code, started_at=started_at)

        old_pid = _read_recorded_pid()
        payload = {
            "ok": None,
            "job_id": job_id,
            # Record this restart job's own pid (and start time) so a watcher
            # (e.g. the incus regression supervisor) can tell a live restart from a
            # stale status left by a killed job or a reboot, and from an unrelated
            # process that later reused the pid. Matches the key schedule_restart
            # seeds with the spawned subprocess pid (this process is that pid).
            "supervisor_pid": os.getpid(),
            "supervisor_started_at": runtime.process_create_time(os.getpid()),
            "state": "scheduled" if delay_seconds > 0 else "running",
            "trigger": trigger,
            "delay_seconds": delay_seconds,
            "scope": scope,
            # Recorded whether or not a rollback ends up happening, so a failed
            # restart with no `rollback` record is readable: armed and killed
            # before it could recover, versus never recoverable at all. All three
            # fields of the target, because a record that named the version and
            # the distribution but not the install would be silent about the one
            # of the three that has actually been wrong in production.
            "rollback_to": rollback_to.version if rollback_to else None,
            "rollback_package": rollback_to.package if rollback_to else None,
            "rollback_launcher": rollback_to.launcher._asdict() if rollback_to else None,
            "rollback_target_source": rollback_target_source,
            "rollback_discovery_error": rollback_discovery_error,
            "old_pid": old_pid,
            "new_pid": None,
            "log_path": str(log_path),
            "error": None,
            "created_at": _now_iso(),
            "stage_durations": stage_durations,
        }
        _write_status(payload)
        restart_started_at = time.monotonic()
        write(f"restart job scheduled trigger={trigger!r} delay_seconds={delay_seconds!r} old_pid={old_pid!r}")

        if delay_seconds > 0:
            delay_started_at = time.monotonic()
            time.sleep(delay_seconds)
            mark_duration("delay_seconds_actual", delay_started_at)
            payload["state"] = "running"
            _write_status(payload)
            write("restart job started after delay")
            restart_started_at = time.monotonic()

        if legacy_target_required and rollback_to is None:
            return fail(
                "legacy upgrade rollback target unavailable; existing runtime was left running",
                2,
                started_at=restart_started_at,
            )

        write("stopping UI and service" if restart_ui else "stopping service (Web UI kept running)")
        stop_runtime_started_at = time.monotonic()
        try:
            ui_stopped, ui_timings, stop_ui_seconds, ui_pid, stopped, stop_service_seconds = _stop_runtime_for_restart(stop_ui=restart_ui)
        except Exception as exc:
            return fail(f"stop runtime failed: {exc}", 2, started_at=restart_started_at)
        stage_durations.update(ui_timings)
        record_duration("stop_ui_total_seconds", stop_ui_seconds)
        record_duration("stop_service_seconds", stop_service_seconds)
        mark_duration("stop_runtime_seconds", stop_runtime_started_at)
        if restart_ui and ui_pid and ui_stopped is False and runtime.pid_alive(ui_pid):
            return fail(f"UI pid {ui_pid} did not stop", 2, started_at=restart_started_at)
        if stopped is False:
            remaining_service_pids = _remaining_service_pids_after_stop()
            if remaining_service_pids:
                payload["remaining_service_pids"] = remaining_service_pids
                pid_list = ",".join(str(pid) for pid in remaining_service_pids)
                return fail(
                    f"service stop failed; remaining service pid(s): {pid_list}",
                    2,
                    started_at=restart_started_at,
                )

        wait_lock_release_started_at = time.monotonic()
        if not _wait_for_service_lock_release():
            mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)
            return fail("service lock did not release after stopping runtime", 2, started_at=restart_started_at)
        mark_duration("wait_service_lock_release_seconds", wait_lock_release_started_at)

        if rollback_to:
            # Read here and nowhere else: the service is stopped, its lock is
            # released, and the new version has not run yet, so this is the last
            # instant at which the window still holds only what earlier versions
            # put there. Every backup numbered at or above it afterwards was
            # written by the version about to start.
            backup_watermark = next_backup_sequence(paths.get_state_backups_dir())
            write(f"pre-start backup sequence is {backup_watermark}; rollback target is {rollback_to.version}")

        write("starting service")
        start_runtime_started_at = time.monotonic()
        try:
            started = _start_runtime_processes(start_ui=restart_ui)
            new_pid, ui_pid = started.service_pid, started.ui_pid
        except Exception as exc:
            return fail(f"start runtime failed: {exc}", 1, started_at=restart_started_at)
        mark_duration("start_runtime_seconds", start_runtime_started_at)

        service_status = runtime.read_status()
        if not new_pid:
            new_pid = _service_pid_from_status(_read_starting_service_status())
            service_status = runtime.read_status()
        if not new_pid or not runtime.pid_alive(new_pid):
            return fail("start runtime completed but service pid is not alive", 3, started_at=restart_started_at)
        if not runtime.service_pid_recorded(new_pid):
            write(f"start runtime returned while service pid={new_pid} is still acquiring its lock")
        wait_lock_started_at = time.monotonic()
        # Asked unconditionally, and asked of the SERVICE rather than of the lock.
        # Holding the lock was never the end of starting up -- the database is
        # migrated and the controller built after it -- so a job that skipped this
        # whenever the lock had already been taken was skipping it in exactly the
        # case a bad migration produces: lock acquired, then dead, then a restart
        # recorded as succeeded over an instance with nothing running.
        # `wait_for_service_ready` also resolves the real holder, since under a
        # delegated user scope the returned pid may be a launcher that never
        # records itself.
        resolved_pid = runtime.wait_for_service_ready(new_pid, timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS)
        if resolved_pid is None:
            mark_duration("wait_service_lock_seconds", wait_lock_started_at)
            return fail(
                f"service pid {new_pid} did not finish starting",
                3,
                started_at=restart_started_at,
            )
        new_pid = resolved_pid
        mark_duration("wait_service_lock_seconds", wait_lock_started_at)
        recorded_ui_pid = service_status.get("ui_pid") if service_status else ui_pid
        runtime.write_status("running", f"pid={new_pid}", new_pid, _live_ui_pid(recorded_ui_pid))

        mark_duration("restart_total_seconds", restart_started_at)
        payload.update(ok=True, state="succeeded", new_pid=new_pid, error=None)
        _write_status(payload)
        write(f"restart job succeeded new_pid={new_pid}")

        if prepare_show_runtime:
            env = get_restart_environment(vibe_path=vibe_path)
            prepare_command = [
                *get_restart_command(vibe_path=vibe_path),
                "runtime",
                "prepare",
                "--strict",
            ]
            write("preparing Show Runtime after service restart")
            prepare_started_at = time.monotonic()
            try:
                prepare_result = subprocess.run(
                    prepare_command,
                    cwd=safe_cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=300,
                )
                if prepare_result.returncode != 0:
                    write(f"Show Runtime preparation failed with exit code {prepare_result.returncode}")
                else:
                    write("Show Runtime preparation succeeded")
            except subprocess.TimeoutExpired:
                write("Show Runtime preparation timed out after 300 seconds")
            except Exception as exc:
                write(f"Show Runtime preparation skipped: {exc}")
            finally:
                mark_duration("prepare_show_runtime_seconds", prepare_started_at)

        pending_restart = _consume_pending_restart_for_job(job_id)
        if pending_restart is not None:
            write(
                "scheduling pending follow-up restart "
                f"trigger={pending_restart.get('trigger')!r} scope={pending_restart.get('scope')!r}"
            )
            try:
                schedule_restart(
                    delay_seconds=0.0,
                    vibe_path=vibe_path,
                    trigger=str(pending_restart.get("trigger") or "pending-restart"),
                    scope=str(pending_restart.get("scope") or "service"),
                )
            except Exception as exc:
                payload["pending_restart"] = {"scheduled": False, "error": str(exc)}
                _write_status(payload)
                write(f"failed to schedule pending follow-up restart: {exc}")

        return 0


def schedule_restart(
    *,
    delay_seconds: float = 0.0,
    vibe_path: str | None = None,
    trigger: str = "cli",
    scope: str = "all",
    prepare_show_runtime: bool = False,
    memory_ui_secret: str | None = None,
    rollback_to: RollbackTarget | None = None,
    python_executable: str | None = None,
) -> dict:
    """Serialize every restart seed with install activation and pruning."""

    with atomic_upgrade_lock():
        return _schedule_restart_locked(
            delay_seconds=delay_seconds,
            vibe_path=vibe_path,
            trigger=trigger,
            scope=scope,
            prepare_show_runtime=prepare_show_runtime,
            memory_ui_secret=memory_ui_secret,
            rollback_to=rollback_to,
            python_executable=python_executable,
        )


def _schedule_restart_locked(
    *,
    delay_seconds: float,
    vibe_path: str | None,
    trigger: str,
    scope: str,
    prepare_show_runtime: bool,
    memory_ui_secret: str | None,
    rollback_to: RollbackTarget | None,
    python_executable: str | None,
) -> dict:
    """Spawn the detached restart job.

    `rollback_to` is the install currently on the machine, and passing it is what
    makes this restart recoverable: if the restart fails and leaves nothing
    holding the service lock, the job reinstalls exactly that release, puts back
    the database if the new one migrated it, and starts the service again. Only
    an upgrade has an answer for it -- a plain restart is already running the
    version it would roll back to, so there is nothing to reinstall and the
    failure is the operator's to look at.

    It arrives as one value from `rollback_target()` rather than as a version, a
    distribution name and an install, because a caller that can pass one without
    the others eventually does, and what it produces then is a pin naming a
    release that was never published, or a reinstall of the right release
    followed by a start of the wrong one. The argv below is the only place the
    three are apart, and only because a command line has no other shape; `main()`
    is the only place they are put back together.
    """
    from core.memory.ui_access import process_ui_read_secret
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    memory_ui_secret = memory_ui_secret or process_ui_read_secret()
    guard_source_checkout_default_state_bootstrap()
    job_id = uuid.uuid4().hex[:12]
    if python_executable:
        invocation = [python_executable, "-c", "from vibe.cli import main; main()", "restart"]
    else:
        invocation = get_restart_invocation_command(vibe_path=vibe_path)
    command = [*invocation[:-1], "__restart-supervisor"] if invocation and invocation[-1] == "restart" else [
        *(invocation or ["vibe"]),
        "__restart-supervisor",
    ]
    command.extend(["--job-id", job_id, "--delay-seconds", str(delay_seconds), "--trigger", trigger])
    if scope != "all":
        command.extend(["--scope", scope])
    if vibe_path:
        command.extend(["--vibe-path", vibe_path])
    if prepare_show_runtime:
        command.append("--prepare-show-runtime")
    if rollback_to:
        command.extend(
            [
                "--rollback-to",
                rollback_to.version,
                # Not optional the way the package name is. A missing package
                # name still leaves `build_upgrade_plan` a defensible default,
                # while a missing launcher leaves the job with only its own --
                # which, in the case this exists for, is the release being
                # undone.
                "--rollback-python",
                rollback_to.launcher.python,
                "--rollback-main",
                rollback_to.launcher.main,
            ]
        )
        if rollback_to.package:
            command.extend(["--rollback-package", rollback_to.package])
    env = get_restart_environment(vibe_path=vibe_path)
    if python_executable:
        # A candidate supervisor must import the staged wheel, never a source
        # checkout inherited from the parent process.
        env = dict(os.environ if env is None else env)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    log_path = _restart_log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Seed the status BEFORE spawning the job so the child's own writes (which set
    # state="running" plus its pid and start time) always land afterwards and are
    # never clobbered. A zero-delay restart could otherwise race the parent's
    # "scheduled" write on top of the child's "running" write, hiding the active
    # restart from the supervisor and making it treat the stopped service as a
    # crash. The job records its real supervisor_pid once it starts.
    payload = {
        "ok": None,
        "job_id": job_id,
        "state": "scheduled",
        "trigger": trigger,
        "scope": scope,
        "delay_seconds": delay_seconds,
        "supervisor_pid": None,
        "old_pid": _read_recorded_pid(),
        "new_pid": None,
        "log_path": str(log_path),
        "error": None,
        "created_at": _now_iso(),
    }
    _write_status(payload)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{_now_iso()} spawning restart supervisor job_id={job_id} delay_seconds={delay_seconds!r}\n")
            log.flush()
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if memory_ui_secret is not None else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=get_safe_cwd(),
                env=runtime._memory_ui_child_env(
                    env,
                    memory_ui_secret=memory_ui_secret,
                ),
            )
            runtime._spawn_stdin(process, memory_ui_secret=memory_ui_secret)
    except OSError as exc:
        # The seed status above is now "scheduled"; if the job can't be spawned
        # (bad cached vibe path, missing executable, permission/log-open error) no
        # child will ever overwrite it, leaving a permanently pending restart in
        # `vibe status`. Mark it failed before propagating.
        payload.update(ok=False, state="failed", error=f"failed to spawn restart supervisor: {exc}")
        _write_status(payload)
        _prune_restart_logs()
        raise
    # Surface the spawned pid to the caller without rewriting the status (that
    # would reintroduce the race); the job writes its own pid on disk when it runs.
    payload["supervisor_pid"] = process.pid
    _prune_restart_logs()
    return payload


def main(argv: list[str] | None = None) -> int:
    from core.memory.ui_access import initialize_process_ui_read_secret

    initialize_process_ui_read_secret()
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--trigger", default="cli")
    parser.add_argument("--scope", default="all", choices=("all", "service"))
    parser.add_argument("--vibe-path")
    parser.add_argument("--prepare-show-runtime", action="store_true")
    parser.add_argument("--rollback-to")
    parser.add_argument("--rollback-package")
    parser.add_argument("--rollback-python")
    parser.add_argument("--rollback-main")
    args = parser.parse_args(argv)
    # The one place a rollback target is apart, and so the one place it is put
    # back together. Reassembling here rather than passing four values inward
    # means nothing downstream can hold a version without the install it names --
    # and an incomplete `--rollback-*` set is refused outright rather than
    # quietly completed from this process, which is the failed release.
    rollback_to = None
    if args.rollback_to:
        if not args.rollback_python or not args.rollback_main:
            parser.error("--rollback-to requires --rollback-python and --rollback-main")
        rollback_to = RollbackTarget(
            version=args.rollback_to,
            package=args.rollback_package,
            launcher=runtime.ServiceLauncher(python=args.rollback_python, main=args.rollback_main),
        )
    return _run_restart_job(
        job_id=args.job_id,
        delay_seconds=max(0.0, args.delay_seconds),
        vibe_path=args.vibe_path,
        trigger=args.trigger,
        scope=args.scope,
        prepare_show_runtime=args.prepare_show_runtime,
        rollback_to=rollback_to,
    )


if __name__ == "__main__":
    raise SystemExit(main())
