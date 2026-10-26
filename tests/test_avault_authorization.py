"""avault's one-time macOS Keychain authorization, against fake avault binaries.

The contract is described in ``tests/scenario_harness/avault_keychain.py``; the
closed loop through the Web action is AUTH-SETUP-134.
"""

from __future__ import annotations

import threading
import time

import pytest

from storage.vault_crypto import Sealed
from tests.scenario_harness.avault_keychain import use_fake_avault_on_mac
from vibe import api, cli


@pytest.fixture
def mac_avault(monkeypatch, tmp_path):
    """Point Avibe at a fake avault on a Mac; return a factory taking the fake's options."""
    return lambda **options: use_fake_avault_on_mac(monkeypatch, tmp_path, **options)


def _secret() -> list[dict]:
    return [{"name": "API_TOKEN", "env": "API_TOKEN", "envelope": Sealed(ciphertext="ct", nonce="n", wrap_meta="wm")}]


def test_exit_77_reports_authorization_required_until_the_key_reads_again(mac_avault):
    avault = mac_avault()

    with pytest.raises(api.AvaultAuthorizationRequiredError) as exc:
        api.avault_pubkey()

    # Nothing left avault, so one-shot grants are released rather than consumed.
    assert isinstance(exc.value, api.AvaultPreHandoffError)
    assert api.get_avault_authorization_status()["required"] is True
    row = api.dependencies_status(dependency_ids=["avault"])["deps"][0]
    assert row["status"] == "ready" and row["authorization_required"] is True

    avault.flag("authorized")
    assert api.avault_pubkey() == {"public_key": "pk", "fingerprint": "fp"}
    assert api.get_avault_authorization_status()["required"] is False


def test_stale_login_session_replaces_raw_avault_failure(mac_avault, monkeypatch):
    mac_avault()
    monkeypatch.setattr(api, "macos_login_session_state", lambda: "stale_login_session")

    with pytest.raises(api.AvaultStaleLoginSessionError) as exc:
        api.avault_pubkey()

    assert exc.value.code == "stale_login_session"
    with pytest.raises(api.AvaultStaleLoginSessionError) as exc:
        api.avault_deliver_run(_secret(), ["sh", "-c", "exit 0"])
    assert isinstance(exc.value, api.AvaultPreHandoffError)
    status = api.get_avault_authorization_status()
    assert status["login_session_state"] == "stale_login_session"


def test_target_service_state_comes_from_the_running_service(monkeypatch):
    from vibe import internal_client

    monkeypatch.setattr(api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(internal_client, "health_identity_sync", lambda: {
        "ok": True,
        "service": "vibe-remote-internal",
        "version": 1,
        "macos_login_session_state": "stale_login_session",
    })

    assert api.macos_login_session_state(service_pid=4242) == "stale_login_session"


def test_avault_without_the_contract_keeps_todays_failures(mac_avault):
    avault = mac_avault(version="0.1.7", unauthorized_status=70)

    with pytest.raises(api.AvaultError) as exc:
        api.avault_pubkey()
    assert not isinstance(exc.value, api.AvaultAuthorizationRequiredError)
    assert api.get_avault_authorization_status()["required"] is False

    # Its child's 77 is just the child's exit code; a pre-0.1.8 avault is never
    # probed again, since its background Keychain reads may still prompt.
    avault.flag("authorized")
    before = len(avault.calls())
    assert api.avault_deliver_run(_secret(), ["sh", "-c", "exit 77"])["exit_code"] == 77
    assert [call for call in avault.calls()[before:] if call.startswith("pubkey")] == []


def test_deliver_run_77_is_authorization_only_when_the_master_key_check_agrees(mac_avault):
    avault = mac_avault()

    with pytest.raises(api.AvaultAuthorizationRequiredError) as exc:
        api.avault_deliver_run(_secret(), ["sh", "-c", "exit 0"])
    # The probe cannot prove no child ran, so the delivery stays fail-closed.
    assert not isinstance(exc.value, api.AvaultPreHandoffError)
    assert api.get_avault_authorization_status()["required"] is True

    avault.flag("authorized")
    assert api.avault_deliver_run(_secret(), ["sh", "-c", "exit 77"]) == {"exit_code": 77, "delivered": True}
    assert api.get_avault_authorization_status()["required"] is False


def test_agent_authorization_frame_becomes_the_same_pre_handoff_error(mac_avault, monkeypatch):
    from vibe.avault_agent import AvaultAgentAuthorizationRequiredError

    mac_avault()

    class Client:
        def deliver_inject(self, **_kwargs):
            raise AvaultAgentAuthorizationRequiredError("macOS Keychain needs a one-time authorization")

    monkeypatch.setattr(api, "_avault_agent_client", Client)
    monkeypatch.setattr(api, "_require_avault_grant_delivery_surface", lambda _feature: None)

    with pytest.raises(api.AvaultPreHandoffError) as exc:
        api.avault_agent_deliver_inject(grant_id="vgr_1", path="/tmp/out.env", fmt="dotenv", secrets=[])
    assert isinstance(exc.value, api.AvaultAuthorizationRequiredError)
    assert api.get_avault_authorization_status()["required"] is True


def _wait_for_attempt() -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        attempt = api.get_avault_authorization_status()["attempt"]
        if attempt and attempt["state"] != "running":
            return attempt
        time.sleep(0.02)
    raise AssertionError("authorization attempt did not finish")


def test_concurrent_authorize_requests_share_one_avault_dialog(mac_avault):
    avault = mac_avault()
    with pytest.raises(api.AvaultAuthorizationRequiredError):
        api.avault_pubkey()
    avault.flag("hold")

    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(api.start_avault_authorization())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {result["attempt"]["state"] for result in results} == {"running"}
    assert len({result["attempt"]["id"] for result in results}) == 1

    avault.flag("release")
    attempt = _wait_for_attempt()

    assert attempt["state"] == "succeeded"
    assert [call for call in avault.calls() if call == "key authorize"] == ["key authorize"]
    assert api.get_avault_authorization_status()["required"] is False


@pytest.mark.parametrize(
    ("setup", "reason", "runs_avault"),
    [
        (
            lambda avault, monkeypatch: (
                avault.flag("deny"),
                monkeypatch.setattr(api, "macos_login_session_state", lambda: "stale_login_session"),
            ),
            "stale_login_session",
            True,
        ),
        (lambda avault, monkeypatch: avault.flag("deny"), "not_completed", True),
        (
            lambda avault, monkeypatch: (
                avault.flag("deny"),
                monkeypatch.setattr(api, "_process_cannot_show_macos_dialogs", lambda: True),
            ),
            "no_gui_session",
            True,
        ),
        (lambda avault, monkeypatch: monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: None), "avault_missing", False),
    ],
)
def test_authorize_failures_are_classified_without_parsing_avault_text(mac_avault, monkeypatch, setup, reason, runs_avault):
    avault = mac_avault()
    setup(avault, monkeypatch)

    result = api.avault_key_authorize()

    assert result["ok"] is False and result["reason"] == reason
    assert ("key authorize" in avault.calls()) is runs_avault
    if runs_avault:
        if reason == "stale_login_session":
            assert "detail" not in result
        else:
            assert result["detail"].startswith("avault:")


