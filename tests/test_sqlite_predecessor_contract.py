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
    ]
    monkeypatch.setattr(contract, "_run", lambda *args: json.dumps([releases[:2], releases[2:]]))
    assert contract.previous_release("test/repo")["tag_name"] == "gh-v3.2.5rc20"
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
    for epoch in (1, None):
        contract.activate_build(epoch)
        loaded = runpy.run_path(str(path))
        assert loaded["VERIFIED_PREDECESSOR_EPOCH"] == epoch
        assert loaded["other"] == 7
    path.write_text("other = 7\n")
    with pytest.raises(RuntimeError, match="Missing/ambiguous"):
        contract.activate_build(1)


def test_wrong_previous_binary_is_rejected_before_any_store_write(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "run_probe", lambda *args: {"version": "wrong", "storage": str(tmp_path / "storage.py")})
    with pytest.raises(RuntimeError, match="wrong predecessor"):
        contract.verify_predecessor(tmp_path / "venv/bin/python", tmp_path, expected_version="3.2.5rc18")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("loss", [None, "replacement", "json"])
def test_source_fixture_exercises_probe_and_detects_reader_write_loss(tmp_path, monkeypatch, loss):
    real_probe = contract.run_probe

    def source_fixture(python, action, home, *, current=False):
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
        assert contract.verify_predecessor(fixture_python, tmp_path, expected_version="source-fixture") == 1
    else:
        with pytest.raises((AssertionError, RuntimeError), match="lost new values|verify probe failed"):
            contract.verify_predecessor(fixture_python, tmp_path, expected_version="source-fixture")
