"""The admitted inspector can observe a published config, never start or resume pairing."""

import json
import re

import pytest

from config import paths
from config.atomic_io import write_atomic
from tests.test_desktop_claim_pipe import _ORIGIN, _parent
from tests.test_pairing_recovery import _response, pairing_host as pairing_host
from vibe import (
    desktop_claim_admission as gate,
    desktop_claim_pipe as pipe,
    desktop_local_instance as local,
    remote_access,
)


def _inspect(parent, *, admit=True):
    parent.send(op="inspect", origin=_ORIGIN)
    challenge = parent.receive()["challenge"]
    if admit:
        assert gate.acknowledge(_ORIGIN, challenge)
    parent.send(op="begin", challenge=challenge)
    return parent.receive()


@pytest.fixture
def root(pairing_host, monkeypatch):
    path, calls = pairing_host
    assert paths.get_state_dir().parent == path
    paths.get_state_dir().mkdir(exist_ok=True)
    forbidden = []

    def never(*args, **kwargs):
        forbidden.append(True)
        raise AssertionError("read-only observation reached a writer")

    monkeypatch.setattr(pipe, "DesktopClaim", never)
    monkeypatch.setattr(local, "ensure_installation_id", never)
    monkeypatch.setattr(remote_access, "start", never)
    yield path
    assert not forbidden and not calls


def _save_bound(**changes):
    # A published old pairing need not have an installation UUID or an AV-8 claim receipt.
    path = paths.get_config_path()
    payload = json.loads(path.read_text())
    payload["remote_access"]["vibe_cloud"].update(
        {
            **_response("inst_A"),
            "enabled": False,
            "backend_url": "https://avibe.bot",
            "session_secret": "fixture-session",
            **changes,
        }
    )
    write_atomic(path, json.dumps(payload))


@pytest.mark.parametrize(
    "case",
    [
        "off",
        "on",
        "changed_secret",
        "restored_bytes",
        "identity_appears",
        "pending",
        "other_root",
        "foreign_receipt",
        "no_challenge",
        "bad_challenge",
        "arm",
    ],
)
def test_inspector_accepts_only_its_original_publication_and_one_recheck(root, case, monkeypatch, tmp_path_factory):
    _save_bound(enabled=case == "on")
    cfg_path = paths.get_config_path()
    before = cfg_path.read_bytes()
    with _parent() as parent:
        binding = _inspect(parent)
        assert set(binding) == {"v", "event", "instance_id", "generation"}
        assert (binding["v"], binding["event"], binding["instance_id"]) == (1, "binding", "inst_A")
        receipt = binding["generation"]
        assert re.fullmatch(r"[0-9a-f]{64}", receipt)
        if case == "changed_secret":
            _save_bound(instance_secret="fixture-new-generation")
        elif case == "restored_bytes":
            _save_bound(instance_secret="fixture-intermediate-generation")
            write_atomic(cfg_path, before)
        elif case == "identity_appears":
            write_atomic(
                local._identity_path(), '{"schema_version":1,"installation_id":"13345678-1234-1234-1234-123456789abc"}'
            )
        elif case == "pending":
            write_atomic(remote_access._pending_pairing_path(), "fixture-private-journal")
        elif case == "other_root":
            other = tmp_path_factory.mktemp("other-installation")
            write_atomic(other / "config" / "config.json", before)
            (other / "state").mkdir()
            monkeypatch.setenv("AVIBE_HOME", str(other))
        elif case == "foreign_receipt":
            with _parent() as second:
                receipt = _inspect(second)["generation"]
            assert receipt != binding["generation"]
        if case == "arm":
            parent.send(
                op="arm", kind="new", hostname="fixture.avibe.bot", claim_id="not-a-ticket", existing_instance_id=None
            )
        elif case == "no_challenge":
            parent.send(op="recheck", generation=receipt)
        else:
            parent.send(op="recheck", generation=receipt, challenge="X" if case == "bad_challenge" else "b" * 32)
        if case in ("on", "off"):
            assert parent.receive() == {"v": 1, "event": "unchanged", "generation": receipt, "challenge": "b" * 32}
            assert cfg_path.read_bytes() == before and not local._identity_path().exists()
        elif case not in ("arm", "foreign_receipt", "no_challenge", "bad_challenge"):
            assert parent.receive() == {
                "v": 1,
                "event": "result",
                "outcome": "error",
                "code": "pairing_recovery_required" if case == "pending" else "local_binding_changed",
            }
        assert parent.receive() is None


@pytest.mark.parametrize(
    "case", ["empty", "no_ack", "missing", "broken", "bad_identity", "partial", "foreign_backend", "journal"]
)
def test_inspect_without_a_committed_binding_never_repairs_or_starts(root, case):
    path = paths.get_config_path()
    if case == "missing":
        path.unlink()
    elif case == "broken":
        write_atomic(path, "fixture-private-invalid-config")
    elif case == "bad_identity":
        write_atomic(local._identity_path(), "fixture-private-invalid-identity")
    elif case == "partial":
        _save_bound(instance_secret="")
    elif case == "foreign_backend":
        _save_bound(backend_url="https://fixture.invalid")
    elif case == "journal":
        write_atomic(remote_access._pending_pairing_path(), "fixture-private-pending-key")
    before = {
        f: f.read_bytes() for f in (path, local._identity_path(), remote_access._pending_pairing_path()) if f.exists()
    }
    with _parent() as parent:
        result = _inspect(parent, admit=case != "no_ack")
        if case == "no_ack":
            assert result is None
        else:
            if case == "empty":
                assert result == {"v": 1, "event": "no_binding"}
            else:
                code = {"journal": "pairing_recovery_required", "foreign_backend": "invalid_backend"}.get(
                    case, "local_binding_unavailable"
                )
                assert result == {"v": 1, "event": "result", "outcome": "error", "code": code}
            assert parent.receive() is None
    assert before == {
        f: f.read_bytes() for f in (path, local._identity_path(), remote_access._pending_pairing_path()) if f.exists()
    }
    assert not list(path.parent.glob("*.bak*"))
