"""The first admitted inspector reads a released journal, but cannot act on any class of it."""
import json
import os
import time

import pytest

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config
from tests.test_desktop_bound_observation import _inspect
from tests.test_desktop_claim_pipe import _parent
from tests.test_pairing_recovery import _fail_config_publication_once, pairing_host as pairing_host
from vibe import desktop_claim_pipe as pipe, desktop_legacy_observation as legacy, desktop_local_instance as local
from vibe import remote_access as pairing


def _prepare():
    cfg = V2Config.load()
    # The released prepare's account label need not be the signed-in subject.
    value = local.prepare("not-the-observer", local._revision(cfg, None, None))
    return {"operation_id": value["operation_id"], "account_id": "not-the-observer",
            "installation_id": value["local"]["installation_id"],
            "expected_binding_revision": value["local"]["binding_revision"]}


def _read_only(monkeypatch):
    calls = []

    def forbidden(*_a, **_kw):
        calls.append(True)
        raise AssertionError("inspect reached a writer or a transport")

    for obj, names in [(pipe, ["DesktopClaim"]), (local, ["_config", "ensure_installation_id"]),
                       (V2Config, ["load", "save"]), (pairing, ["_json_request", "_persist_pairing", "start",
                        "_retry_installation_pairing", "_write_pending_pairing_record", "_retire_pending_pairing"])]:
        for name in names:
            monkeypatch.setattr(obj, name, forbidden)
    return calls


