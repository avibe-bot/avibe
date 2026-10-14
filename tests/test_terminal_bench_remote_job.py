"""Remote owner fences and cleanup failure classes; no provider calls."""
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl")
from scripts.terminal_bench import remote_job as job


@pytest.fixture(autouse=True)
def host_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "HOST_RESERVATION", tmp_path / "host-reservation")
    monkeypatch.setenv("SERVICE_RESULT", "success")


def config(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('artifacts=["/app/result.txt"]\n')
    data = {"job_name": "one-job", "jobs_dir": str(tmp_path / "jobs"), "n_attempts": 1, "n_concurrent_trials": 1,
            "agents": [{"name": "nop", "override_timeout_sec": 2}], "tasks": [{"path": str(task)}],
            "environment": {"type": "docker", "delete": True}}
    p = tmp_path / "config.json"
    job.save(p, data)
    return p


def prepared(tmp_path, seconds=5):
    p = config(tmp_path)
    root = tmp_path / "runs" / "one"
    job.prepare(p, root, seconds, "BENCHMARK_API_KEY")
    (root / "credential").mkdir(mode=0o700)
    key = root / "credential/provider.key"
    key.write_text("not-real-secret")
    key.chmod(0o600)
    job.save(root / "ownership.json", {"trial_name": "task__nonce", "trial_id": "trial-1", "job_id": "job-1", "projects": ["task_nonce_env"]})
    return root


def complete(root):
    m = job.load(root / "manifest.json")
    trial = Path(m["job_dir"]) / "task__nonce"
    trial.mkdir(parents=True, exist_ok=True)
    job.save(trial / "result.json", {"id": "trial-1", "trial_name": "task__nonce", "finished_at": "2026-10-14T00:00:00Z"})
    job.save(trial.parent / "result.json", {"id": "job-1", "finished_at": "2026-10-14T00:00:00Z", "n_total_trials": 1,
            "stats": {"n_completed_trials": 1, "n_running_trials": 0, "n_pending_trials": 0}})


class Docker:
    def __init__(self):
        self.containers = [{"Id": "a" * 64, "Config": {"Labels": {"com.docker.compose.project": "task_nonce_env"}}},
                           {"Id": "b" * 64, "Config": {"Labels": {"com.docker.compose.project": "bystander"}}}]
        self.networks = [{"Id": "owned-net", "Labels": {"com.docker.compose.project": "task_nonce_env"}},
                         {"Id": "bystander-net", "Labels": {"com.docker.compose.project": "bystander"}}]
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1:3] == ["ps", "-aq"]:
            return "\n".join(c["Id"] for c in self.containers)
        if argv[1] == "inspect":
            return json.dumps(self.containers)
        if argv[1:3] == ["rm", "-f"]:
            self.containers[:] = [c for c in self.containers if c["Id"] != argv[-1]]
            return ""
        if argv[1:3] == ["network", "ls"]:
            return "\n".join(n["Id"] for n in self.networks)
        if argv[1:3] == ["network", "inspect"]:
            return json.dumps(self.networks)
        if argv[1:3] == ["network", "rm"]:
            self.networks[:] = [n for n in self.networks if n["Id"] != argv[-1]]
            return ""
        if argv[1] == "cp":
            return ""
        raise AssertionError(argv)


def test_preflight_rejects_paths_and_budget_before_any_job(tmp_path):
    p = config(tmp_path)
    d = job.load(p)
    for path in ["task", "/Volumes/HD1/absent-task"]:
        d["tasks"][0]["path"] = path
        job.save(p, d)
        with pytest.raises((ValueError, FileNotFoundError)):
            job.prepare(p, tmp_path / "state", 2)
        assert not job.HOST_RESERVATION.exists()
    d["tasks"][0]["path"] = str(tmp_path / "task")
    d["n_attempts"] = 2
    job.save(p, d)
    with pytest.raises(ValueError):
        job.validate(p)


