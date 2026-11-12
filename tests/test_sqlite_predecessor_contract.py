"""The one-predecessor harness must not select a convenient tag or hide data loss.

Source-fixture smoke below validates the probe itself, NOT a released binary and
NOT no-copy activation. The real wheel check runs separately in CI/publishing.
"""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import sqlite3
import sys

import pytest

from scripts import sqlite_predecessor_contract as contract

pytestmark = pytest.mark.no_sqlite_template


def _release(tag, at, **kwargs):
    return {"tag_name": tag, "published_at": at, "draft": False, **kwargs}


def test_selects_immediately_previous_publication_including_same_graph_rcs(monkeypatch):
    releases = [
        _release("v9.0.0", "2026-10-01T00:00:00Z"),
        _release("gh-v3.2.5rc19", "2026-11-08T01:00:00Z"),
        _release("gh-v3.2.5rc20", "2026-11-08T02:00:00Z"),
        _release("gh-v3.2.5rc21", "2026-11-08T03:00:00Z", draft=True),
        # Managed-runtime releases share the repository but carry no Avibe wheel.
        _release("model-hub-engine-v8.0.63-1", "2026-11-10T04:24:07Z"),
        _release("git-runtime-v2.55.0-1", "2026-11-10T05:00:00Z"),
    ]
    monkeypatch.setattr(contract, "_run", lambda *args: json.dumps([releases[:2], releases[2:]]))
    assert contract.previous_release("test/repo")["tag_name"] == "gh-v3.2.5rc20"
    assert contract.previous_release("test/repo", release_tag="gh-v3.2.5rc25")["tag_name"] == "gh-v3.2.5rc20"
    assert contract.previous_release("test/repo", release_tag="gh-v3.2.5rc20")["tag_name"] == "gh-v3.2.5rc19"
    with pytest.raises(RuntimeError, match="No published predecessor"):
        contract.previous_release("test/repo", release_tag="v9.0.0")


