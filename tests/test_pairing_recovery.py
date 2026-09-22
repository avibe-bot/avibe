"""Pairing recovery contracts through real config and binding consumers."""

from __future__ import annotations

import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from config import paths
from config.v2_config import RemoteAccessConfig, V2Config
from storage import remote_access_authorization_service
from tests.test_remote_access_vibe_cloud import _config
from vibe import api, cli, model_service, remote_access


def _response(instance_id="inst_A"):
    return {
        "instance_id": instance_id,
        "client_id": "fixture-client",
        "issuer": "https://backend.test",
        "authorization_endpoint": "https://backend.test/oauth/authorize",
        "token_endpoint": "https://backend.test/oauth/token",
        "jwks_uri": "https://backend.test/jwks.json",
        "public_url": "https://fixture.avibe.bot",
        "redirect_uri": "https://fixture.avibe.bot/auth/callback",
        "tunnel_token": f"fixture-tunnel-{instance_id}",
        "instance_secret": f"fixture-secret-{instance_id}",
        "instance_kind": "personal",
    }


@pytest.fixture
def pairing_host(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access = RemoteAccessConfig()
    config.save()
    monkeypatch.setattr(
        remote_access, "_resolve_pairing_backend_addresses",
        lambda host, port: (ipaddress.ip_address("93.184.216.34"),),
    )
    calls = []

    def redeem(url, payload, **kwargs):
        calls.append((url, payload["pairing_key"]))
        return _response("inst_B" if payload["pairing_key"] == "key_B" else "inst_A")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    monkeypatch.setattr(remote_access, "start", lambda config: {"ok": True, "running": True})
    monkeypatch.setattr(
        remote_access, "status",
        lambda config=None: {"ok": True, "paired": True, "running": True},
    )
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_service, "request_model_service_refresh", lambda: None)
    return tmp_path, calls


def _fail_config_publication_once(monkeypatch):
    import config.v2_config as config_module

    original = config_module.write_atomic
    remaining = [1]

    def write(path, content, **kwargs):
        if Path(path) == paths.get_config_path() and remaining[0]:
            remaining[0] -= 1
            raise OSError("fixture config publication failure")
        return original(path, content, **kwargs)

    monkeypatch.setattr(config_module, "write_atomic", write)


def _clear_identity():
    return {
        key: ""
        for key in _response()
    } | {
        "enabled": False,
        "backend_url": "",
        "session_secret": "",
    }


def _wait_for_marker(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _write_child_result(path, result):
    path.write_text(json.dumps(result), encoding="utf-8")


def _child_pair(pairing_key, backend_url, result_path):
    try:
        _write_child_result(result_path, remote_access.pair(pairing_key, backend_url))
    except BaseException as exc:
        _write_child_result(result_path, {
            "child_exception": type(exc).__name__,
            "detail": str(exc),
        })


def _child_resume(ready_path, start_path, result_path):
    try:
        ready_path.touch()
        _wait_for_marker(start_path)
        _write_child_result(result_path, remote_access.pair("", ""))
    except BaseException as exc:
        _write_child_result(result_path, {
            "child_exception": type(exc).__name__,
            "detail": str(exc),
        })


def _fork_context():
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process pairing controls require fork")
    return multiprocessing.get_context("fork")


def _save_paired_identity(instance_id="existing"):
    config = V2Config.load()
    cloud = config.remote_access.vibe_cloud
    for field, value in _response(instance_id).items():
        if hasattr(cloud, field):
            setattr(cloud, field, value)
    cloud.backend_url = "https://backend.test"
    cloud.session_secret = f"session-{instance_id}"
    cloud.enabled = True
    config.save()
    return V2Config.load()


def test_recovery_really_saves_credentials_and_binding(pairing_host, monkeypatch):
    root, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    first = remote_access.pair("key_A", "https://backend.test")
    assert first["error"] == "pairing_save_failed_after_redeem"
    journal = root / "state/pending-pairing.json"
    assert journal.is_file()
    if not sys.platform.startswith("win"):
        assert journal.stat().st_mode & 0o777 == 0o600
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ""

    # Repairing an unrelated setting must not throw away redeemed credentials.
    api.save_config({"ui": {"setup_port": 5124}}, validate_remote_access_network=False)
    assert remote_access.pair("", "")["ok"]
    saved = V2Config.load().remote_access.vibe_cloud
    binding = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert saved.instance_id == binding["instance_id"] == "inst_A"
    assert saved.instance_secret == _response()["instance_secret"]
    assert saved.tunnel_token == _response()["tunnel_token"]
    assert saved.session_secret
    assert not journal.exists()
    assert len(calls) == 1


def test_cli_does_not_prompt_for_unrecoverable_pending_record(pairing_host, monkeypatch, capsys):
    root, _ = pairing_host
    journal = root / "state/pending-pairing.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"schema_version": 1, "phase": "prepared"}', encoding="utf-8")

    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda prompt: pytest.fail("an existing pending operation must not prompt for a new key"),
    )
    result = cli.cmd_remote_pair(SimpleNamespace(
        pairing_key=None,
        backend_url="https://avibe.bot",
        device_name="fixture",
        json=True,
    ))
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "pairing_recovery_invalid"


