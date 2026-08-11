from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import memory_runtime_sitecustomize as bootstrap
from scripts import memory_runtime_sync_scrubbers as scrubbers
from core.memory.everos_insight.recorder import _scrub_text


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
    monkeypatch.setattr(bootstrap.os, "getppid", lambda: parent_pid)
    monkeypatch.setattr(bootstrap.os, "getpid", lambda: 654)
    monkeypatch.setattr(
        bootstrap.os,
        "kill",
        lambda _pid, _signal: events.append("stopped"),
    )
    monkeypatch.setattr(sys, "argv", ["-m", "everos.entrypoints.cli.main", "cascade", "sync"])
    monkeypatch.setitem(
        sys.modules,
        "avibe_memory_sync_scrubbers",
        SimpleNamespace(install_error_scrubbers=lambda: events.append("scrubbers")),
    )

    bootstrap.bootstrap()

    assert events == ["stopped", "scrubbers"]


def test_artifact_bootstrap_rejects_nonexact_argv(monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_BOOTSTRAP", "1")
    monkeypatch.setenv("AVIBE_MEMORY_CHILD_ROLE", "cascade_sync")
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_NONCE", "a" * 64)
    monkeypatch.setenv("AVIBE_MEMORY_SYNC_PARENT_PID", "321")
    monkeypatch.setattr(bootstrap.os, "getppid", lambda: 321)
    monkeypatch.setattr(sys, "argv", ["-m", "everos.entrypoints.cli.main", "cascade", "rebuild"])

    with pytest.raises(RuntimeError, match="unexpected argv"):
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
    monkeypatch.setattr(scrubbers.importlib, "import_module", modules.__getitem__)
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
