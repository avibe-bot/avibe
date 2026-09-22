"""Real supervised children must not borrow the service's caller identity.

All child output is synthetic, allowlisted context. Vault writes below use an
isolated database with dummy sealed envelopes, never a real credential or grant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import sys
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.caller_context import AVIBE_CALLER_SESSION_PROOF_ENV, CALLER_CONTEXT_ENV_NAMES
from core.scheduled_tasks import TaskExecutionStore
from core.watches import ManagedWatch, ManagedWatchService, ManagedWatchStore, WatchRuntimeStateStore
from storage import vault_service
from storage.db import create_sqlite_engine
from storage.models import metadata as schema, vault_requests
from storage.vault_crypto import Sealed


def _probe_command() -> list[str]:
    # Import the task source explicitly: neither the installed CLI nor a parent
    # PYTHONPATH is evidence about the code being tested.
    root = str(Path(__file__).resolve().parents[1])
    script = f"""
import json, os, sys
from types import SimpleNamespace
sys.path.insert(0, {root!r})
from core.caller_context import CALLER_CONTEXT_ENV_NAMES, AVIBE_CALLER_SESSION_PROOF_ENV
from vibe.cli import _vault_cli_delivery_context
requester, delivery, session = _vault_cli_delivery_context(SimpleNamespace(), mode="run")
keys = CALLER_CONTEXT_ENV_NAMES | {{AVIBE_CALLER_SESSION_PROOF_ENV,
    "AVIBE_WATCH_ID", "AVIBE_WATCH_LAST_DELIVERY", "COMMAND_CONFIG_PROBE"}}
print(json.dumps({{"requester": requester, "delivery": delivery, "session": session,
    "context": {{key: os.environ[key] for key in keys if key in os.environ}},
    "has_path": bool(os.environ.get("PATH"))}}, ensure_ascii=False))
"""
    return [sys.executable, "-c", script]


def _contaminate_parent(monkeypatch) -> None:
    for key in CALLER_CONTEXT_ENV_NAMES:
        monkeypatch.setenv(key, "stale-service-caller")
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_unrelated")
    monkeypatch.setenv("AVIBE_CALLER_REMOTE", "1")
    monkeypatch.setenv("AVIBE_CALLER_RESOURCE_CONTEXT", '{"sub":"wrong-owner"}')
    monkeypatch.setenv(AVIBE_CALLER_SESSION_PROOF_ENV, "stale-proof")


def _watch(tmp_path, *, session_id, shell=False, metadata=None):
    command = _probe_command()
    watch = ManagedWatch(
        id="wat_context",
        name="会话归属回归",
        session_key="slack::channel::legacy",
        session_id=session_id,
        command=[] if shell else command,
        shell_command=(subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)) if shell else None,
        cwd=str(tmp_path),
        metadata=metadata or {},
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=ManagedWatchStore(tmp_path / "watches.json"),
        request_store=TaskExecutionStore(tmp_path / "requests"),
        runtime_store=WatchRuntimeStateStore(tmp_path / "runtime.json"),
    )
    return service, watch


@pytest.mark.parametrize("contaminated", [False, True])
@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("session_id", ["ses_watch_owner", None])
def test_hfr_486_watch_child_vault_callback_uses_only_current_binding(
    tmp_path, monkeypatch, contaminated, shell, session_id
):
    """HFR-486: real Watch -> supervisor -> CLI -> request expiry -> callback."""
    if contaminated:
        _contaminate_parent(monkeypatch)
    monkeypatch.setenv("COMMAND_CONFIG_PROBE", "preserve-普通配置")
    service, watch = _watch(
        tmp_path, session_id=session_id, shell=shell,
        metadata={
            "created_by": {"caller": {"session_id": "ses_old_creator"}},
            "delivered_reports": 3,
        },
    )
    result = asyncio.run(service._run_cycle(watch, timeout_seconds=30))
    assert result.exit_code == 0, result.stderr
    child = json.loads(result.stdout)
    assert child["session"] == session_id
    assert child["has_path"]
    assert child["context"] == {
        **({"AVIBE_SESSION_ID": session_id} if session_id else {}),
        "AVIBE_CALLER_SOURCE": "watch",
        "AVIBE_WATCH_ID": watch.id,
        "AVIBE_WATCH_LAST_DELIVERY": "3",
        "COMMAND_CONFIG_PROBE": "preserve-普通配置",
    }
    assert child["requester"].get("session_id") == session_id
    assert child["delivery"].get("session_id") == session_id

    engine = create_sqlite_engine(tmp_path / "vault.sqlite")
    schema.create_all(engine)
    try:
        with engine.begin() as conn:
            vault_service.create_secret(
                conn, name="TEST_APPROVAL",
                sealed=Sealed(ciphertext="dummy", nonce="dummy", wrap_meta="dummy"),
                protection="protected",
            )
            request = vault_service.create_access_request(
                conn, "TEST_APPROVAL", requester=child["requester"], delivery=child["delivery"],
            )
            conn.execute(
                vault_requests.update().where(vault_requests.c.id == request["id"]).values(
                    expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                )
            )
            expired = vault_service._load_request_row(conn, request["id"])
            assert expired["status"] == "expired"
            row = dict(conn.execute(
                select(vault_requests).where(vault_requests.c.id == request["id"])
            ).mappings().one())
            callback = vault_service.resolve_request_callback(row)
            if session_id:
                assert callback is not None
                assert callback.session_id == session_id
                assert "expired" in callback.message
            else:
                assert callback is None
    finally:
        engine.dispose()


def test_hfr_486_retargeted_watch_uses_the_new_binding_next_cycle(tmp_path, monkeypatch):
    _contaminate_parent(monkeypatch)
    service, watch = _watch(tmp_path, session_id="ses_first")
    for target in ("ses_first", "ses_second", None):
        watch.session_id = target
        result = asyncio.run(service._run_cycle(watch, timeout_seconds=30))
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["session"] == target


@pytest.mark.parametrize("session_id", ["ses_remote", None])
def test_hfr_486_watch_carries_definition_authority_not_service_authority(tmp_path, monkeypatch, session_id):
    _contaminate_parent(monkeypatch)
    # The runner carries stored provenance; ordinary admission, not this helper,
    # owns whether that snapshot is valid. Exercise those consumers separately.
    snapshot = {"sub": "definition-editor", "vibe_instance_role": "editor"}
    service, watch = _watch(
        tmp_path, session_id=session_id, metadata={"resource_user_context": snapshot},
    )
    result = asyncio.run(service._run_cycle(watch, timeout_seconds=30))
    assert result.exit_code == 0, result.stderr
    child = json.loads(result.stdout)
    assert child["session"] == session_id
    assert child["context"]["AVIBE_CALLER_REMOTE"] == "1"
    assert json.loads(child["context"]["AVIBE_CALLER_RESOURCE_CONTEXT"]) == snapshot
    assert AVIBE_CALLER_SESSION_PROOF_ENV not in child["context"]