def test_pair_preflight_lock_failure_never_reaches_redeem(pairing_host, monkeypatch):
    _, calls = pairing_host

    class FailingLock:
        def __enter__(self):
            raise OSError("fixture preflight lock failure")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(remote_access, "config_file_lock", lambda: FailingLock())
    result = remote_access.pair("key_A", "https://backend.test")
    assert result["error"] == "pairing_local_write_unavailable"
    assert "config lock" in result["detail"]
    assert not calls


def test_pair_sqlite_initialization_failure_never_reaches_redeem(pairing_host, monkeypatch):
    _, calls = pairing_host
    monkeypatch.setattr(remote_access, "_run_pending_deferred_context_migration", lambda: None)

    def fail_sqlite():
        raise OSError("fixture sqlite initialization failure")

    monkeypatch.setattr("storage.importer.ensure_sqlite_state", fail_sqlite)
    result = remote_access.pair("key_A", "https://backend.test")
    assert result["error"] == "pairing_local_write_unavailable"
    assert "sqlite initialization failure" in result["detail"]
    assert not calls


def test_recovery_lock_entry_failure_is_structured(pairing_host, monkeypatch):
    class FailingLock:
        def __enter__(self):
            raise OSError("fixture recovery lock failure")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(remote_access, "config_file_lock", lambda: FailingLock())
    result = remote_access.pair("", "")
    assert result["error"] == "pairing_recovery_unavailable"
    assert "recovery lock failure" in result["detail"]


def test_recovery_second_lock_entry_failure_is_structured(pairing_host, monkeypatch):
    class FailingSecondLock:
        entries = 0

        def __enter__(self):
            self.entries += 1
            if self.entries == 2:
                raise OSError("fixture second recovery lock failure")
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    lock = FailingSecondLock()
    monkeypatch.setattr(remote_access, "config_file_lock", lambda: lock)
    result = remote_access.pair("", "")
    assert result["error"] == "pairing_recovery_unavailable"
    assert "second recovery lock failure" in result["detail"]
    assert lock.entries == 2


def test_prepared_claim_has_stable_session_secret_before_redeem(pairing_host, monkeypatch):
    root, _ = pairing_host
    observed = []

    def redeem(url, payload, **kwargs):
        observed.append(
            json.loads((root / "state/pending-pairing.json").read_text(encoding="utf-8"))
        )
        return _response()

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    assert remote_access.pair("key_A", "https://backend.test")["ok"]
    assert observed[0]["phase"] == "prepared"
    assert observed[0]["session_secret"]
    assert (
        V2Config.load().remote_access.vibe_cloud.session_secret
        == observed[0]["session_secret"]
    )