def test_authorize_needs_an_avault_with_the_contract(mac_avault):
    avault = mac_avault(version="0.1.7", unauthorized_status=70)

    assert api.avault_key_authorize() == {"ok": False, "reason": "avault_upgrade_required"}
    assert "key authorize" not in avault.calls()


def test_authorize_is_not_applicable_off_macos(mac_avault, monkeypatch):
    avault = mac_avault()
    monkeypatch.setattr(api.platform, "system", lambda: "Linux")

    assert api.avault_key_authorize() == {"ok": True, "reason": "not_applicable"}
    assert avault.calls() == []


def test_status_readers_and_failed_calls_never_authorize(mac_avault, monkeypatch):
    avault = mac_avault()
    real_dependencies_status = api.dependencies_status
    monkeypatch.setattr(api, "dependencies_status", lambda **_kwargs: real_dependencies_status(dependency_ids=["avault"]))
    monkeypatch.setattr(
        "core.git_runtime.git_runtime_status",
        lambda: {"resolution": "system", "version": "2.0", "managed": {}},
    )

    for _ in range(3):
        with pytest.raises(api.AvaultAuthorizationRequiredError):
            api.avault_pubkey()
    api._check_avault_keychain_authorization_after_upgrade()
    api.get_avault_authorization_status()
    items = cli._managed_dependencies_doctor_items()

    assert "key authorize" not in avault.calls()
    doctor = next(item for item in items if item.get("code") == "dependencies.avault.authorization_required")
    assert doctor["status"] == "warn" and "repair" not in doctor


def test_doctor_reports_stale_login_session_with_restart_action(monkeypatch):
    monkeypatch.setattr(api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.runtime, "resolve_service_owner_pid", lambda include_starting=False: 4242)
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        return "stale_login_session"

    monkeypatch.setattr(api, "macos_login_session_state", probe)

    item = cli._macos_login_session_doctor_items(language="en")[0]

    assert item["status"] == "fail"
    assert item["code"] == "stale_login_session"
    assert calls == [{"service_pid": 4242}]
    assert "vibe restart" in item["action"]


def test_cli_authorize_is_for_the_user_not_an_agent(mac_avault, monkeypatch, capsys):
    avault = mac_avault()
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_agent")

    assert cli.cmd_vault_key_authorize(None) == 1

    error = capsys.readouterr().err
    assert '"code": "user_action_required"' in error
    assert avault.calls() == []


def test_cli_authorize_reports_stale_login_session_without_raw_detail(mac_avault, monkeypatch, capsys):
    avault = mac_avault()
    avault.flag("deny")
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    monkeypatch.setattr(api, "macos_login_session_state", lambda: "stale_login_session")

    assert cli.cmd_vault_key_authorize(None) == 1
    error = capsys.readouterr().err
    assert '"code": "stale_login_session"' in error
    assert "errSecParam" not in error
    assert "vibe restart" in error


def test_cli_authorize_reports_success_and_failures(mac_avault, monkeypatch, capsys):
    import json

    avault = mac_avault()
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    avault.flag("deny")

    assert cli.cmd_vault_key_authorize(None) == 1
    err = capsys.readouterr().err
    assert "enter this Mac's login password and choose Always Allow" in err
    failure = json.loads(err[err.index("{"):])
    assert failure["code"] == "avault_authorization_not_completed"
    assert failure["details"]["avault_error"].startswith("avault:")

    avault.flag("deny", False)
    assert cli.cmd_vault_key_authorize(None) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "authorized"