def test_profile_credential_name_is_derived_and_mismatch_refused(tmp_path):
    import hashlib
    p = config(tmp_path)
    wheel = tmp_path / "avibe.whl"
    wheel.write_bytes(b"fixture-only-wheel")
    d = job.load(p)
    d["agents"] = [{"import_path": "scripts.terminal_bench.harbor_agent:FullAvibeAgent", "model_name": "fixture",
        "override_timeout_sec": 30, "kwargs": {"wheel_path": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "profile": {"model": "fixture", "base_url": "http://127.0.0.1:9/v1", "credential_env": "ACTUAL_PROVIDER_KEY"}}}]
    job.save(p, d)
    assert job.validate(p)[3] == "ACTUAL_PROVIDER_KEY"
    with pytest.raises(ValueError, match="credential variable"):
        job.validate(p, "WRONG_KEY")
    assert not job.HOST_RESERVATION.exists()


def test_lock_is_host_wide_and_destination_claim_is_permanent(tmp_path):
    root = prepared(tmp_path)
    other = tmp_path / "totally-different-parent" / "second"
    with pytest.raises(FileExistsError):
        job.prepare(tmp_path / "config.json", other, 2)
    assert not other.exists()
    job.release(root)
    with pytest.raises(FileExistsError):
        job.prepare(tmp_path / "config.json", other, 2)
    assert not job.HOST_RESERVATION.exists()


@pytest.mark.parametrize("broken_result", ["{truncated", "[]", "null", '{"finished_at":"not-a-trial"}'])
def test_bad_results_never_skip_exact_owned_cleanup(tmp_path, monkeypatch, broken_result):
    root = prepared(tmp_path)
    manifest = job.load(root / "manifest.json")
    jobdir = Path(manifest["job_dir"])
    jobdir.mkdir(parents=True)
    (jobdir / "result.json").write_text(broken_result)
    job.save(root / "lifecycle.json", {"state": "completed", "exit_code": 0})
    docker = Docker()
    monkeypatch.setattr(job, "command", docker)
    receipt = job.finalize(root)
    assert receipt["state"] == "missing_terminal_result"
    assert receipt["harbor_finished"] is False
    assert receipt["cleanup"]["confirmed"] is True
    assert [c["Id"] for c in docker.containers] == ["b" * 64]
    assert [n["Id"] for n in docker.networks] == ["bystander-net"]
    assert not job.HOST_RESERVATION.exists()
    assert not (root / "credential/provider.key").exists()


