"""Direct inventory and curation: independent paths through the real persistent store."""

from concurrent.futures import ThreadPoolExecutor
import subprocess

from core.handlers.direct_model_menu import PickerModel, publish_inventory, read_inventory, read_menu, set_removed


def test_read_before_and_after_failed_refresh_and_account_switch(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    public = (PickerModel("public"),)
    private = (PickerModel("account-a-only"),)

    def menu(identity):
        return read_menu("codex", identity, db_path=db, fallback=public)

    publish_inventory("codex", "a", private, db_path=db)

    def forbidden(*args, **kwargs):
        raise AssertionError("a picker read started a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = menu("a")
    assert before.observed_at and before.models == private
    for identity in ("b", None):
        snapshot = menu(identity)
        assert snapshot.models == public and snapshot.observed_at is None
        assert read_inventory("codex", identity, db_path=db).models == ()
    publish_inventory("codex", "a", None, db_path=db)
    assert menu("a") == before  # failed read did not advance the evidence or drop a row
    publish_inventory("codex", "b", [], db_path=db)
    empty = menu("b")
    assert empty.models == () and empty.observed_at is not None
    assert menu("a") == before


def test_owner_mutation_survives_discovery_without_changing_supply(tmp_path, monkeypatch):
    from config import paths
    from config.v2_config import V2Config
    from tests.ui_server_test_helpers import csrf_headers
    from vibe.ui_server import app

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    V2Config.default().save()
    db = paths.get_sqlite_state_path()
    client = app.test_client()
    url = "/api/backend/claude/models/visibility"
    payload = {"model_id": "opus", "removed": True}
    assert client.put(url, json=payload).status_code == 403
    headers = csrf_headers(client)
    assert client.put(url, json={**payload, "removed": "false"}, headers=headers).status_code == 400
    old = PickerModel("opus", resolved_model="opus-1")
    new = PickerModel("opus", resolved_model="opus-2", reasoning_efforts=("high",))
    publish_inventory("claude", "a", [old], db_path=db)
    assert client.put(url, json=payload, headers=headers).get_json() == {"ok": True}
    publish_inventory("claude", "a", [new], db_path=db)
    assert read_inventory("claude", "a", db_path=db).models == (new,)
    assert read_menu("claude", "a", db_path=db).models == ()
    assert read_menu("claude", "a", removed=True, db_path=db).models == (new,)
    assert read_menu("claude", None, fallback=(old,), db_path=db).models == ()
    assert read_menu("claude", "b", removed=True, db_path=db).models == ()
    assert client.put(url, json={**payload, "removed": False}, headers=headers).status_code == 200
    assert read_menu("claude", "a", db_path=db).models == (new,)


def test_independent_deletions_survive_competing_writers(tmp_path):
    db = tmp_path / "state.sqlite3"
    models = [PickerModel("p/one"), PickerModel("p/two"), PickerModel("q/one")]
    publish_inventory("opencode", "account/cwd", models, db_path=db)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda row: set_removed("opencode", row.id, True, db_path=db), models[:2]))
    assert read_menu("opencode", "account/cwd", db_path=db).models == (models[2],)
