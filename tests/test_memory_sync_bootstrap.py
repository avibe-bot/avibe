from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.memory import secret_scrubber
from core.memory.artifact import EVEROS_VERSION
from core.memory.everos_insight.recorder import _scrub_text
from scripts import memory_runtime_sitecustomize as bootstrap
from scripts import memory_runtime_sync_scrubbers as scrubbers


def test_artifact_bootstrap_is_inert_without_explicit_gate(monkeypatch) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_SYNC_BOOTSTRAP", raising=False)
    monkeypatch.setattr(bootstrap.os, "kill", lambda *_args: pytest.fail("must not stop"))

    bootstrap.bootstrap()


def test_artifact_bootstrap_has_no_mutable_host_source_path() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")

    assert "SOURCE_ROOT" not in source
    assert "sys.path" not in source


def test_artifact_bootstrap_self_stops_before_scrubber_imports_everos(monkeypatch) -> None:
    events: list[str] = []
    parent_pid = 321
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_BOOTSTRAP", "1")
    monkeypatch.setenv("AVIBE_MEMORY_CHILD_ROLE", "cascade_sync")
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_NONCE", "a" * 64)
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_PID", str(parent_pid))
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME", float(8.25).hex())
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_UID", "")
    monkeypatch.setattr(bootstrap.os, "getppid", lambda: parent_pid)
    monkeypatch.setattr(bootstrap.os, "getpid", lambda: 654)
    monkeypatch.setattr(
        bootstrap.os,
        "kill",
        lambda _pid, _signal: events.append("stopped"),
    )
    monkeypatch.setattr(sys, "argv", ["-m", "everos.entrypoints.cli.main", "cascade", "sync"])
    monkeypatch.setattr(sys, "orig_argv", ["python", "-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"], raising=False)
    def install_scrubbers() -> None:
        assert "AVIBE_MEMORY_SYNC_BOOTSTRAP" not in os.environ
        events.append("scrubbers")

    monkeypatch.setitem(
        sys.modules,
        "avibe_memory_sync_scrubbers",
        SimpleNamespace(install_error_scrubbers=install_scrubbers),
    )

    bootstrap.bootstrap()

    assert events == ["stopped", "scrubbers"]


def test_artifact_bootstrap_rejects_nonexact_argv(monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_BOOTSTRAP", "1")
    monkeypatch.setenv("AVIBE_MEMORY_CHILD_ROLE", "cascade_sync")
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_NONCE", "a" * 64)
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_PID", "321")
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME", float(8.25).hex())
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_UID", "")
    monkeypatch.setattr(bootstrap.os, "getppid", lambda: 321)
    monkeypatch.setattr(sys, "argv", ["-m", "everos.entrypoints.cli.main", "cascade", "rebuild"])

    monkeypatch.setattr(sys, "orig_argv", ["python", "-I", "-m", "everos.entrypoints.cli.main", "cascade", "rebuild"], raising=False)
    monkeypatch.setattr(bootstrap.os, "_exit", lambda code: (_ for _ in ()).throw(RuntimeError(f"exit {code}")))
    with pytest.raises(RuntimeError, match="exit 79"):
        bootstrap.bootstrap()


def test_artifact_scrubbers_redact_before_persistence(monkeypatch) -> None:
    persisted: list[str] = []

    class RunRecordStore:
        async def _update_status(self, _run_id, _status, _finished_at, error):
            persisted.append(error)

    class StateRepo:
        async def mark_failed(self, _md_path, *, retryable, error, new_retry_count):
            del retryable, new_retry_count
            persisted.append(error)

    repo = StateRepo()
    modules = {
        "everos.infra.ome._stores.run_record": SimpleNamespace(RunRecordStore=RunRecordStore),
        "everos.infra.persistence.sqlite.repos.md_change_state": SimpleNamespace(
            md_change_state_repo=repo
        ),
    }
    monkeypatch.setattr(secret_scrubber.importlib, "import_module", modules.__getitem__)
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "secret-value")

    scrubbers.install_error_scrubbers()
    asyncio.run(RunRecordStore()._update_status("run", "failed", None, "Bearer secret-value"))
    asyncio.run(
        repo.mark_failed(
            "memory.md",
            retryable=True,
            error="api_key=secret-value",
            new_retry_count=1,
        )
    )

    assert persisted == ["Bearer [REDACTED]", "api_key=[REDACTED]"]


def test_artifact_scrubber_matches_existing_persistence_redaction(monkeypatch) -> None:
    base_url = "https://Provider.Invalid/private/v1"
    api_key = "sk-super-secret-value"
    monkeypatch.setenv("EVEROS_EMBEDDING__BASE_URL", base_url)
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", api_key)
    samples = (
        f"request {base_url}/embeddings failed with {api_key}",
        "Authorization: Bearer abc.def-123",
        "api_key=api-123456789 at /Users/name/private.txt",
        r"token in C:\\Users\\name\\private.txt",
    )

    for sample in samples:
        assert scrubbers._scrub(sample) == _scrub_text(
            sample,
            base_urls=(base_url,),
            exact_values=(api_key,),
        )