def test_probe_environment_has_no_caller_credentials_routing_or_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fixture-token-not-a-credential")
    monkeypatch.setenv("AVIBE_SESSION_ID", "do-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "/do-not-import")
    env = contract._probe_env(tmp_path)
    assert not {"GH_TOKEN", "AVIBE_SESSION_ID", "PYTHONPATH"}.intersection(env)
    for key in ("HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
        assert Path(env[key]).is_dir()


def test_activation_only_fills_the_build_literal_and_can_disable_it(tmp_path, monkeypatch):
    path = tmp_path / "storage" / "compatibility.py"
    path.parent.mkdir()
    path.write_text("VERIFIED_PREDECESSOR_EPOCH: int | None = None\nother = 7\n")
    monkeypatch.setattr(contract, "ROOT", tmp_path)
    for epoch in (1, 2, None):
        contract.activate_build(epoch)
        loaded = runpy.run_path(str(path))
        assert loaded["VERIFIED_PREDECESSOR_EPOCH"] == epoch
        assert loaded["other"] == 7
    path.write_text("other = 7\n")
    with pytest.raises(RuntimeError, match="Missing/ambiguous"):
        contract.activate_build(1)


def test_projection_contract_ignores_presentation_copy_but_not_identity_or_outcome():
    step = {"id": "tool:response:call", "name": "read", "arguments": {"path": "/work/a"}, "status": "completed", "text": "read"}
    group = {"id": step["id"], "steps": 1, "status": "done", "rows": [{**step, "kind": "tool_call"}]}
    before = contract._projection_facts([step], {"groups": [group]}, [group], [], [])
    restyled = {**step, "text": "Read", "new_display_field": "compatible"}
    restyled_group = {**group, "new_group_field": "compatible", "rows": [{**restyled, "kind": "tool_call"}]}
    assert contract._projection_facts([restyled], {"groups": [restyled_group]}, [restyled_group], [], []) == before
    assert contract._projection_facts([{**restyled, "status": "unknown"}], {"groups": [group]}, [group], [], []) != before
    assert contract._projection_facts([], {"groups": [group]}, [group], [], []) != before


def test_refusal_control_cannot_activate_a_build():
    with pytest.raises(SystemExit, match="2"):
        contract.main(["--require-refusal", "--activate"])


def test_wrong_previous_binary_is_rejected_before_any_store_write(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "run_probe", lambda *args: {"version": "wrong", "storage": str(tmp_path / "storage.py")})
    with pytest.raises(RuntimeError, match="wrong predecessor"):
        contract.verify_predecessor(tmp_path / "venv/bin/python", tmp_path, expected_version="3.2.5rc18")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("loss", [None, "replacement", "json", "projection"])
def test_source_fixture_exercises_probe_and_detects_reader_write_loss(tmp_path, monkeypatch, loss):
    real_probe = contract.run_probe

    def source_fixture(python, action, home, *, current=False):
        if not current and action == "projection-read" and loss == "projection":
            with sqlite3.connect(home / "state/vibe.sqlite") as conn:
                conn.execute("UPDATE messages SET metadata_json=json_set(metadata_json, '$.tool_projection.version', 99) "
                             "WHERE json_extract(metadata_json, '$.tool_projection.version')=1")
        result = real_probe(Path(sys.executable), action, home, current=True)
        if action == "identity":
            # Name this explicitly as a source fixture, never a published result.
            result["version"] = "source-fixture"
        if not current and action == "exercise" and loss:
            with sqlite3.connect(home / "state/vibe.sqlite") as conn:
                if conn.execute("SELECT version_num FROM alembic_version").fetchone() == (contract.FUTURE_REVISION,):
                    if loss == "replacement":
                        columns = [row[1] for row in conn.execute("PRAGMA table_info(scope_settings)")
                                   if not row[1].startswith("reader_contract_")]
                        columns_sql = ", ".join(f'"{name}"' for name in columns)
                        conn.execute(f"INSERT OR REPLACE INTO scope_settings ({columns_sql}) SELECT {columns_sql} FROM scope_settings")
                    else:
                        key, payload = conn.execute("SELECT record_key, payload_json FROM runtime_records WHERE record_type='active_poll'").fetchone()
                        value = json.loads(payload)
                        value.pop("reader_contract_extra")
                        conn.execute("UPDATE runtime_records SET payload_json=? WHERE record_key=?", (json.dumps(value), key))
        return result

    monkeypatch.setattr(contract, "run_probe", source_fixture)
    # This fixture's identity check names its actual source root; no published
    # wheel/version is impersonated, and activate_build is never invoked.
    fixture_python = contract.ROOT / "bin/python"
    if loss is None:
        assert contract.verify_predecessor(fixture_python, tmp_path, expected_version="source-fixture") == 2
    else:
        with pytest.raises((AssertionError, RuntimeError), match="lost new values|verify probe failed|projection-read probe failed"):
            contract.verify_predecessor(fixture_python, tmp_path, expected_version="source-fixture")


def test_source_fixture_proves_trace_only_reader_refusal_is_not_a_capability_pass(tmp_path, monkeypatch):
    real_probe = contract.run_probe
    from alembic.script import ScriptDirectory
    from storage import migrations

    candidate_head = ScriptDirectory.from_config(migrations.alembic_config()).get_current_head()

    def unsupported_reader(python, action, home, *, current=False):
        if not current and action == "open":
            with sqlite3.connect(home / "state/vibe.sqlite") as conn:
                assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (candidate_head,)
            return {"refused": True}
        result = real_probe(Path(sys.executable), action, home, current=True)
        if action == "identity":
            result.update(version="source-fixture", supported_epochs=[])
        return result

    monkeypatch.setattr(contract, "run_probe", unsupported_reader)
    assert contract.verify_predecessor(contract.ROOT / "bin/python", tmp_path, expected_version="source-fixture") is None