@pytest.mark.parametrize("case,expected", [
    ("no_request", "legacy_pairing_no_request"), ("recent", "legacy_pairing_wait"),
    ("uncertain", "legacy_pairing_wait"), ("old_display", "legacy_pairing_wait"),
    ("terminal", "legacy_pairing_ended"), ("expired", "legacy_pairing_expired"),
    ("stale_dispatch", "legacy_pairing_wait"), ("recent_terminal", "legacy_pairing_wait"),
    ("source_changed", "legacy_pairing_ended"), ("missing_id", "legacy_pairing_ended"),
    ("damaged_id", "legacy_pairing_ended"), ("sealed", "legacy_pairing_publication"),
    ("sealed_missing_id", "legacy_pairing_incompatible"), ("applied", "legacy_pairing_publication"),
    ("applied_off", "legacy_pairing_incompatible"), ("partial", "pairing_recovery_required"),
    ("off", "pairing_recovery_required"), ("unknown_cfg", "pairing_recovery_required"),
    ("mixed", "pairing_recovery_required"), ("nested", "pairing_recovery_required"),
    ("null_request", "pairing_recovery_required"), ("unknown", "pairing_recovery_required"),
    ("unprotected", "pairing_recovery_required"), ("missing_lock", "pairing_recovery_required"),
    ("future_identity", "pairing_recovery_required"), ("pending_reply", "legacy_pairing_wait"),
    ("duplicate", "pairing_recovery_required"), ("unreadable", "pairing_recovery_required"),
])
def test_private_inspector_stops_at_the_actual_journal(pairing_host, monkeypatch, case, expected):
    guard = _prepare()
    journal, identity, cfg = pairing._pending_pairing_path(), local._identity_path(), paths.get_config_path()
    sealed = case.startswith(("sealed", "applied"))
    req = sealed or case in {"uncertain", "pending_reply", "old_display", "terminal", "expired", "stale_dispatch", "recent_terminal",
                             "missing_id", "damaged_id", "source_changed"}
    if req:
        if case.startswith("sealed"):
            _fail_config_publication_once(monkeypatch)
        elif case.startswith("applied"):
            monkeypatch.setattr(pairing, "_retire_pending_pairing", lambda **_: False)
        else:
            def reply(*_a, **_kw):
                if case in {"terminal", "stale_dispatch", "recent_terminal"}:
                    raise pairing.BackendRequestError(422, {"error": "fixture_rejected"})
                if case == "expired":
                    raise pairing.BackendRequestError(400, {"error": "pairing_key_expired"})
                if case == "pending_reply":
                    raise pairing.BackendRequestError(409, {"error": "pairing_redeem_pending", "retry_after_seconds": 1})
                raise TimeoutError
            monkeypatch.setattr(pairing, "_json_request", reply)
        pairing.pair("fixture-key", "https://backend.test", "fixture-device", desktop_enrollment=guard)
    record = json.loads(journal.read_bytes())
    assert ("redeem_request" in record) is req
    if sealed:
        assert record["phase"] == ("applied" if case.startswith("applied") else "redeemed")
    if case in {"old_display", "stale_dispatch", "mixed", "nested", "null_request", "unknown"}:
        record.update({"old_display": {"desktop_failure": "pairing_key_expired"}, "stale_dispatch": {"desktop_dispatch": "a" * 32},
                       "mixed": {"pairing": {}}, "nested": {"desktop_enrollment": {**record["desktop_enrollment"], "key": "fixture"}},
                       "null_request": {"redeem_request": None}, "unknown": {"future_receipt": {"value": "fixture"}}}[case])
        write_atomic(journal, json.dumps(record))
    elif case in {"missing_id", "sealed_missing_id"}:
        identity.unlink()
    elif case == "damaged_id":
        write_atomic(identity, "{broken")
    elif case in {"source_changed", "partial", "off", "unknown_cfg", "applied_off"}:
        raw = json.loads(cfg.read_bytes())
        raw["remote_access"]["vibe_cloud"].update({"source_changed": {"backend_url": "https://changed.invalid"},
            "partial": {"issuer": "fixture"}, "off": {"enabled": False, "instance_id": "old", "instance_secret": "fixture"},
            "applied_off": {"enabled": False}, "unknown_cfg": {"future_credential": "fixture"}}[case])
        write_atomic(cfg, json.dumps(raw))
    elif case == "future_identity":
        write_atomic(identity, '{"schema_version":2,"future_key":"fixture"}')
    elif case == "missing_lock":
        cfg.with_name(".config.json.lock").unlink()
    elif case == "unprotected":
        if os.name == "nt":
            pytest.skip("POSIX fixture; chmod cannot create an untrusted Windows DACL")
        journal.chmod(0o644)
    elif case == "duplicate":
        write_atomic(journal, '{"phase":"redeemed",' + json.dumps(record)[1:])
    elif case == "unreadable":
        write_atomic(journal, "{broken")
    if case not in {"recent", "recent_terminal"}:
        old = time.time_ns() - 120_000_000_000
        os.utime(journal, ns=(old, old))

    def snapshot():
        return [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) if p.exists() else None for p in (journal, cfg, identity)]

    before = snapshot()
    calls = _read_only(monkeypatch)
    with _parent() as parent:
        assert _inspect(parent) == {"v": 1, "event": "result", "outcome": "error", "code": expected}
        assert parent.receive() is None
    assert not calls and snapshot() == before
    if case == "missing_lock":
        assert not cfg.with_name(".config.json.lock").exists()


@pytest.mark.parametrize("change", ["record", "config", "identity", "absent_identity", "root"])
def test_no_classification_survives_a_swapped_file_or_adoption(pairing_host, monkeypatch, change, tmp_path_factory):
    _prepare()
    files = {"config": paths.get_config_path(), "identity": local._identity_path(), "record": pairing._pending_pairing_path()}
    if change == "absent_identity":
        files["identity"].unlink()
    original = legacy.classify
    observed = []

    def while_pinned(*args, **kw):
        code = original(*args, **kw)
        observed.append(code)
        if change == "root":
            monkeypatch.setenv("AVIBE_HOME", str(tmp_path_factory.mktemp("successor")))
        elif change == "absent_identity":
            write_atomic(files["identity"], "fixture-successor")
        else:
            path = files[change]
            write_atomic(path, path.read_bytes())  # identical contents, different generation
        return code

    monkeypatch.setattr(legacy, "classify", while_pinned)
    calls = _read_only(monkeypatch)
    with _parent() as parent:
        assert _inspect(parent) == {"v": 1, "event": "result", "outcome": "error", "code": "local_binding_changed"}
        assert parent.receive() is None
    assert observed == ["legacy_pairing_wait"] and not calls