def test_artifact_bootstrap_pathless_sync_acceptance_boundary(tmp_path: Path) -> None:
    """Exercise bootstrap ordering and argv admission with a behavioral fake."""

    venv_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], check=True)
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = next((venv_dir / "lib").glob("python*/site-packages"))
    (site / "avibe_memory_sync_bootstrap.py").write_bytes(Path(bootstrap.__file__).read_bytes())
    (site / "avibe_memory_sync_scrubbers.py").write_bytes(Path(secret_scrubber.__file__).read_bytes())
    (site / "avibe_memory_sync_bootstrap.pth").write_text(
        "import avibe_memory_sync_bootstrap\n", encoding="ascii"
    )
    target = tmp_path / "sync-result.json"
    scrubbed = tmp_path / "scrubbed"
    markdown_root = tmp_path / "markdown"
    (markdown_root / "nested").mkdir(parents=True)
    (markdown_root / "alpha.md").write_text("alpha", encoding="utf-8")
    (markdown_root / "nested" / "beta.md").write_text("beta", encoding="utf-8")
    (markdown_root / "ignored.txt").write_text("ignored", encoding="utf-8")
    queue = tmp_path / "cascade-queue.json"
    queue.write_text(
        json.dumps(
            [
                {"path": "alpha.md", "state": "pending"},
                {"path": "nested/beta.md", "state": "pending"},
                {"path": "settled.md", "state": "completed"},
            ]
        ),
        encoding="utf-8",
    )
    infra = site / "everos" / "infra" / "ome" / "_stores"
    infra.mkdir(parents=True)
    for package in (site / "everos", site / "everos/infra", site / "everos/infra/ome", site / "everos/infra/ome/_stores"):
        (package / "__init__.py").write_text("", encoding="ascii")
    (infra / "run_record.py").write_text(
        f"from pathlib import Path\nPath({str(scrubbed)!r}).write_text('scrubber')\n"
        "class RunRecordStore:\n    async def _update_status(self, *args, **kwargs): pass\n",
        encoding="ascii",
    )
    repo_dir = site / "everos" / "infra" / "persistence" / "sqlite" / "repos"
    repo_dir.mkdir(parents=True)
    for package in (
        site / "everos/infra/persistence",
        site / "everos/infra/persistence/sqlite",
        repo_dir,
    ):
        (package / "__init__.py").write_text("", encoding="ascii")
    (repo_dir / "md_change_state.py").write_text(
        "class Repo:\n    async def mark_failed(self, *args, **kwargs): pass\nmd_change_state_repo = Repo()\n",
        encoding="ascii",
    )
    main = site / "everos" / "entrypoints"
    main.mkdir(parents=True)
    (site / "everos/entrypoints/__init__.py").write_text("", encoding="ascii")
    (main / "cli").mkdir()
    (main / "cli/__init__.py").write_text("", encoding="ascii")
    (main / "cli/main.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:] == ['cascade', 'sync']\n"
        "source = Path(os.environ['FAKE_MEMORY_MARKDOWN_ROOT'])\n"
        "queue = Path(os.environ['FAKE_MEMORY_QUEUE'])\n"
        "rows = json.loads(queue.read_text(encoding='utf-8'))\n"
        "drained = 0\n"
        "for row in rows:\n"
        "    if row['state'] == 'pending':\n"
        "        row['state'] = 'completed'\n"
        "        drained += 1\n"
        "queue.write_text(json.dumps(rows), encoding='utf-8')\n"
        "result = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'scanned': sorted(str(path.relative_to(source)) for path in source.rglob('*.md')),\n"
        "    'drained': drained,\n"
        "}\n"
        "Path(os.environ['FAKE_MEMORY_RESULT']).write_text(json.dumps(result), encoding='utf-8')\n",
        encoding="ascii",
    )
    env = {
        **os.environ,
        "AVIBE_MEMORY_SYNC_BOOTSTRAP": "1",
        "AVIBE_MEMORY_CHILD_ROLE": "cascade_sync",
        "AVIBE_MEMORY_SYNC_NONCE": "a" * 64,
        "AVIBE_MEMORY_SYNC_PARENT_PID": str(os.getpid()),
        "AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME": float(1.0).hex(),
        "AVIBE_MEMORY_SYNC_PARENT_UID": str(os.getuid()) if hasattr(os, "getuid") else "",
        "FAKE_MEMORY_MARKDOWN_ROOT": str(markdown_root),
        "FAKE_MEMORY_QUEUE": str(queue),
        "FAKE_MEMORY_RESULT": str(target),
    }
    child = subprocess.Popen(
        [str(python), "-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"],
        cwd=tmp_path,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = __import__("psutil").Process(child.pid).status()
            if status == __import__("psutil").STATUS_STOPPED:
                break
            if child.poll() is not None:
                raise AssertionError(f"child exited before SIGSTOP: {child.returncode}")
            time.sleep(0.01)
        else:
            raise AssertionError("child did not enter SIGSTOP")
        assert not scrubbed.exists()
        assert not target.exists()
        child.send_signal(signal.SIGCONT)
        assert child.wait(timeout=10) == 0
        assert scrubbed.exists() and target.exists()
        assert scrubbed.stat().st_mtime_ns <= target.stat().st_mtime_ns
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "argv": ["cascade", "sync"],
            "scanned": ["alpha.md", "nested/beta.md"],
            "drained": 2,
        }
        assert [row["state"] for row in json.loads(queue.read_text(encoding="utf-8"))] == [
            "completed",
            "completed",
            "completed",
        ]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    # The upstream live-safety contract covers pathless sync, not fix --apply.
    # Prove the artifact bootstrap rejects both alternate commands before the
    # fake CLI can mutate its queue; this is executable scope evidence, not a
    # source-string assertion about EverOS internals.
    settled_queue = queue.read_bytes()
    for rejected_argv in (("cascade", "rebuild"), ("cascade", "fix", "--apply")):
        target.unlink(missing_ok=True)
        rejected = subprocess.run(
            [str(python), "-I", "-m", "everos.entrypoints.cli.main", *rejected_argv],
            cwd=tmp_path,
            env=env,
        )
        assert rejected.returncode != 0
        assert not target.exists()
        assert queue.read_bytes() == settled_queue

    # Import/install failure after the ownership handshake also exits before
    # the EverOS CLI target can run.
    target.unlink(missing_ok=True)
    scrubbed.unlink(missing_ok=True)
    (site / "avibe_memory_sync_scrubbers.py").write_text("raise RuntimeError('broken scrubber')\n", encoding="ascii")
    failed_install = subprocess.Popen(
        [str(python), "-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"],
        cwd=tmp_path,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and __import__("psutil").Process(failed_install.pid).status() != __import__("psutil").STATUS_STOPPED:
            if failed_install.poll() is not None:
                break
            time.sleep(0.01)
        if failed_install.poll() is None:
            failed_install.send_signal(signal.SIGCONT)
        assert failed_install.wait(timeout=10) != 0
    finally:
        if failed_install.poll() is None:
            failed_install.kill()
            failed_install.wait()
    assert not target.exists()

    # An ungated isolated invocation remains inert and normal.
    assert subprocess.run([str(python), "-I", "-c", "print('ok')"], env={k: v for k, v in env.items() if k != "AVIBE_MEMORY_SYNC_BOOTSTRAP"}, capture_output=True, text=True).stdout.strip() == "ok"


def test_pinned_everos_pathless_sync_scans_and_drains(tmp_path: Path) -> None:
    """MEMORY-REPAIR-001: run the lock-pinned CLI against isolated state."""

    required = os.environ.get("AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT") == "1"
    if importlib.util.find_spec("everos") is None:
        if required:
            pytest.fail("managed EverOS runtime is required for this contract")
        pytest.skip("managed EverOS runtime is not installed")
    assert importlib.metadata.version("everos") == EVEROS_VERSION

    home = tmp_path / "home"
    runtime_root = tmp_path / "everos-root"
    scratch = tmp_path / "tmp"
    for directory in (home, scratch):
        directory.mkdir()
    profile = (
        runtime_root
        / "default_app"
        / "default_project"
        / "users"
        / "contract-user"
        / "user.md"
    )
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "---\n"
        "user_id: contract-user\n"
        "summary: Pinned sync contract\n"
        "explicit_info: []\n"
        "implicit_traits: []\n"
        "profile_timestamp_ms: 1\n"
        "---\n"
        "Pinned sync contract.\n",
        encoding="utf-8",
    )
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "TMPDIR": str(scratch),
            "EVEROS_ROOT": str(runtime_root),
            "EVEROS_EMBEDDING__API_KEY": "",
            "EVEROS_LLM__API_KEY": "",
            "EVEROS_MULTIMODAL__API_KEY": "",
            "EVEROS_RERANK__API_KEY": "",
        }
    )
    command = [
        sys.executable,
        "-I",
        "-m",
        "everos.entrypoints.cli.main",
        "cascade",
    ]

    synced = subprocess.run(
        [*command, "sync"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert synced.returncode == 0, synced.stderr
    assert "sync complete" in synced.stdout
    assert "processed 1 row(s)" in synced.stdout

    status = subprocess.run(
        [*command, "status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert status.returncode == 0, status.stderr
    assert "pending:                  0" in status.stdout
    assert "done:                     1" in status.stdout

    drained = subprocess.run(
        [*command, "sync"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert drained.returncode == 0, drained.stderr
    assert "processed 0 row(s)" in drained.stdout

    # ``fix --apply`` is intentionally absent: #1318 has no upstream
    # online-safety contract for that operation.
