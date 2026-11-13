"""Generation storage contracts not covered by ordinary create/replace tests.

Synthetic envelopes exercise orchestration only, not end-to-end custody secrecy.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from storage import vault_generation as generation, vault_service as vs
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, metadata, vault_audit, vault_secrets
from storage.vault_crypto import Sealed

PASSWORD = {"type": "password", "length": 24, "digits": True, "symbols": True, "no_lookalikes": False}


@pytest.fixture
def engine(tmp_path):
    value = create_sqlite_engine(tmp_path / "generation.sqlite")
    metadata.create_all(value)
    yield value
    value.dispose()


def sealed(label="one"):
    return Sealed(ciphertext=f"opaque-{label}", nonce="nonce", wrap_meta="wrap")


def generate(engine, name="LOGIN", field=None, *, session_id="ses_origin", recipe=None, metadata=None):
    with engine.connect() as conn:
        snapshot = generation.capture_target(conn, name, field)
    with engine.begin() as conn:
        return generation.commit_generated(conn, snapshot, sealed(field or name), recipe=recipe or PASSWORD,
                                           metadata=metadata or {}, session_id=session_id, release_scopes=[])


def events(engine):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(vault_audit)).mappings()]


def test_generated_provenance_and_operation_summary_are_distinct_and_value_free(engine):
    with engine.begin() as conn:
        now = vs._now()
        conn.execute(agent_sessions.insert().values(id="ses_origin", session_anchor="ses_origin", title="Login setup",
                     agent_name="vibey", agent_backend="vibey", agent_variant="vibey",
                     workdir="/test", native_session_id="", status="active", visibility="background", pinned=0,
                     metadata_json="{}", created_at=now, updated_at=now, last_active_at=now))
    result = generate(engine, field="password")
    assert result["generated"] == {"by": "agent", "session_id": "ses_origin", "type": "password", "length": 24}
    first = result["secret"]["generated"][0]
    assert first == {"field": "password", **result["generated"], "session_title": "Login setup", "at": first["at"]}
    assert "ciphertext" not in json.dumps(result)
    second = generate(engine, field="token", recipe={"type": "hex", "bytes": 32})
    assert second["secret"]["generated"][0] == first
    assert [item["field"] for item in second["secret"]["generated"]] == ["password", "token"]
    audits = [json.loads(row["delivery"]) for row in events(engine) if row["event"] == "generated"]
    assert [{key: value for key, value in row.items() if key != "_resource_access_snapshot"} for row in audits] == [
        result["generated"], second["generated"],
    ]


@pytest.mark.parametrize("session_id", [None, "ses_nonexistent"])
def test_owner_terminal_has_cli_provenance_without_fabricated_session(engine, session_id):
    result = generate(engine, session_id=session_id)
    assert result["generated"]["by"] == "cli"
    assert result["secret"]["generated"][0]["session_id"] is None
    assert result["secret"]["generated"][0]["session_title"] is None


def test_existing_session_with_nullable_title_remains_agent_attribution(engine):
    with engine.begin() as conn:
        now = vs._now()
        conn.execute(agent_sessions.insert().values(id="ses_origin", session_anchor="ses_origin", title=None,
                     agent_name="vibey", agent_backend="vibey", agent_variant="vibey", workdir="/test",
                     native_session_id="", status="active", visibility="background", pinned=0,
                     metadata_json="{}", created_at=now, updated_at=now, last_active_at=now))
    provenance = generate(engine)["secret"]["generated"][0]
    assert (provenance["by"], provenance["session_id"], provenance["session_title"]) == ("agent", "ses_origin", None)


@pytest.mark.parametrize("field,kind,protection", [
    (None, "static", "standard"), ("password", "static", "standard"),
    ("username", "static", "standard"), ("new", "keypair", "standard"),
    ("new", "static", "protected"),
])
def test_target_rejections_never_store_or_audit(engine, field, kind, protection):
    with engine.begin() as conn:
        vs.create_secret(conn, name="LOGIN", sealed=sealed(), kind=kind, protection=protection,
                         default_field="password" if kind == "static" else None,
                         fields=[{"name": "username", "sealed": sealed("extra")}] if kind == "static" else None)
    before = events(engine)
    with engine.begin() as conn:
        original = dict(conn.execute(select(vault_secrets)).mappings().one())
        with pytest.raises(generation.GenerateTargetError):
            generation.capture_target(conn, "LOGIN", field)
        assert dict(conn.execute(select(vault_secrets)).mappings().one()) == original
    assert events(engine) == before


@pytest.mark.parametrize("existing", [False, True])
def test_competing_generations_commit_only_one_record_and_one_generated_event(engine, existing):
    if existing:
        with engine.begin() as conn:
            vs.create_secret(conn, name="LOGIN", sealed=sealed())
    with engine.connect() as conn:
        snapshots = [generation.capture_target(conn, "LOGIN", "password") for _ in range(2)]

    def commit(snapshot):
        try:
            with engine.begin() as conn:
                return generation.commit_generated(conn, snapshot, sealed(), recipe=PASSWORD, metadata={},
                                                   session_id=None, release_scopes=[])["ok"]
        except (generation.GenerateTargetError, vs.SecretConflictError):
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(commit, snapshots)) == [False, True]
    assert len([row for row in events(engine) if row["event"] == "generated"]) == 1
    with engine.connect() as conn:
        assert vs.get_secret_meta(conn, "LOGIN")["fields"] == (["value", "password"] if existing else ["password"])


def test_failed_generated_audit_rolls_back_entire_commit_even_if_caller_catches(engine, monkeypatch):
    with engine.connect() as conn:
        snapshot = generation.capture_target(conn, "LOGIN", None)
    real_audit = vs.audit

    def audit(conn, event, **kwargs):
        if event == "generated":
            raise RuntimeError("audit unavailable")
        return real_audit(conn, event, **kwargs)

    monkeypatch.setattr(vs, "audit", audit)
    with engine.begin() as conn:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            generation.commit_generated(conn, snapshot, sealed(), recipe=PASSWORD, metadata={}, session_id=None, release_scopes=[])
        assert vs.list_secrets(conn) == []
    assert events(engine) == []


@pytest.mark.parametrize("operation", ["replace_default", "replace_extra", "rotate", "remove", "rename"])
def test_provenance_tracks_value_replacement_removal_and_default_rename(engine, operation):
    generate(engine, field="password")
    generate(engine, field="token")
    with engine.begin() as conn:
        if operation == "replace_default":
            meta = vs.update_secret_field(conn, "LOGIN", "password", sealed("new"), record="LOGIN")
        elif operation == "replace_extra":
            meta = vs.update_secret_field(conn, "LOGIN", "token", sealed("new"), record="LOGIN.token")
        elif operation == "rotate":
            meta = vs.rotate_secret(conn, "LOGIN", sealed("new"))
        elif operation == "remove":
            meta = vs.remove_secret_field(conn, "LOGIN", "token")
        else:
            before = vs.get_secret_meta(conn, "LOGIN")
            meta = vs.update_secret_metadata(conn, "LOGIN", default_field="passphrase")
            assert meta["updated_at"] == before["updated_at"]
    expected = ["passphrase", "token"] if operation == "rename" else ["token"] if operation in {"replace_default", "rotate"} else ["password"]
    assert [item["field"] for item in meta["generated"]] == expected


def test_metadata_changes_during_custody_fail_without_losing_newer_metadata(engine):
    generate(engine)
    with engine.connect() as conn:
        snapshot = generation.capture_target(conn, "LOGIN", "token")
    with engine.begin() as conn:
        vs.update_secret_metadata(conn, "LOGIN", tags=["newer"])
    before = events(engine)
    with engine.begin() as conn:
        with pytest.raises(vs.SecretConflictError):
            generation.commit_generated(conn, snapshot, sealed(), recipe=PASSWORD, metadata={"tags": ["older"]}, session_id=None, release_scopes=[])
        meta = vs.get_secret_meta(conn, "LOGIN")
        assert meta["tags"] == ["newer"]
        assert meta["fields"] == ["value"]
    assert events(engine) == before


def test_list_find_and_rest_project_the_same_generated_provenance(engine, monkeypatch, capfd):
    from vibe import api, cli
    from vibe.ui_server import app

    provenance = generate(engine)["secret"]["generated"]
    monkeypatch.setattr(cli, "_open_vault_engine", lambda **kwargs: engine)
    monkeypatch.setattr(api, "_vault_engine", lambda: engine)
    for command, function in (("list", cli.cmd_vault_list), ("find", cli.cmd_vault_find)):
        args = cli.build_parser().parse_args(["vault", command])
        assert function(args) == 0
        assert json.loads(capfd.readouterr().out)["secrets"][0]["generated"] == provenance
    response = app.test_client().get("/api/vault/secrets")
    assert response.status_code == 200
    assert response.get_json()["secrets"][0]["generated"] == provenance


def test_strict_provenance_projection_never_reflects_arbitrary_nested_metadata(engine):
    generated = generate(engine)["secret"]["generated"][0]
    known_plaintext = "never-reflect-this-stored-test-value"
    variants = [
        {**generated, "session_id": {"value": known_plaintext}},
        {**generated, "session_title": {"value": known_plaintext}},
        {**generated, "by": {"value": known_plaintext}},
        {**generated, "length": True}, {**generated, "at": "not-a-date"},
        {**generated, "value": known_plaintext}, {**generated, "field": "removed"},
    ]
    with engine.begin() as conn:
        conn.execute(vault_secrets.update().values(public_meta=json.dumps({"generated": variants})))
        meta = vs.get_secret_meta(conn, "LOGIN")
        assert "generated" not in meta
        assert known_plaintext not in json.dumps(vs.list_secrets(conn))
    with engine.begin() as conn:
        with pytest.raises(vs.InvalidRequestError, match="storage-owned"):
            vs.create_secret(conn, name="FORGED", sealed=sealed(), public_meta={"generated": [generated]})