def test_network_only_and_bad_artifact_metadata_are_cleaned(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    (tmp_path / "task/task.toml").write_text("broken = [")
    docker = Docker()
    docker.containers = []
    monkeypatch.setattr(job, "command", docker)
    receipt = job.finalize(root)
    assert receipt["cleanup"]["confirmed"]
    assert docker.networks[0]["Id"] == "bystander-net"
    assert receipt["artifacts"] == [{"status": "metadata_unavailable"}]


def test_cleanup_failure_retains_reservation_and_receipt(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    monkeypatch.setattr(job, "command", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("docker", 1)))
    receipt = job.finalize(root)
    assert receipt["cleanup"]["confirmed"] is False
    assert job.HOST_RESERVATION.exists()
    assert not (root / "credential/provider.key").exists()
    assert receipt["finished_at"]


def test_completion_requires_exact_trial_and_no_pending_work(tmp_path):
    root = prepared(tmp_path)
    m, o = job.load(root / "manifest.json"), job.load(root / "ownership.json")
    complete(root)
    assert job.terminal_evidence(m, o)
    assert not job.terminal_evidence(m, {**o, "trial_id": "some-other-trial"})
    p = Path(m["job_dir"]) / "result.json"
    d = job.load(p); d["stats"]["n_running_trials"] = 1; job.save(p, d)
    assert not job.terminal_evidence(m, o)


@pytest.mark.parametrize("chunk_size", [1, 3, 4096])
def test_stream_redaction_handles_split_keys(tmp_path, chunk_size):
    class Chunks(io.StringIO):
        def read(self, n):
            return super().read(min(n, chunk_size))
    p = tmp_path / "log"
    job.redact_stream(Chunks("before-secret-非ASCII-after\n"), p, "secret-非ASCII")
    assert p.read_text() == "before-[REDACTED]-after\n"
    assert p.stat().st_mode & 0o777 == 0o600


@pytest.mark.allow_foreign_signals("Supervisor owns this test child process group")
@pytest.mark.reap_processes
@pytest.mark.parametrize("mode,expected", [("ok", "completed"), ("missing", "missing_terminal_result"), ("crash", "harbor_error"), ("slow", "time_limit")])
def test_worker_never_releases_slot_before_post_stop(tmp_path, monkeypatch, mode, expected):
    root = prepared(tmp_path, seconds=1 if mode == "slow" else 10)
    program = tmp_path / "child-python"
    program.write_text(f'''#!{sys.executable}
import sys,time
print('not-real-secret',flush=True)
if {mode!r}=='slow': time.sleep(30)
if {mode!r}=='crash': sys.exit(3)
''')
    program.chmod(0o700)
    if mode == "ok":
        complete(root)
    monkeypatch.setattr(job, "sys", SimpleNamespace(executable=str(program)))
    before = signal.getsignal(signal.SIGTERM)
    code = job.worker(root)
    assert not (root / "receipt.json").exists()
    assert job.HOST_RESERVATION.exists()
    assert signal.getsignal(signal.SIGTERM) == before
    monkeypatch.setattr(job, "command", Docker())
    receipt = job.finalize(root)
    assert receipt["state"] == expected
    assert code == (0 if mode in {"ok", "missing"} else 1)
    assert "not-real-secret" not in (root / "harbor.log").read_text()
    assert not job.HOST_RESERVATION.exists()


def test_finalize_refuses_live_owner_and_status_does_not_wait_on_dead_unit(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    monkeypatch.delenv("SERVICE_RESULT")
    with pytest.raises(ValueError):
        job.finalize(root)
    monkeypatch.setattr(job, "unit_state", lambda unit: {"ActiveState": "failed"})
    result, code = job.status(root)
    assert code == 1 and result["cleanup_confirmed"] is False


def test_lost_launch_ack_fences_unit_without_publishing_clean_receipt(tmp_path, monkeypatch):
    p = config(tmp_path)
    private = tmp_path / "credential"; private.mkdir(mode=0o700)
    key = private / "provider.key"; key.write_text("KEY-SENTINEL"); key.chmod(0o600)
    calls = []
    def manager(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "systemd-run":
            raise subprocess.TimeoutExpired("systemd-run", 10)
        if argv[:2] == ["systemctl", "show"]:
            return "LoadState=loaded\nActiveState=active\n"
        return ""
    monkeypatch.setattr(job, "command", manager)
    root = tmp_path / "runs/one"
    with pytest.raises(subprocess.TimeoutExpired):
        job.submit(p, root, key, 5)
    assert not (root / "receipt.json").exists()
    assert job.HOST_RESERVATION.exists()
    assert calls[-1][:3] == ["systemctl", "stop", "--no-block"]
    assert "KEY-SENTINEL" not in json.dumps(calls)
    assert any(a.startswith("--property=ExecStopPost=") for a in calls[0])


def test_artifact_recovery_stops_at_its_total_budget(tmp_path, monkeypatch):
    root = prepared(tmp_path)
    (tmp_path / "task/task.toml").write_text('artifacts=' + json.dumps(['/app/f' + str(i) for i in range(32)]))
    clock = [0.0]
    monkeypatch.setattr(job.time, "monotonic", lambda: clock[0])
    calls = []
    def copy(argv, timeout):
        calls.append(argv); clock[0] += timeout
        raise subprocess.TimeoutExpired("docker cp", timeout)
    monkeypatch.setattr(job, "command", copy)
    result = job.copy_artifacts(root, job.load(root / "manifest.json"), [{"Id": "a" * 64}], 8)
    assert clock[0] <= 8 and len(calls) <= 3
    assert result[-1]["status"] == "recovery_budget_exhausted"