def test_definitive_redeem_failure_retires_prepared_claim(pairing_host, monkeypatch):
    root, _ = pairing_host
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(400, {"error": "invalid_pairing_key"})
        ),
    )
    result = remote_access.pair("key_bad", "https://backend.test")
    assert result["error"] == "invalid_pairing_key"
    assert not (root / "state/pending-pairing.json").exists()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_definitive_failure_retirement_can_be_retried_through_cli(
    pairing_host,
    monkeypatch,
    capsys,
    language,
):
    root, calls = pairing_host
    journal = root / "state/pending-pairing.json"
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)

    def redeem(url, payload, **kwargs):
        calls.append(payload["pairing_key"])
        if payload["pairing_key"] == "key_bad":
            raise remote_access.BackendRequestError(400, {"error": "invalid_pairing_key"})
        return _response()

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    monkeypatch.setattr(
        cli.getpass, "getpass",
        lambda _: pytest.fail("retirement must not prompt or redeem"),
    )
    original_unlink = Path.unlink

    def fail_journal_unlink(path, missing_ok=False):
        if path == journal:
            assert json.loads(path.read_text(encoding="utf-8"))["phase"] == "retirement_pending"
            raise OSError("fixture definitive retirement failure")
        return original_unlink(path, missing_ok=missing_ok)

    args = SimpleNamespace(pairing_key=None)
    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    assert cli.cmd_remote_pair(SimpleNamespace(
        pairing_key="key_bad", backend_url="https://backend.test",
    )) == 1
    first_output = capsys.readouterr().err
    assert ("was not applied" if language == "en" else "尚未应用") in first_output
    assert "vibe remote pair" in first_output
    assert cli.cmd_remote_pair(args) == 1
    assert journal.exists()
    assert ("was not applied" if language == "en" else "尚未应用") in capsys.readouterr().err

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert cli.cmd_remote_pair(args) == 1
    output = capsys.readouterr().err
    assert ("was cleared" if language == "en" else "已清理") in output
    assert "vibe remote pair" in output
    assert not journal.exists()
    assert calls == ["key_bad"]
    assert not V2Config.load().remote_access.vibe_cloud.is_runtime_paired()

    # A subsequent normal CLI invocation can securely ask for the replacement.
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "key_good")
    assert cli.cmd_remote_pair(args) == 0
    assert calls == ["key_bad", "key_good"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


def test_definitive_failure_marker_write_failure_does_not_promise_recovery(
    pairing_host, monkeypatch, capsys,
):
    root, _ = pairing_host
    write_record = remote_access._write_pending_pairing_record

    def fail_terminal_record(record):
        if record["phase"] == "retirement_pending":
            raise OSError("fixture terminal marker write failure")
        return write_record(record)

    monkeypatch.setattr(remote_access, "_write_pending_pairing_record", fail_terminal_record)
    monkeypatch.setattr(
        remote_access, "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            remote_access.BackendRequestError(400, {"error": "invalid_pairing_key"})
        ),
    )
    result = remote_access.pair("key_bad", "https://backend.test")
    assert result["error"] == "pairing_retirement_failed"
    assert result["pairing"]["retirement_pending"] is False
    journal = root / "state/pending-pairing.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "prepared"
    assert remote_access.pair("", "")["error"] == "pairing_recovery_not_ready"
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "en")
    cli._print_remote_pair_failure(result)
    output = capsys.readouterr().err
    assert "was not applied" in output
    assert "keyless retry cannot recover" in output


@pytest.mark.parametrize(
    "failure",
    [OSError("fixture timeout"), remote_access.BackendRequestError(503, {"error": "backend_http_error"})],
)
def test_uncertain_redeem_failure_retains_prepared_claim(pairing_host, monkeypatch, failure):
    root, _ = pairing_host
    monkeypatch.setattr(
        remote_access,
        "_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    result = remote_access.pair("key_unknown", "https://backend.test")
    assert result["ok"] is False
    assert json.loads(
        (root / "state/pending-pairing.json").read_text(encoding="utf-8")
    )["phase"] == "prepared"
    assert remote_access.pair("", "")["error"] == "pairing_recovery_not_ready"


@pytest.mark.parametrize(
    "response",
    [
        {**_response(), "client_id": 123},
        {
            **_response(),
            "tunnel_origin_update": {
                "ok": False,
                "error": "tunnel_origin_update_failed",
            },
        },
    ],
)
def test_definitive_response_failure_retires_prepared_claim(
    pairing_host,
    monkeypatch,
    response,
):
    root, _ = pairing_host
    monkeypatch.setattr(remote_access, "_json_request", lambda *args, **kwargs: response)
    result = remote_access.pair("key_bad_response", "https://backend.test")
    assert result["ok"] is False
    assert not (root / "state/pending-pairing.json").exists()


@pytest.mark.parametrize("phase", [[], {}])
def test_non_string_pending_phase_fails_closed(pairing_host, phase):
    root, _ = pairing_host
    journal = root / "state/pending-pairing.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({
            "schema_version": 1,
            "operation_id": "fixture-operation",
            "phase": phase,
        }),
        encoding="utf-8",
    )
    result = remote_access.pair("", "")
    assert result["error"] == "pairing_recovery_invalid"


