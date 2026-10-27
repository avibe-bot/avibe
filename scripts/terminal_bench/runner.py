"""Thin full-product trial supervisor (no Harbor or product dependency changes).

Use the interpreter in the pinned Avibe wheel's venv. This is not a sandbox: real
benchmarks must run inside an isolated task container. Local tests default to an
owned HOME; sandbox callers may supply their explicit task image environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Mapping

import psutil

from .diagnostics import project_responses, response_query
from .profile import Profile, validate_layout

_CONTROL = Path(__file__).with_name("control.py")
_OWNER = "AVIBE_BENCHMARK_OWNER"
_TERMINAL = {"succeeded", "failed", "canceled", "cancelled"}


class TrialError(RuntimeError):
    """A wrapper-owned symbolic code, never an upstream diagnostic body."""

    _CODES = {
        "owned_process_cleanup_unconfirmed", "service_exited_before_ready",
        "controller_readiness_timeout", "service_not_ready", "harness_quiescence_unconfirmed",
        "credential_input_refused", "cli_helper_invalid_run_payload",
    }
    _HELPER_CODE = re.compile(
        r"(?:initialize|serve|health|configure|usage|versions|cli)_helper_"
        r"(?:timeout|invalid_json|failed_-?[0-9]+)\Z"
    )

    def __init__(self, code: str):
        self.code = code if code in self._CODES or self._HELPER_CODE.fullmatch(code) else "unclassified_trial_error"
        super().__init__(self.code)


def failure_details(exc: BaseException) -> dict[str, str]:
    result = {"outcome": "infrastructure_error", "error_type": type(exc).__name__}
    if isinstance(exc, TrialError):
        result["error_code"] = exc.code
    return result


def scrub(value, key: str | None):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, dict):
        return {scrub(str(k), key): scrub(v, key) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key) for v in value]
    return value


class OwnedProcesses:
    """Observe before shutdown, including children that create their own sessions.

    Pids are never authority on their own. Capture creation identity while the
    process is a descendant, or proves our random per-trial environment marker.
    Never read a global Avibe runtime pidfile or kill by executable name.
    """

    def __init__(self, process: subprocess.Popen, marker: str):
        self.process = process
        self.marker = marker
        self.identities: dict[int, float] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._remember(psutil.Process(process.pid))
        self._thread = threading.Thread(target=self._observe, daemon=True)
        self._thread.start()

    def _remember(self, process):
        try:
            created = process.create_time()
        except psutil.NoSuchProcess:
            return
        with self._lock:
            self.identities[process.pid] = created

    def _live(self):
        with self._lock:
            recorded = list(self.identities.items())
        live = []
        for pid, created in recorded:
            try:
                process = psutil.Process(pid)
                current_creation = process.create_time()
                if current_creation != created:
                    # On macOS create_time can shift with the system clock.
                    # A readable matching marker still proves ownership; an
                    # unreadable identity cannot prove safe cleanup.
                    if process.environ().get(_OWNER) != self.marker:
                        continue
                    self._remember(process)
                if process.status() != psutil.STATUS_ZOMBIE:
                    live.append(process)
            except psutil.NoSuchProcess:
                continue
        return live

    def refresh(self):
        for process in self._live():
            try:
                for child in process.children(recursive=True):
                    self._remember(child)
            except psutil.NoSuchProcess:
                pass

    def discover_detached(self):
        # Detached engines/jobs inherit this random marker; records survive
        # reparenting. No environment values are logged or exported.
        for process in psutil.process_iter():
            try:
                if process.environ().get(_OWNER) == self.marker:
                    self._remember(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _observe(self):
        while not self._stop.wait(0.1):
            try:
                self.refresh()
            except psutil.Error:
                # Rechecked synchronously and fail-closed by close().
                pass

    @staticmethod
    def _signal(process, *, kill=False):
        try:
            process.kill() if kill else process.terminate()
        except psutil.NoSuchProcess:
            pass

    def close(self, grace: float) -> dict:
        self.refresh()
        self.discover_detached()
        if self.process.poll() is None:
            self.process.terminate()  # main.py's real Controller shutdown owner
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.refresh()
            if not self._live():
                break
            time.sleep(0.05)
        self.discover_detached()
        for process in self._live():
            self._signal(process)
        _, alive = psutil.wait_procs(self._live(), timeout=1)
        for process in alive:
            # _live rechecks create_time before every forced-signal batch.
            if process in self._live():
                self._signal(process, kill=True)
        psutil.wait_procs(self._live(), timeout=2)
        self.process.wait(timeout=3)
        self._stop.set()
        self._thread.join(timeout=2)
        self.discover_detached()
        if self._live():
            raise TrialError("owned_process_cleanup_unconfirmed")
        return {"owned_processes_stopped": True, "observed_process_count": len(self.identities)}


class FullRuntimeTrial:
    """A fresh home, the normal service, IPC bootstrap, and ordinary CLI Run.

    source_root is for source-checkout tests only. Omit it to exercise the pinned
    installed wheel. No source package subsets are copied into the task.
    """

    def __init__(self, workdir: Path, state: Path, output: Path, profile: Profile, *,
                 task_environment: Mapping[str, str] | None = None, source_root: Path | None = None):
        validate_layout(workdir, state, output)
        self.workdir, self.state, self.output = (p.resolve() for p in (workdir, state, output))
        self.profile = profile
        self.source_root = source_root.resolve() if source_root else None
        self.state.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            self.output.mkdir(parents=True, exist_ok=False, mode=0o700)
        except OSError:
            self.state.rmdir()  # only our just-created, empty reservation
            raise
        self.marker = secrets.token_hex(24)
        self.home = self.state / "home"
        self.home.mkdir()
        self.avibe_home = self.state / "avibe"
        self.env = self._environment(task_environment)
        self.profile_path = self.state / "profile.json"
        self.profile_path.write_text(json.dumps(profile.payload()), encoding="utf-8")
        self._service = None
        self._processes = None
        self._log = None
        self._socket_dir = None
        self._key = None
        self._result = {"outcome": "not_started", "verifier_reward": None, "usage": None,
                        "accounting_complete": False, "cleanup": None, "run_id": None, "session_id": None}

    def _environment(self, task_environment):
        if task_environment is None:
            env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
                   "HOME": str(self.home), "USERPROFILE": str(self.home),
                   "XDG_CONFIG_HOME": str(self.home / ".config"),
                   "XDG_DATA_HOME": str(self.home / ".local/share"),
                   "XDG_CACHE_HOME": str(self.home / ".cache"), "TMPDIR": str(self.state / "tmp")}
            (self.state / "tmp").mkdir()
        else:
            # This mapping is the sandbox image's environment, not os.environ
            # of the user's machine. Retain task toolchains and the real HOME.
            env = dict(task_environment)
        for name in list(env):
            upper = name.upper()
            if (upper.startswith(("AVIBE_", "VIBE_", "ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_"))
                    or name == self.profile.credential_env):
                env.pop(name)
        env.update({
            "AVIBE_HOME": str(self.avibe_home), _OWNER: self.marker,
            "AVIBE_WEB_UI_SUPERVISOR": "terminal-bench-no-ui",
            "VIBE_STARTUP_DEPENDENCY_RECONCILE": "0",
            "VIBE_INSTALL_SKIP_ASKILL": "1", "VIBE_ASKILL_AUTO_UPDATE": "0",
            "VIBE_SHOW_RUNTIME_OFFLINE": "1" if self.profile.offline else "0",
            "VIBE_TMUX_OFFLINE": "1" if self.profile.offline else "0",
            "VIBE_GIT_OFFLINE": "1" if self.profile.offline else "0",
            "VIBE_MODEL_HUB_ENGINE_OFFLINE": "1" if self.profile.offline else "0",
            "VIBE_MODEL_HUB_ENABLED": "1", "PYTHONUNBUFFERED": "1",
        })
        # Local metadata avoids catalog/network drift; it does not change model
        # requests, Agent prompts, tools, Harness or context management.
        catalog = self.state / "empty-models-dev.json"
        catalog.write_text("{}", encoding="utf-8")
        env["AVIBE_MODELS_DEV_URL"] = catalog.as_uri()
        # Use the installed product's bundled backend catalog, resolved in child.
        return env

    def _command(self, operation: str, *args: str):
        if operation == "cli":
            code = "import runpy; runpy.run_module('vibe',run_name='__main__')"
            command_args = list(args)
        else:
            code = "import runpy,sys; runpy.run_path(sys.argv.pop(1),run_name='__main__')"
            command_args = [str(_CONTROL), operation, *args]
        if self.source_root:
            code = "import sys; sys.path.insert(0,sys.argv.pop(1)); " + code
            command_args.insert(0, str(self.source_root))
        return [sys.executable, "-I", "-c", code, *command_args]

    def _call(self, operation: str, *args: str, timeout=None, stdin=None):
        try:
            proc = subprocess.run(self._command(operation, *args), cwd=self.state, env=self.env,
                                  input=stdin, text=True, encoding="utf-8", capture_output=True,
                                  timeout=timeout or self.profile.setup_seconds)
        except subprocess.TimeoutExpired:
            raise TrialError(f"{operation}_helper_timeout") from None
        if proc.returncode:
            raise TrialError(f"{operation}_helper_failed_{proc.returncode}")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise TrialError(f"{operation}_helper_invalid_json") from None

    def _call_run_show(self, run_id: str, *, timeout: float = 15):
        """Accept a valid durable Run payload even when CLI exits nonzero.

        ``vibe runs show`` returns a meaningful failed Run alongside a nonzero
        exit code. That is a product outcome, not helper infrastructure.
        """
        argv = self._command("cli", "runs", "show", run_id)
        try:
            proc = subprocess.run(argv, cwd=self.state, env=self.env,
                                  text=True, encoding="utf-8", capture_output=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TrialError("cli_helper_timeout") from None
        try:
            payload = json.loads(proc.stdout)
            row = payload.get("run") if isinstance(payload, dict) else None
            if not isinstance(row, dict) or row.get("id") not in {None, run_id}:
                raise ValueError
            return payload
        except (TypeError, ValueError, json.JSONDecodeError):
            if proc.returncode:
                raise TrialError(f"cli_helper_failed_{proc.returncode}") from None
            raise TrialError("cli_helper_invalid_run_payload") from None

    def start(self):
        if os.name != "nt":
            # UNIX socket names have a ~104-byte bound on macOS. Follow the
            # test/operator TMPDIR (HD1 here), not a long nested trial path.
            self._socket_dir = tempfile.TemporaryDirectory(prefix="tb-ipc-")
            self.env["VIBE_INTERNAL_DISPATCH_SOCKET"] = str(Path(self._socket_dir.name) / "dispatch.sock")
        self._result["installation"] = self._call("initialize", "--workdir", str(self.workdir))
        self._result["installation"].update(self._call("versions"))
        self.env["AVIBE_BACKEND_MODEL_CATALOG_URL"] = Path(self._result["installation"]["backend_catalog"]).as_uri()
        self._log = (self.state / "service.log").open("w", encoding="utf-8")
        self._service = subprocess.Popen(self._command("serve"), cwd=self.state, env=self.env,
                                         stdout=self._log, stderr=self._log, start_new_session=True)
        self._processes = OwnedProcesses(self._service, self.marker)
        deadline = time.monotonic() + self.profile.setup_seconds
        while time.monotonic() < deadline:
            if self._service.poll() is not None:
                raise TrialError("service_exited_before_ready")
            health = self._call("health", timeout=min(10, self.profile.setup_seconds))
            if health.get("ready"):
                self._result["controller_health"] = health["identity"]
                self._result["service_pid"] = self._service.pid
                self._result["outcome"] = "ready"
                return
            time.sleep(self.profile.poll_seconds)
        raise TrialError("controller_readiness_timeout")

    def execute(self, instruction: str, *, key: str | None):
        if self._result["outcome"] != "ready":
            raise TrialError("service_not_ready")
        if not key:
            self._result.update(outcome="blocked", blocker="model_source_credential_required")
            return
        self._key = key.strip()
        if not self._key:
            self._result.update(outcome="blocked", blocker="model_source_credential_required")
            return
        self._result["source"] = self._call("configure", "--profile", str(self.profile_path), stdin=self._key)
        started_at = time.monotonic()
        self._call("cli", "agent", "update", "vibey", "--model", self.profile.model,
                   "--reasoning-effort", self.profile.reasoning_effort)
        instruction_path = self.state / "instruction.txt"
        instruction_path.write_text(instruction, encoding="utf-8")
        # Explicit asynchronous creation preserves stable ids before waiting.
        # Expiry of a CLI waiter does not cancel an Agent turn.
        accepted = self._call("cli", "agent", "run", "--agent", "vibey", "--cwd", str(self.workdir),
                              "--message-file", str(instruction_path), "--no-callback")
        run_id, session_id = accepted["run"]["id"], accepted["run"]["session_id"]
        self._result.update(run_id=run_id, session_id=session_id)
        deadline = time.monotonic() + self.profile.wall_seconds
        terminal_row = None
        quiet_observations = 0
        while time.monotonic() < deadline:
            row = self._call_run_show(run_id)["run"]
            if row["status"] in _TERMINAL:
                terminal_row = row
                # A Run's final reply can hand over to a Watch, scheduled Task
                # or subagent. They are real product work, not noise to discard
                # at the first terminal. This home belongs only to this trial.
                status = self._call("cli", "harness", "status", "--global", timeout=15)
                if (not status.get("controller", {}).get("available")
                        or any(status.get("truncated", {}).values())):
                    raise TrialError("harness_quiescence_unconfirmed")
                busy = bool(status.get("runs") or status.get("watches") or status.get("tasks"))
                busy = busy or any(item.get("state") == "active" for item in status.get("runtime_agents", []))
                quiet_observations = 0 if busy else quiet_observations + 1
                if quiet_observations >= 2:
                    self._result.update(outcome=terminal_row["status"], final_text=terminal_row.get("result_text"),
                                        run_error=terminal_row.get("error"), harness_quiescent=True)
                    break
            time.sleep(self.profile.poll_seconds)
        else:
            self._result["outcome"] = "time_limit"
            try:
                cancelled = self._call("cli", "runs", "cancel", run_id, timeout=15)
                self._result["cancel_requested"] = cancelled.get("ok") is True
            except TrialError as exc:
                # A concurrent terminal or unavailable IPC must not skip the
                # final service/tree teardown, nor erase the deadline outcome.
                self._result["cancel_requested"] = False
                self._result["cancel_error"] = str(exc)
            # Cancellation is asynchronous; a receipt alone is not quiescence.
            cancel_deadline = time.monotonic() + self.profile.cleanup_seconds
            self._result["run_settled_after_cancel"] = False
            while time.monotonic() < cancel_deadline:
                try:
                    row = self._call_run_show(run_id)["run"]
                except TrialError:
                    break
                if row["status"] in _TERMINAL:
                    self._result["run_settled_after_cancel"] = True
                    break
                time.sleep(self.profile.poll_seconds)
            # Service shutdown and exact-owned-process reaping still follow;
            # even a terminal Run may own Watch-backed work after its turn.
        self._result["elapsed_seconds"] = time.monotonic() - started_at
        self._result["outcome_scope"] = "initial_run_after_fresh_home_harness_quiescence_or_deadline"
        self._result["final_text_scope"] = "initial_run_result"
        # Entire fresh-home accounting, including bootstrap calls, is identified
        # as such. Never claim completeness for interrupted/provider-failed calls.
        try:
            self._result["usage"] = self._call("usage", timeout=15)
        except TrialError:
            self._result["usage_unavailable"] = True

    def close(self):
        # Capture only allowlisted response shapes while the normal CLI can
        # still reach this trial's service. Failure is explicit but never
        # changes the measured outcome or prevents owned-process cleanup.
        session_id = self._result.get("session_id")
        self._result["diagnostics"] = {"schema_version": 1, "status": "no_session", "raw_text_exported": False}
        if session_id and self._service and self._service.poll() is None:
            try:
                payload = self._call("cli", "data", "query", "--sql", response_query(session_id), "--limit", "9", timeout=10)
                self._result["diagnostics"] = project_responses(payload)
            except Exception:
                self._result["diagnostics"] = {"schema_version": 1, "status": "unavailable", "raw_text_exported": False}
        elif session_id:
            self._result["diagnostics"]["status"] = "service_unavailable"
        try:
            if self._processes:
                self._result["cleanup"] = self._processes.close(self.profile.cleanup_seconds)
        finally:
            if self._log:
                self._log.close()
        if self._socket_dir:
            self._socket_dir.cleanup()
        self._result["profile"] = self.profile.payload()
        self._result["execution_path"] = "normal_service_model_hub_ipc_and_cli_run"
        self._result["runtime_state_exported"] = False
        self._result.setdefault("installation", {})["mode"] = "source_checkout" if self.source_root else "installed_package"
        self._result["accounting_scope"] = "fresh_home_reported_usage_including_bootstrap_not_actual_bill"
        result = scrub(self._result, self._key)
        (self.output / "execution.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException as exc:
            self._result.update(failure_details(exc))
            self.close()
            raise

    def __exit__(self, exc_type, exc, _tb):
        if exc is not None:
            self._result.update(failure_details(exc))
        self.close()


def _interrupt_trial(signum, frame):
    # A second TERM must not interrupt cleanup. The outer container supervisor
    # can still use KILL if its independent final grace deadline expires.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workdir", "state", "output", "profile", "instruction"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path,
                        help="Private per-trial input; consumed and unlinked before service startup.")
    parser.add_argument("--sandbox-environment", action="store_true",
                        help="Use only inside an externally isolated task container; preserve its HOME/toolchains.")
    args = parser.parse_args(argv)
    profile = Profile.parse(json.loads(args.profile.read_text(encoding="utf-8")))
    if args.credential_file is not None:
        from .credential import consume
        try:
            key = consume(args.credential_file)
        except Exception:
            summary = json.dumps(failure_details(TrialError("credential_input_refused")))
            print(summary)
            print(summary, file=sys.stderr)
            return 2
    else:
        # Direct invocation may retain its named env mode. The Harbor Docker
        # adapter must use credential-file instead: exec env leaks into argv.
        key = os.environ.get(profile.credential_env)
    trial = FullRuntimeTrial(args.workdir, args.state, args.output, profile,
                             task_environment=os.environ if args.sandbox_environment else None)
    previous_term = signal.signal(signal.SIGTERM, _interrupt_trial)
    try:
        with trial:
            trial.execute(args.instruction.read_text(encoding="utf-8"), key=key)
    except (Exception, KeyboardInterrupt) as exc:
        summary = json.dumps(failure_details(exc))
        print(summary)
        print(summary, file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    print(json.dumps({"outcome": trial._result["outcome"], "report": str(trial.output / "execution.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
