"""Single-job Harbor lifetime on a dedicated Linux evaluation host.

systemd owns execution independently of SSH. Only its post-stop phase finalizes
artifacts and releases ownership; Avibe Harness observes the durable receipt.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # Product tests still support Python 3.10.
    import tomli as tomllib

from .credential import consume, metadata

HOST_RESERVATION = Path("/run/lock/avibe-benchmark.active.json")
FINALIZE_SECONDS = 45


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evidence(path):
    try:
        value = load(path)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file, indent=2)
        file.write("\n")
    temporary.replace(path)


def command(argv, *, timeout=10):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True).stdout


def absolute(path, *, exists=True):
    p = Path(path)
    if any(char in str(p) for char in "%$\n\r\x00"):
        raise ValueError("systemd specifiers and variable expansions are not supported in paths")
    if not p.is_absolute():
        raise ValueError("absolute execution paths required; relocate explicitly before submission")
    resolved = p.resolve(strict=exists)
    if any(char in str(resolved) for char in "%$\n\r\x00"):
        raise ValueError("resolved path contains a systemd expansion")
    return resolved


def validate(config_path, credential_env=None):
    """No job creation, Docker mutation, credential consumption or model call."""
    config = load(config_path)
    if config.get("n_attempts", 1) != 1 or config.get("n_concurrent_trials", 1) != 1:
        raise ValueError("one attempt and concurrency one required")
    if config.get("retry", {}).get("max_retries", 0) != 0 or any(config.get(k) for k in ("source_jobs", "datasets", "user_agent")):
        raise ValueError("retry, dataset, regrade and simulated-user jobs unsupported")
    if len(config.get("agents", [])) != 1 or len(config.get("tasks", [])) != 1:
        raise ValueError("exactly one agent and local task required")
    name = config.get("job_name", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
        raise ValueError("explicit safe job name required")
    env = config.get("environment", {})
    if env.get("type", "docker") != "docker" or env.get("delete", True) is not True or env.get("import_path"):
        raise ValueError("isolated Docker with delete=true required")
    task = absolute(config["tasks"][0]["path"])
    task_config = tomllib.loads((task / "task.toml").read_text())
    if task_config.get("steps"):
        raise ValueError("single-step tasks only; verifier ownership is pinned to Harbor 0.24")
    for overlay in env.get("extra_docker_compose", []):
        absolute(overlay)
    agent = config["agents"][0]
    seconds = agent.get("override_timeout_sec")
    if type(seconds) not in (int, float) or not 0 < seconds <= 1800:
        raise ValueError("explicit agent limit no greater than 1800 seconds required")
    kwargs = agent.get("kwargs", {})
    if agent.get("import_path") == "scripts.terminal_bench.harbor_agent:FullAvibeAgent":
        from .profile import Profile
        profile = Profile.parse(kwargs["profile"])
        if profile.model != agent.get("model_name"):
            raise ValueError("profile model mismatch")
        if credential_env is not None and credential_env != profile.credential_env:
            raise ValueError("credential variable must match the full-runtime profile")
        credential_env = profile.credential_env
        wheel = absolute(kwargs["wheel_path"])
        with wheel.open("rb") as file:
            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != kwargs["wheel_sha256"]:
                raise ValueError("wheel hash mismatch")
    credential_env = credential_env or "BENCHMARK_API_KEY"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", credential_env):
        raise ValueError("invalid credential variable")
    jobs = absolute(config["jobs_dir"], exists=False)
    if (jobs / name).exists():
        raise ValueError("fresh job destination required")
    return config, jobs / name, task, credential_env


def release(root):
    if HOST_RESERVATION.is_file() and HOST_RESERVATION.read_text() == str(Path(root).resolve()):
        HOST_RESERVATION.unlink()


def prepare(config_path, root, wall_seconds, credential_env=None):
    config, job_dir, task, credential_env = validate(config_path, credential_env)
    root = absolute(root, exists=False)
    source_root = absolute(Path(__file__).resolve().parents[2])
    absolute(sys.executable)  # Reject manager expansion in the interpreter path too.
    if not 0 < wall_seconds <= 7200:
        raise ValueError("invalid hard wall cap")
    if any(root == p or root in p.parents or p in root.parents for p in (task, job_dir)):
        raise ValueError("supervisor state must be disjoint from task and job")
    root.parent.mkdir(parents=True, exist_ok=True)
    HOST_RESERVATION.parent.mkdir(parents=True, exist_ok=True)
    with HOST_RESERVATION.open("x", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(str(root))
    try:
        root.mkdir(mode=0o700)
        job_dir.parent.mkdir(parents=True, exist_ok=True)
        # Keep this per-destination claim permanently, including failed starts.
        with (job_dir.parent / ("." + job_dir.name + ".claimed")).open("x", encoding="utf-8") as file:
            file.write(str(root))
        manifest = {"schema_version": 1, "unit": "avibe-benchmark-" + uuid.uuid4().hex,
                    "job_dir": str(job_dir), "task": str(task), "wall_seconds": wall_seconds,
                    "credential_env": credential_env, "created_at": time.time(),
                    "config_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
                    "source_root": str(source_root)}
        save(root / "config.json", config)
        save(root / "manifest.json", manifest)
        save(root / "lifecycle.json", {"state": "prepared"})
        return manifest
    except Exception:
        release(root)
        raise


def terminal_evidence(manifest, ownership):
    """A finished job alone is insufficient: the one exact trial must be final."""
    job_dir = Path(manifest["job_dir"])
    result = evidence(job_dir / "result.json")
    name = ownership.get("trial_name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", name):
        return False
    trial = evidence(job_dir / name / "result.json")
    if not result or not trial or not result.get("finished_at") or not trial.get("finished_at"):
        return False
    stats = result.get("stats") or {}
    if not isinstance(stats, dict):
        return False
    return (result.get("n_total_trials") == 1 and stats.get("n_completed_trials") == 1
            and stats.get("n_running_trials") == 0 and stats.get("n_pending_trials") == 0
            and result.get("id") == ownership.get("job_id")
            and trial.get("id") == ownership.get("trial_id") and trial.get("trial_name") == name)


def docker_state(projects, run=command):
    ids = run(["docker", "ps", "-aq"]).split()
    containers = json.loads(run(["docker", "inspect", *ids])) if ids else []
    ids = run(["docker", "network", "ls", "-q"]).split()
    networks = json.loads(run(["docker", "network", "inspect", *ids])) if ids else []
    owned = [c for c in containers if (c.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") in projects]
    nets = [n for n in networks if (n.get("Labels") or {}).get("com.docker.compose.project") in projects]
    return owned, nets


def archive_artifact(container_id, source, destination, timeout):
    """Keep untrusted Docker tar bytes opaque; never extract onto the host.

    Separate exclusive files prevent overlapping artifact paths or symlinks in
    one artifact from redirecting a later host write. Bound both bytes and time.
    """
    deadline = time.monotonic() + timeout
    with destination.open("xb") as file:
        os.fchmod(file.fileno(), 0o600)
        child = subprocess.Popen(["docker", "cp", f"{container_id}:{source}", "-"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                size = 0
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        raise subprocess.TimeoutExpired("artifact archive", timeout)
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise OSError("artifact archive exceeds byte budget")
                    file.write(chunk)
                child.wait(timeout=max(.1, deadline - time.monotonic()))
                if child.returncode:
                    raise subprocess.CalledProcessError(child.returncode, "artifact archive")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()


def copy_artifacts(root, manifest, containers, deadline):
    records = []
    try:
        declared = tomllib.loads((Path(manifest["task"]) / "task.toml").read_text()).get("artifacts", [])
    except (OSError, ValueError):
        return [{"status": "metadata_unavailable"}]
    for container in containers:
        for index, path in enumerate(declared[:32]):
            if time.monotonic() >= deadline:
                return [*records, {"status": "recovery_budget_exhausted"}]
            if not isinstance(path, str):
                continue
            source = Path(path)
            if not source.is_absolute() or ".." in source.parts or len(source.parts) < 3:
                continue
            if source.parts[1] in {"installed-agent", "proc", "sys", "dev", "etc", "root", "run", "logs"}:
                continue
            destination = root / "recovered-artifacts" / container["Id"][:12] / f"{index:02d}.tar"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                archive_artifact(container["Id"], source, destination, min(3, max(.1, deadline - time.monotonic())))
                status = "archived"
            except (subprocess.SubprocessError, OSError):
                status = "unavailable"
            records.append({"path": path, "status": status, "archive": str(destination.relative_to(root)),
                            "extracted_on_host": False})
    return records


def finalize(root, *, never_started=False):
    # Publishing a cleanup receipt while the unit can still launch work would
    # release the slot early. Only ExecStopPost or a proven absent unit may do so.
    if not never_started and "SERVICE_RESULT" not in os.environ:
        raise ValueError("finalization requires the manager post-stop fence")
    root = Path(root)
    with (root / "finalize.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = evidence(root / "receipt.json")
        if prior and prior.get("finished_at"):
            return prior
        manifest = load(root / "manifest.json")
        lifecycle = evidence(root / "lifecycle.json") or {}
        ownership = evidence(root / "ownership.json") or {}
        finished = terminal_evidence(manifest, ownership)
        manager = os.environ.get("SERVICE_RESULT", "not-started")
        reason = lifecycle.get("state", "supervisor_lost")
        if never_started:
            reason = "submit_failed"
        elif manager == "timeout":
            reason = "time_limit"
        elif reason not in {"completed", "harbor_error", "time_limit", "interrupted", "launch_failed"}:
            reason = "supervisor_lost"
        if reason == "completed" and not finished:
            reason = "missing_terminal_result"
        receipt = {"schema_version": 1, "state": reason, "harbor_exit_code": lifecycle.get("exit_code"),
                   "harbor_finished": finished, "harbor_result_path": str(Path(manifest["job_dir"]) / "result.json"),
                   "wall_seconds": manifest["wall_seconds"], "finished_at": None,
                   "cleanup": {"confirmed": False, "unit_stopped": True}, "artifacts": []}
        save(root / "receipt.json", receipt)  # Even a killed finalizer leaves an honest incomplete receipt.
        deadline = time.monotonic() + FINALIZE_SECONDS
        def bounded(argv):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("cleanup budget exhausted")
            return command(argv, timeout=min(5, left))
        projects = ownership.get("projects", [])
        errors = []
        try:
            # This journal is written by the pre-environment Harbor START hook,
            # covering networks created before any container/log mount exists.
            if not projects and (Path(manifest["job_dir"]).exists() or not never_started):
                # Without START there should be no Docker mutation; fail closed
                # if a corrupted/missing ownership journal cannot establish it.
                if not lifecycle.get("no_trial_started"):
                    raise ValueError("ownership unavailable")
            owned, networks = docker_state(projects, bounded)
            if not finished:
                try:
                    receipt["artifacts"] = copy_artifacts(root, manifest, owned, min(deadline - 15, time.monotonic() + 8))
                except (OSError, ValueError, TypeError):
                    receipt["artifacts"] = [{"status": "recovery_unavailable"}]
            for argv in (["docker", "rm", "-f", c["Id"]] for c in owned):
                try:
                    bounded(argv)
                except (subprocess.SubprocessError, OSError):
                    errors.append("container_removal_failed")
            for net in networks:
                try:
                    bounded(["docker", "network", "rm", net["Id"]])
                except (subprocess.SubprocessError, OSError):
                    errors.append("network_removal_failed")
            remaining, nets = docker_state(projects, bounded)
            receipt["cleanup"].update(confirmed=not (remaining or nets or errors),
                                      removed_container_ids=[c["Id"] for c in owned], owned_projects=projects,
                                      remaining_ids=[c["Id"] for c in remaining], remaining_network_ids=[n["Id"] for n in nets])
        except (subprocess.SubprocessError, OSError, ValueError, TypeError):
            errors.append("owned_cleanup_unconfirmed")
        finally:
            (root / "credential" / "provider.key").unlink(missing_ok=True)
            if errors:
                receipt["cleanup"]["errors"] = errors
            receipt["finished_at"] = time.time()
            save(root / "receipt.json", receipt)
            if receipt["cleanup"]["confirmed"]:
                release(root)
        return receipt


def redact_stream(pipe, output, secret):
    """Keep a look-behind so split stdout chunks cannot expose a credential."""
    pending, written = "", 0
    with output.open("w", encoding="utf-8") as file:
        os.chmod(output, 0o600)
        while True:
            chunk = pipe.read(4096)
            pending += chunk
            end = len(pending) if not chunk else max(0, len(pending) - len(secret))
            if secret:
                start = pending.find(secret, max(0, end - len(secret)))
                if start >= 0 and start < end < start + len(secret):
                    end = start
            text = pending[:end].replace(secret, "[REDACTED]") if secret else pending[:end]
            if written < 16 * 1024 * 1024:
                file.write(text[:16 * 1024 * 1024 - written])
                file.flush()
                written += len(text)
            pending = pending[end:]
            if not chunk:
                break


def worker(root):
    root = Path(root)
    manifest = load(root / "manifest.json")
    stopped = threading.Event()
    previous_term = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    previous_int = signal.signal(signal.SIGINT, lambda *_: stopped.set())
    child = thread = None
    reason, code = "launch_failed", None
    try:
        key = consume(root / "credential" / "provider.key")
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE"}}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "home/.config"),
                   XDG_CACHE_HOME=str(root / "home/.cache"), PYTHONPATH=manifest["source_root"], HARBOR_TELEMETRY="0")
        env[manifest["credential_env"]] = key
        argv = [sys.executable, "-m", "scripts.terminal_bench.harbor_job", "--root", str(root)]
        deadline = time.monotonic() + max(0, manifest["created_at"] + manifest["wall_seconds"] - time.time())
        child = subprocess.Popen(argv, cwd=manifest["source_root"], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", start_new_session=True)
        thread = threading.Thread(target=redact_stream, args=(child.stdout, root / "harbor.log", key), daemon=True)
        thread.start()
        save(root / "lifecycle.json", {"state": "running", "pid": child.pid})
        while child.poll() is None and not stopped.is_set() and time.monotonic() < deadline:
            stopped.wait(.2)
        if child.poll() is None:
            reason = "time_limit" if time.monotonic() >= deadline else "interrupted"
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        else:
            reason = "completed" if child.returncode == 0 else "harbor_error"
        code = child.returncode
    finally:
        try:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            if thread:
                thread.join(timeout=2)
            # No receipt/slot release here. systemd kills every remaining unit
            # process before ExecStopPost performs the definitive Docker sweep.
            save(root / "lifecycle.json", {"state": reason, "exit_code": code,
                 "no_trial_started": not (root / "ownership.json").exists()})
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
    return 0 if reason == "completed" else 1


def unit_state(unit):
    data = command(["systemctl", "show", unit, "--property=LoadState,ActiveState"], timeout=5)
    return dict(line.split("=", 1) for line in data.splitlines() if "=" in line)


def submit(config, root, credential, wall, credential_env=None):
    metadata(Path(credential))  # Validate before claiming ownership or consuming.
    root = absolute(root, exists=False)
    manifest = prepare(config, root, wall, credential_env)
    try:
        secret = consume(Path(credential))
        (root / "credential").mkdir(mode=0o700)
        with (root / "credential" / "provider.key").open("x", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(secret)
        entry = [sys.executable, "-m", "scripts.terminal_bench.remote_job"]
        stop = " ".join(json.dumps(s, ensure_ascii=False) for s in [*entry, "finalize", "--root", str(root)])
        command(["systemd-run", "--quiet", "--unit=" + manifest["unit"],
                 "--property=Type=exec", "--property=KillMode=control-group",
                 "--property=WorkingDirectory=" + manifest["source_root"],
                 "--property=RuntimeMaxSec=" + str(wall), "--property=TimeoutStopSec=90",
                 "--property=ExecStopPost=" + stop,
                 "--", *entry, "worker", "--root", str(root)])
    except Exception:
        # Launch acknowledgement may have been lost AFTER the manager accepted
        # the unit. Fence that exact unit; never publish an early clean receipt.
        try:
            state = unit_state(manifest["unit"])
            if state.get("LoadState") == "not-found":
                finalize(root, never_started=True)
            else:
                command(["systemctl", "stop", "--no-block", manifest["unit"]], timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass  # Reservation deliberately retained; status reports the ambiguity.
        raise
    return {"unit": manifest["unit"], "receipt": str(root / "receipt.json")}


def status(root):
    receipt = evidence(root / "receipt.json")
    if receipt and receipt.get("finished_at"):
        return receipt, 0
    manifest = load(root / "manifest.json")
    state = unit_state(manifest["unit"])
    if state.get("ActiveState") in {"active", "activating", "deactivating"}:
        return {"state": "waiting", "unit": manifest["unit"]}, 75
    return {"state": "missing_finalization_receipt", "unit": manifest["unit"], "cleanup_confirmed": False}, 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "submit", "worker", "finalize", "status"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--credential-env")
    parser.add_argument("--wall-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        code = 0
        if args.action == "preflight":
            validate(args.config, args.credential_env)
            result = {"preflight": "passed", "model_requests": 0}
        elif args.action == "submit":
            result = submit(args.config, args.root, args.credential_file, args.wall_seconds, args.credential_env)
        elif args.action == "worker":
            return worker(args.root)
        elif args.action == "finalize":
            result = finalize(args.root)
        else:
            result, code = status(args.root)
        print(json.dumps(result))
        return code
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
