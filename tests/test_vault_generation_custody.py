"""Opt-in real custody smoke; requires an explicit test binary, never PATH/keychain.

Run with AVIBE_TEST_AVAULT_GENERATE_BINARY=/absolute/test/avault. The fixture
forces file storage in its private AVAULT_HOME. Generated values travel only
from avault to an intended child, which checks the format and prints nothing.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.control_requests import new_request
from core.handlers.cli_requests import handle_control_request
from storage import db, vault_service as vs
from storage.models import metadata, vault_audit
from storage.vault_crypto import Sealed
from vibe import api, cli


@pytest.mark.parametrize("kind", ["password", "hex", "base64"])
def test_real_file_store_generation_never_returns_values_to_service(monkeypatch, tmp_path, capfd, caplog, kind):
    explicit = os.environ.get("AVIBE_TEST_AVAULT_GENERATE_BINARY")
    if not explicit:
        pytest.skip("requires explicit merged-contract avault test binary; no installed-runtime fallback")
    binary = Path(explicit)
    assert binary.is_absolute() and binary.is_file()
    node = shutil.which("node")
    assert node, "node is needed for a silent intended delivery child"
    home = tmp_path / "custody"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("AVAULT_HOME", str(home))
    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: str(binary))
    monkeypatch.setattr(api, "_avault_store_args", lambda: ["--store", "file"])
    monkeypatch.setattr(api, "release_vault_agent_scopes", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_publish_cli_vaults_updated", lambda **kwargs: None)
    engine = db.create_sqlite_engine(tmp_path / "service.sqlite")
    metadata.create_all(engine)
    monkeypatch.setattr(db, "get_cached_sqlite_engine", lambda: engine)
    sentinel = "known-nonsecret-fixture-never-in-service-output"
    try:
        # Existing plaintext-input seal is used solely to establish a known,
        # nonsecret test fixture. Product generation sends no plaintext stdin.
        fixture = subprocess.run([str(binary), "--store", "file", "seal", "--name", "LOGIN"],
                                 input=sentinel.encode(), capture_output=True, check=False, timeout=20)
        assert fixture.returncode == 0
        assert sentinel not in fixture.stdout.decode() + fixture.stderr.decode()
        with engine.begin() as conn:
            vs.create_secret(conn, name="LOGIN", sealed=Sealed(**json.loads(fixture.stdout)))
        request = new_request("vault.generate", {"name": "LOGIN", "field": "token", "recipe": {"type": kind}})
        response = asyncio.run(handle_control_request(SimpleNamespace(), request.to_payload()))
        assert response["exit_code"] == 0, response
        with engine.connect() as conn:
            envelope = vs.get_envelope(conn, "LOGIN.token")
            original = vs.get_envelope(conn, "LOGIN")
            audits = [dict(row) for row in conn.execute(select(vault_audit)).mappings()]
        entries = [
            {"name": "LOGIN.token", "env": "GENERATED", "envelope": api._envelope_payload(envelope)},
            {"name": "LOGIN", "env": "KNOWN", "envelope": api._envelope_payload(original)},
        ]
        child = (
            "const v=process.env.GENERATED;const k=" + json.dumps(kind) + ";"
            "let ok=process.env.KNOWN===" + json.dumps(sentinel) + ";"
            "if(k==='password')ok=ok&&v.length===24;"
            "else if(k==='hex')ok=ok&&/^[0-9a-f]{64}$/.test(v);"
            "else ok=ok&&Buffer.from(v,'base64').length===32&&Buffer.from(v,'base64').toString('base64')===v;"
            "process.exit(ok?0:1);"
        )
        delivered = subprocess.run([str(binary), "--store", "file", "deliver", "run", "--", node, "-e", child],
                                   input=json.dumps(entries).encode(), capture_output=True, check=False, timeout=20)
        assert delivered.returncode == 0
        assert delivered.stdout == b"" and delivered.stderr == b""
        captured = capfd.readouterr()
        assert sentinel not in json.dumps(response) + json.dumps(audits) + captured.out + captured.err + caplog.text
    finally:
        engine.dispose()