def test_retirement_failure_preserves_applied_record_for_local_retry(pairing_host, monkeypatch):
    root, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair("key_A", "https://backend.test")["error"] == (
        "pairing_save_failed_after_redeem"
    )

    journal = root / "state/pending-pairing.json"
    started = []
    monkeypatch.setattr(
        remote_access,
        "start",
        lambda config: started.append(config) or {"ok": True, "running": True},
    )
    original_unlink = Path.unlink

    def fail_journal_unlink(path, missing_ok=False):
        if path == journal:
            raise OSError("fixture pending record retirement failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    result = remote_access.pair("", "")
    assert result["error"] == "pairing_retirement_failed"
    assert len(calls) == 1
    assert not started

    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.instance_id == "inst_A"
    binding = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert binding["instance_id"] == "inst_A"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "applied"

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert remote_access.pair("", "")["ok"]
    assert not journal.exists()
    assert len(calls) == 1


def test_cli_resumes_without_asking_for_another_key(pairing_host, monkeypatch, capsys):
    _, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert not remote_access.pair("key_A", "https://backend.test")["ok"]

    def unexpected_prompt(*args):
        pytest.fail("recoverable pairing must not solicit a new key")

    monkeypatch.setattr(cli.getpass, "getpass", unexpected_prompt)
    result = cli.cmd_remote_pair(SimpleNamespace(
        pairing_key=None, backend_url="https://avibe.bot", device_name="fixture", json=True,
    ))
    assert result == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"
    assert len(calls) == 1


def test_explicit_key_and_backend_supersede_pending(pairing_host, monkeypatch):
    _, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert not remote_access.pair("key_A", "https://backend.test")["ok"]
    assert remote_access.pair("key_B", "https://other-backend.test")["ok"]
    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.instance_id == "inst_B"
    assert saved.backend_url == "https://other-backend.test"
    assert [key for _, key in calls] == ["key_A", "key_B"]


def test_new_backend_is_validated_before_pending_selection(pairing_host, monkeypatch):
    _, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert not remote_access.pair("key_A", "https://backend.test")["ok"]
    assert not remote_access.pair("key_B", "file:///fixture")["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ""
    assert len(calls) == 1
    assert remote_access.pair("", "")["ok"]


@pytest.mark.parametrize("instance_id", ["../../unrelated", "absolute", "实例/测试"])
def test_remote_id_never_addresses_a_local_file(pairing_host, monkeypatch, instance_id):
    root, calls = pairing_host
    unrelated = root / "unrelated.json"
    original = '{"unrelated_user_data":true}'
    unrelated.write_text(original, encoding="utf-8")
    if instance_id == "absolute":
        instance_id = str(unrelated.with_suffix(""))

    def redeem(url, payload, **kwargs):
        calls.append((url, payload["pairing_key"]))
        return _response(instance_id)

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    assert remote_access.pair("key_A", "https://backend.test")["ok"]
    assert unrelated.read_text(encoding="utf-8") == original
    assert V2Config.load().remote_access.vibe_cloud.instance_id == instance_id
    assert not (root / "state/pending-pairing.json").exists()


def test_unusable_journal_blocks_before_redeem(pairing_host):
    root, calls = pairing_host
    journal = root / "state/pending-pairing.json"
    journal.mkdir(parents=True)
    result = remote_access.pair("key_A", "https://backend.test")
    assert result["error"] == "pairing_local_write_unavailable"
    assert not calls
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ""


def test_cleared_persisted_identity_cannot_be_restored(pairing_host, monkeypatch):
    root, calls = pairing_host
    monkeypatch.setattr(
        remote_access, "_transition_instance_binding",
        lambda **kwargs: {"ok": False, "error": "fixture reconciliation failure"},
    )
    assert remote_access.pair("key_A", "https://backend.test")["error"] == "pairing_reconciliation_failed"
    # Restore the complete original empty identity, not only a subset of keys:
    # equality with a journal's source identity alone cannot detect this ABA.
    api.save_config(
        {"remote_access": {"vibe_cloud": _clear_identity()}},
        validate_remote_access_network=False,
    )
    result = remote_access.pair("", "")
    assert not result["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ""
    assert len(calls) == 1
    assert not (root / "state/pending-pairing.json").exists()


def test_malformed_response_is_not_replayable_and_explicit_key_recovers(pairing_host, monkeypatch):
    _, calls = pairing_host
    responses = [
        {**_response(), "client_id": 123},
        _response("inst_B"),
    ]

    def redeem(url, payload, **kwargs):
        calls.append((url, payload["pairing_key"]))
        return responses.pop(0)

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    malformed = remote_access.pair("key_A", "https://backend.test")
    assert malformed["error"] == "invalid_pairing_response"
    assert remote_access.pair("", "")["error"] == "missing_pairing_key"
    assert remote_access.pair("key_B", "https://backend.test")["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_B"
    assert [key for _, key in calls] == ["key_A", "key_B"]


def test_malformed_record_fails_closed_but_new_operation_replaces_it(pairing_host):
    root, calls = pairing_host
    record = root / "state/pending-pairing.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('{"schema_version": 1, "phase": "redeemed"}', encoding="utf-8")

    assert remote_access.pair("", "")["error"] == "pairing_recovery_invalid"
    assert remote_access.pair("key_A", "https://backend.test")["ok"]
    assert len(calls) == 1


def test_record_backend_url_must_be_typed_before_recovery(pairing_host, monkeypatch):
    root, calls = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair("key_A", "https://backend.test")["error"] == (
        "pairing_save_failed_after_redeem"
    )
    journal = root / "state/pending-pairing.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["pairing"]["backend_url"] = 123
    journal.write_text(json.dumps(payload), encoding="utf-8")

    assert remote_access.pair("", "")["error"] == "pairing_recovery_invalid"
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode semantics")
def test_atomic_replace_accepts_readonly_config_inode_in_writable_parent(pairing_host):
    root, _ = pairing_host
    config_path = paths.get_config_path()
    os.chmod(config_path, 0o444)
    assert remote_access.pair("key_A", "https://backend.test")["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"
    assert (config_path.stat().st_mode & 0o777) == 0o600


def test_redeem_response_publication_failure_is_indeterminate(pairing_host, monkeypatch):
    _, calls = pairing_host
    import vibe.remote_access as module

    original = module.write_atomic
    writes = [0]

    def fail_redeemed_record(path, content, **kwargs):
        writes[0] += 1
        if writes[0] == 2:
            raise OSError("fixture journal publication failure")
        return original(path, content, **kwargs)

    monkeypatch.setattr(module, "write_atomic", fail_redeemed_record)
    result = remote_access.pair("key_A", "https://backend.test")
    assert result["error"] == "pairing_redeem_indeterminate"
    assert len(calls) == 1
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ""
    assert remote_access.pair("", "")["error"] == "pairing_recovery_not_ready"


def test_config_lock_failure_after_redeem_is_indeterminate(pairing_host, monkeypatch):
    class FailingLock:
        def __enter__(self):
            raise OSError("fixture config lock failure")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    def redeem(url, payload, **kwargs):
        monkeypatch.setattr(remote_access, "config_file_lock", lambda: FailingLock())
        return _response()

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    result = remote_access.pair("key_A", "https://backend.test")
    assert result["error"] == "pairing_redeem_indeterminate"
    assert "config lock" in result["detail"]


def test_clear_write_failure_keeps_revocation_fence(pairing_host, monkeypatch):
    root, _ = pairing_host
    config = V2Config.load()
    cloud = config.remote_access.vibe_cloud
    for field, value in _response().items():
        if hasattr(cloud, field):
            setattr(cloud, field, value)
    cloud.backend_url = "https://backend.test"
    cloud.session_secret = "existing-session-secret"
    cloud.enabled = True
    config.save()
    config = V2Config.load()
    claim = remote_access._new_pairing_claim(config, "https://backend.test", "fixture")
    with remote_access.config_file_lock():
        remote_access._write_pending_pairing_record(claim)

    import config.v2_config as config_module

    original = config_module.write_atomic

    def fail_config_only(path, content, **kwargs):
        if Path(path) == paths.get_config_path():
            raise OSError("fixture clear publication failure")
        return original(path, content, **kwargs)

    monkeypatch.setattr(config_module, "write_atomic", fail_config_only)
    with pytest.raises(OSError, match="fixture clear publication failure"):
        api.save_config(
            {"remote_access": {"vibe_cloud": _clear_identity()}},
            validate_remote_access_network=False,
        )
    saved = json.loads((root / "state/pending-pairing.json").read_text(encoding="utf-8"))
    assert saved["phase"] == "revoked"
    assert remote_access.pair("", "")["error"] == "pairing_recovery_revoked"


def test_clear_retirement_failure_keeps_revocation_fence_and_logs(pairing_host, monkeypatch, caplog):
    root, _ = pairing_host
    config = V2Config.load()
    cloud = config.remote_access.vibe_cloud
    for field, value in _response().items():
        if hasattr(cloud, field):
            setattr(cloud, field, value)
    cloud.backend_url = "https://backend.test"
    cloud.session_secret = "existing-session-secret"
    cloud.enabled = True
    config.save()
    config = V2Config.load()
    claim = remote_access._new_pairing_claim(config, "https://backend.test", "fixture")
    with remote_access.config_file_lock():
        remote_access._write_pending_pairing_record(claim)

    journal = root / "state/pending-pairing.json"
    original_unlink = Path.unlink

    def fail_journal_unlink(path, missing_ok=False):
        if path == journal:
            raise OSError("fixture revoked record retirement failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    caplog.set_level("ERROR")
    api.save_config(
        {"remote_access": {"vibe_cloud": _clear_identity()}},
        validate_remote_access_network=False,
    )
    monkeypatch.setattr(Path, "unlink", original_unlink)

    saved = V2Config.load().remote_access.vibe_cloud
    assert not saved.is_runtime_paired()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "revoked"
    assert any("safety fence remains" in record.message for record in caplog.records)

    api.save_config(
        {"ui": {"setup_port": 5124}},
        validate_remote_access_network=False,
    )
    assert not journal.exists()


def test_revoked_fence_is_reused_after_clear_save_failure(pairing_host, monkeypatch):
    root, _ = pairing_host
    config = _save_paired_identity()
    claim = remote_access._new_pairing_claim(config, "https://backend.test", "fixture")
    with remote_access.config_file_lock():
        remote_access._write_pending_pairing_record(claim)

    import config.v2_config as config_module

    original_write = config_module.write_atomic
    fail_once = [True]

    def fail_config_once(path, content, **kwargs):
        if Path(path) == paths.get_config_path() and fail_once[0]:
            fail_once[0] = False
            raise OSError("fixture first clear publication failure")
        return original_write(path, content, **kwargs)

    monkeypatch.setattr(config_module, "write_atomic", fail_config_once)
    with pytest.raises(OSError, match="first clear publication failure"):
        api.save_config(
            {"remote_access": {"vibe_cloud": _clear_identity()}},
            validate_remote_access_network=False,
        )
    journal = root / "state/pending-pairing.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "revoked"

    monkeypatch.setattr(config_module, "write_atomic", original_write)
    api.save_config(
        {"remote_access": {"vibe_cloud": _clear_identity()}},
        validate_remote_access_network=False,
    )
    assert not journal.exists()
    assert not V2Config.load().remote_access.vibe_cloud.is_runtime_paired()


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        json.dumps({"schema_version": 1, "phase": "prepared"}),
    ],
)
def test_unpaired_settings_save_ignores_unreadable_pending_record(
    pairing_host,
    contents,
):
    root, _ = pairing_host
    journal = root / "state/pending-pairing.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(contents, encoding="utf-8")

    saved = api.save_config(
        {"ui": {"setup_port": 5124}},
        validate_remote_access_network=False,
    )
    assert saved.ui.setup_port == 5124
    assert journal.exists()


@pytest.mark.parametrize("contents", ["{", json.dumps({"schema_version": 1, "phase": "prepared"})])
def test_paired_to_unpaired_save_fails_closed_on_unreadable_record(
    pairing_host,
    contents,
):
    root, _ = pairing_host
    _save_paired_identity()
    journal = root / "state/pending-pairing.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(contents, encoding="utf-8")

    with pytest.raises(remote_access._PairingRevocationUnavailable):
        api.save_config(
            {"remote_access": {"vibe_cloud": _clear_identity()}},
            validate_remote_access_network=False,
        )
    assert V2Config.load().remote_access.vibe_cloud.is_runtime_paired()
    assert journal.exists()


def test_clear_fences_an_applied_record_after_retirement_failure(pairing_host, monkeypatch):
    root, _ = pairing_host
    _save_paired_identity()
    config = V2Config.load()
    response = _response("existing") | {
        "backend_url": "https://backend.test",
        "instance_kind": "personal",
    }
    session_secret = "existing-session-secret"
    claim = remote_access._new_pairing_claim(config, "https://backend.test", "fixture")
    target_identity = remote_access._pairing_target_identity(response, session_secret)
    record = {
        **claim,
        "phase": "applied",
        "pairing": remote_access._pairing_response_payload(response, session_secret),
        "target_identity": target_identity,
        "target_fingerprint": remote_access._pairing_identity_fingerprint(target_identity),
        "applied_at": 1.0,
    }
    with remote_access.config_file_lock():
        remote_access._write_pending_pairing_record(record)

    journal = root / "state/pending-pairing.json"
    original_unlink = Path.unlink

    def fail_journal_unlink(path, missing_ok=False):
        if path == journal:
            raise OSError("fixture applied record retirement failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    api.save_config(
        {"remote_access": {"vibe_cloud": _clear_identity()}},
        validate_remote_access_network=False,
    )
    monkeypatch.setattr(Path, "unlink", original_unlink)

    assert not V2Config.load().remote_access.vibe_cloud.is_runtime_paired()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "revoked"
    assert remote_access.pair("", "")["error"] == "pairing_recovery_revoked"


def test_unrelated_save_during_redeem_does_not_revoke_claim(pairing_host, monkeypatch):
    _, calls = pairing_host
    entered = threading.Event()
    release = threading.Event()

    def redeem(url, payload, **kwargs):
        calls.append((url, payload["pairing_key"]))
        entered.set()
        assert release.wait(timeout=5)
        return _response("inst_A")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    result_holder = []
    worker = threading.Thread(
        target=lambda: result_holder.append(remote_access.pair("key_A", "https://backend.test")),
    )
    worker.start()
    assert entered.wait(timeout=5)
    api.save_config({"ui": {"setup_port": 5124}}, validate_remote_access_network=False)
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result_holder[0]["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


def test_late_response_cannot_override_new_claim(pairing_host, monkeypatch):
    _, calls = pairing_host
    entered = threading.Event()
    release = threading.Event()

    def redeem(url, payload, **kwargs):
        key = payload["pairing_key"]
        calls.append((url, key))
        if key == "key_A":
            entered.set()
            assert release.wait(timeout=5)
            return _response("inst_A")
        return _response("inst_B")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    result_a = []
    worker = threading.Thread(
        target=lambda: result_a.append(remote_access.pair("key_A", "https://backend.test")),
    )
    worker.start()
    assert entered.wait(timeout=5)
    result_b = remote_access.pair("key_B", "https://other-backend.test")
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result_b["ok"]
    assert result_a[0]["error"] == "pairing_superseded_after_redeem"
    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.instance_id == "inst_B"
    assert saved.backend_url == "https://other-backend.test"


def test_definitive_failure_cannot_retire_superseding_claim(pairing_host, monkeypatch):
    _, calls = pairing_host
    entered = threading.Event()
    release = threading.Event()

    def redeem(url, payload, **kwargs):
        key = payload["pairing_key"]
        calls.append((url, key))
        if key == "key_A":
            entered.set()
            assert release.wait(timeout=5)
            raise remote_access.BackendRequestError(
                400, {"error": "invalid_pairing_key"}
            )
        return _response("inst_B")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    result_a = []
    worker = threading.Thread(
        target=lambda: result_a.append(remote_access.pair("key_A", "https://backend.test")),
    )
    worker.start()
    assert entered.wait(timeout=5)
    result_b = remote_access.pair("key_B", "https://other-backend.test")
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result_b["ok"]
    assert result_a[0]["error"] == "invalid_pairing_key"
    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.instance_id == "inst_B"
    assert saved.backend_url == "https://other-backend.test"
    assert not (Path(paths.get_state_dir()) / "pending-pairing.json").exists()


def test_concurrent_resume_has_one_owner(pairing_host, monkeypatch):
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair("key_A", "https://backend.test")["error"] == "pairing_save_failed_after_redeem"
    barrier = threading.Barrier(3)
    results = []

    def resume():
        barrier.wait()
        results.append(remote_access.pair("", ""))

    workers = [threading.Thread(target=resume) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)
    assert all(not worker.is_alive() for worker in workers)
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("error") == "missing_pairing_key" for result in results) == 1
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


def test_concurrent_resume_has_one_owner_across_processes(pairing_host, monkeypatch):
    ctx = _fork_context()
    root, _ = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair("key_A", "https://backend.test")["error"] == (
        "pairing_save_failed_after_redeem"
    )
    process_dir = root / "process-resume"
    process_dir.mkdir()
    start_path = process_dir / "start"
    workers = [
        ctx.Process(
            target=_child_resume,
            args=(
                process_dir / f"ready-{index}",
                start_path,
                process_dir / f"result-{index}.json",
            ),
        )
        for index in range(2)
    ]
    for worker in workers:
        worker.start()
    for index in range(2):
        _wait_for_marker(process_dir / f"ready-{index}")
    start_path.touch()
    result_paths = [process_dir / f"result-{index}.json" for index in range(2)]
    for result_path in result_paths:
        _wait_for_marker(result_path)
    results = [
        json.loads(result_path.read_text(encoding="utf-8"))
        for result_path in result_paths
    ]
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert worker.exitcode == 0
    assert all("child_exception" not in result for result in results)
    assert sum(bool(result.get("ok")) for result in results) == 1
    assert sum(result.get("error") == "missing_pairing_key" for result in results) == 1
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


def test_old_response_in_process_cannot_override_new_claim(pairing_host, monkeypatch):
    ctx = _fork_context()
    root, _ = pairing_host
    process_dir = root / "process-late-response"
    process_dir.mkdir()
    entered = process_dir / "redeem-entered"
    release = process_dir / "redeem-release"
    result_path = process_dir / "result.json"

    def redeem(url, payload, **kwargs):
        if payload["pairing_key"] == "key_A":
            entered.touch()
            _wait_for_marker(release)
            return _response("inst_A")
        return _response("inst_B")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    worker = ctx.Process(
        target=_child_pair,
        args=("key_A", "https://backend.test", result_path),
    )
    worker.start()
    _wait_for_marker(entered)
    result_b = remote_access.pair("key_B", "https://other-backend.test")
    release.touch()
    _wait_for_marker(result_path)
    result_a = json.loads(result_path.read_text(encoding="utf-8"))
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert worker.exitcode == 0
    assert result_b["ok"]
    assert result_a["error"] == "pairing_superseded_after_redeem"
    saved = V2Config.load().remote_access.vibe_cloud
    assert saved.instance_id == "inst_B"
    assert saved.backend_url == "https://other-backend.test"


def test_clear_during_redeem_fences_old_response_across_processes(pairing_host, monkeypatch):
    ctx = _fork_context()
    root, _ = pairing_host
    _save_paired_identity()
    process_dir = root / "process-clear"
    process_dir.mkdir()
    entered = process_dir / "redeem-entered"
    release = process_dir / "redeem-release"
    result_path = process_dir / "result.json"

    def redeem(url, payload, **kwargs):
        entered.touch()
        _wait_for_marker(release)
        return _response("inst_A")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    worker = ctx.Process(
        target=_child_pair,
        args=("key_A", "https://backend.test", result_path),
    )
    worker.start()
    _wait_for_marker(entered)
    api.save_config(
        {"remote_access": {"vibe_cloud": _clear_identity()}},
        validate_remote_access_network=False,
    )
    release.touch()
    _wait_for_marker(result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert worker.exitcode == 0
    assert result["error"] == "pairing_superseded_after_redeem"
    assert not V2Config.load().remote_access.vibe_cloud.is_runtime_paired()
    assert not (paths.get_state_dir() / "pending-pairing.json").exists()


def test_ordinary_save_during_redeem_does_not_revoke_across_processes(pairing_host, monkeypatch):
    ctx = _fork_context()
    root, _ = pairing_host
    _save_paired_identity()
    process_dir = root / "process-ordinary-save"
    process_dir.mkdir()
    entered = process_dir / "redeem-entered"
    release = process_dir / "redeem-release"
    result_path = process_dir / "result.json"

    def redeem(url, payload, **kwargs):
        entered.touch()
        _wait_for_marker(release)
        return _response("inst_A")

    monkeypatch.setattr(remote_access, "_json_request", redeem)
    worker = ctx.Process(
        target=_child_pair,
        args=("key_A", "https://backend.test", result_path),
    )
    worker.start()
    _wait_for_marker(entered)
    api.save_config({"ui": {"setup_port": 5124}}, validate_remote_access_network=False)
    assert json.loads(
        (paths.get_state_dir() / "pending-pairing.json").read_text(encoding="utf-8")
    )["phase"] == "prepared"
    release.touch()
    _wait_for_marker(result_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert worker.exitcode == 0
    assert result["ok"]
    assert V2Config.load().remote_access.vibe_cloud.instance_id == "inst_A"


@pytest.mark.parametrize(
    ("language", "error", "needle"),
    [
        ("en", "pairing_save_failed_after_redeem", "cloud-side"),
        ("zh", "pairing_save_failed_after_redeem", "云端"),
        ("en", "pairing_recovery_unavailable", "could not start"),
        ("zh", "pairing_recovery_unavailable", "无法启动"),
        ("en", "pairing_retirement_failed", "retired"),
        ("zh", "pairing_retirement_failed", "清理"),
    ],
)
def test_pairing_recovery_cli_messages_use_supported_locales(
    monkeypatch,
    capsys,
    language,
    error,
    needle,
):
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    cli._print_remote_pair_failure({
        "error": error,
        "orphaned_binding": {"instance_id": "inst_A", "device_name": "fixture"},
    })
    output = capsys.readouterr().err
    assert needle in output
    if error == "pairing_retirement_failed":
        assert "vibe remote pair" in output


def test_guided_remote_setup_retries_pending_retirement_without_prompt(
    pairing_host,
    monkeypatch,
):
    _fail_config_publication_once(monkeypatch)
    assert not remote_access.pair("key_A", "https://backend.test")["ok"]

    monkeypatch.setattr(
        cli,
        "_print_remote_setup_intro",
        lambda: pytest.fail("pending recovery must skip the setup intro"),
    )
    monkeypatch.setattr(
        cli,
        "_wait_for_pairing_key_ready",
        lambda: pytest.fail("pending recovery must not prompt for a key"),
    )
    assert cli.cmd_remote_setup(SimpleNamespace(remote_command=None)) == 0


def test_guided_remote_setup_retries_persisted_applied_record(
    pairing_host,
    monkeypatch,
):
    root, _ = pairing_host
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair("key_A", "https://backend.test")["error"] == (
        "pairing_save_failed_after_redeem"
    )
    journal = root / "state/pending-pairing.json"
    original_unlink = Path.unlink
    started = []
    monkeypatch.setattr(
        remote_access,
        "start",
        lambda config: started.append(config) or {"ok": True, "running": True},
    )

    def fail_journal_unlink(path, missing_ok=False):
        if path == journal:
            raise OSError("fixture applied retirement failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    first = remote_access.pair("", "")
    assert first["error"] == "pairing_retirement_failed"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "applied"
    assert V2Config.load().remote_access.vibe_cloud.is_runtime_paired()
    binding = remote_access_authorization_service.load_instance_binding_state(ensure=False)
    assert binding["instance_id"] == "inst_A"
    assert not started

    assert cli.cmd_remote_setup(SimpleNamespace(remote_command=None)) == 1
    assert not started

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert cli.cmd_remote_setup(SimpleNamespace(remote_command=None)) == 0
    assert len(started) == 1
    assert not journal.exists()
