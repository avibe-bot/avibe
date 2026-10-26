"""avault's one-time macOS Keychain authorization, against fake avault binaries.

Contract (avault 0.1.8+, avault/docs/DESIGN.md "Keychain authorization"): a call
that needs the user's approval exits 77 (one-shot) or sends
``code: keychain_authorization_required`` (resident agent); only
``avault key authorize`` may show the dialog. Older avault exits 70 for every
failure and has no ``key authorize``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from storage.vault_crypto import Sealed
from vibe import api, avault_authorization, cli


def _fake_avault(tmp_path: Path, *, version: str = "0.1.8", unauthorized_status: int = 77) -> tuple[Path, Path]:
    """A POSIX-sh avault whose behavior follows files in its state directory.

    ``authorized`` lets it read the master key; ``deny`` makes ``key authorize``
    fail as a refused dialog does; ``hold`` keeps ``key authorize`` waiting for
    ``release``. Every invocation's arguments are appended to ``calls``.
    """
    state = tmp_path / "avault-state"
    state.mkdir()
    script = tmp_path / "bin" / "avault"
    script.parent.mkdir()
    script.write_text(
        f"""#!/bin/sh
STATE="{state}"
[ "$1" = "--store" ] && shift 2
echo "$*" >> "$STATE/calls"
unauthorized() {{
  echo "avault: failed to read master key from Keychain: run \\`avault key authorize\\` in a logged-in session" >&2
  exit {unauthorized_status}
}}
case "$1" in
  version) echo "avault {version}"; exit 0 ;;
  pubkey)
    [ -f "$STATE/authorized" ] || unauthorized
    echo '{{"public_key":"pk","fingerprint":"fp"}}'; exit 0 ;;
  key)
    if [ "$2" = "authorize" ]; then
      if [ -f "$STATE/hold" ]; then while [ ! -f "$STATE/release" ]; do sleep 0.02; done; fi
      if [ -f "$STATE/deny" ]; then echo "avault: Keychain read failed: User canceled the operation." >&2; exit 70; fi
      touch "$STATE/authorized"; echo '{{"ok":true}}'; exit 0
    fi ;;
  deliver)
    shift 2
    [ "$1" = "--" ] && shift
    cat > /dev/null
    [ -f "$STATE/authorized" ] || unauthorized
    "$@"; exit $? ;;
esac
exit 70
"""
    )
    script.chmod(0o755)
    return script, state


def _calls(state: Path) -> list[str]:
    path = state / "calls"
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def mac_avault(monkeypatch, tmp_path):
    """Point Avibe at a fake avault on a Mac; return a factory taking the fake's options."""
    monkeypatch.setattr(api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(api, "_process_cannot_show_macos_dialogs", lambda: False)
    monkeypatch.setattr(avault_authorization, "_ATTEMPT", None)

    def install(**options):
        script, state = _fake_avault(tmp_path, **options)
        monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: str(script))
        return state

    return install


def _secret() -> list[dict]:
    return [{"name": "API_TOKEN", "env": "API_TOKEN", "envelope": Sealed(ciphertext="ct", nonce="n", wrap_meta="wm")}]


def test_exit_77_reports_authorization_required_until_the_key_reads_again(mac_avault):
    state = mac_avault()

    with pytest.raises(api.AvaultAuthorizationRequiredError) as exc:
        api.avault_pubkey()

    # Nothing left avault, so one-shot grants are released rather than consumed.
    assert isinstance(exc.value, api.AvaultPreHandoffError)
    assert api.get_avault_authorization_status()["required"] is True
    avault = api.dependencies_status(dependency_ids=["avault"])["deps"][0]
    assert avault["status"] == "ready" and avault["authorization_required"] is True

    (state / "authorized").touch()
    assert api.avault_pubkey() == {"public_key": "pk", "fingerprint": "fp"}
    assert api.get_avault_authorization_status()["required"] is False


def test_avault_without_the_contract_keeps_todays_failures(mac_avault):
    state = mac_avault(version="0.1.7", unauthorized_status=70)

    with pytest.raises(api.AvaultError) as exc:
        api.avault_pubkey()
    assert not isinstance(exc.value, api.AvaultAuthorizationRequiredError)
    assert api.get_avault_authorization_status()["required"] is False

    # Its child's 77 is just the child's exit code; a pre-0.1.8 avault is never
    # probed again, since its background Keychain reads may still prompt.
    (state / "authorized").touch()
    before = len(_calls(state))
    assert api.avault_deliver_run(_secret(), ["sh", "-c", "exit 77"])["exit_code"] == 77
    assert [call for call in _calls(state)[before:] if call.startswith("pubkey")] == []


def test_deliver_run_77_is_authorization_only_when_the_master_key_check_agrees(mac_avault):
    state = mac_avault()

    with pytest.raises(api.AvaultAuthorizationRequiredError) as exc:
        api.avault_deliver_run(_secret(), ["sh", "-c", "exit 0"])
    # The probe cannot prove no child ran, so the delivery stays fail-closed.
    assert not isinstance(exc.value, api.AvaultPreHandoffError)
    assert api.get_avault_authorization_status()["required"] is True

    (state / "authorized").touch()
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
    state = mac_avault()
    with pytest.raises(api.AvaultAuthorizationRequiredError):
        api.avault_pubkey()
    (state / "hold").touch()

    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(api.start_avault_authorization())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {result["attempt"]["state"] for result in results} == {"running"}
    assert len({result["attempt"]["id"] for result in results}) == 1

    (state / "release").touch()
    attempt = _wait_for_attempt()

    assert attempt["state"] == "succeeded"
    assert [call for call in _calls(state) if call == "key authorize"] == ["key authorize"]
    assert api.get_avault_authorization_status()["required"] is False


@pytest.mark.parametrize(
    ("setup", "reason", "runs_avault"),
    [
        (lambda state, monkeypatch: (state / "deny").touch(), "not_completed", True),
        (
            lambda state, monkeypatch: (
                (state / "deny").touch(),
                monkeypatch.setattr(api, "_process_cannot_show_macos_dialogs", lambda: True),
            ),
            "no_gui_session",
            True,
        ),
        (lambda state, monkeypatch: monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: None), "avault_missing", False),
    ],
)
def test_authorize_failures_are_classified_without_parsing_avault_text(mac_avault, monkeypatch, setup, reason, runs_avault):
    state = mac_avault()
    setup(state, monkeypatch)

    result = api.avault_key_authorize()

    assert result["ok"] is False and result["reason"] == reason
    assert ("key authorize" in _calls(state)) is runs_avault
    if runs_avault:
        assert result["detail"].startswith("avault:")


def test_authorize_needs_an_avault_with_the_contract(mac_avault):
    state = mac_avault(version="0.1.7", unauthorized_status=70)

    assert api.avault_key_authorize() == {"ok": False, "reason": "avault_upgrade_required"}
    assert "key authorize" not in _calls(state)


def test_authorize_is_not_applicable_off_macos(mac_avault, monkeypatch):
    state = mac_avault()
    monkeypatch.setattr(api.platform, "system", lambda: "Linux")

    assert api.avault_key_authorize() == {"ok": True, "reason": "not_applicable"}
    assert _calls(state) == []


def test_status_readers_and_failed_calls_never_authorize(mac_avault, monkeypatch):
    state = mac_avault()
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

    assert "key authorize" not in _calls(state)
    doctor = next(item for item in items if item.get("code") == "dependencies.avault.authorization_required")
    assert doctor["status"] == "warn" and "repair" not in doctor


def test_cli_authorize_is_for_the_user_not_an_agent(mac_avault, monkeypatch, capsys):
    state = mac_avault()
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses_agent")

    assert cli.cmd_vault_key_authorize(None) == 1

    error = capsys.readouterr().err
    assert '"code": "user_action_required"' in error
    assert _calls(state) == []


def test_cli_authorize_reports_success_and_failures(mac_avault, monkeypatch, capsys):
    import json

    state = mac_avault()
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    (state / "deny").touch()

    assert cli.cmd_vault_key_authorize(None) == 1
    err = capsys.readouterr().err
    assert "enter this Mac's login password and choose Always Allow" in err
    failure = json.loads(err[err.index("{"):])
    assert failure["code"] == "avault_authorization_not_completed"
    assert failure["details"]["avault_error"].startswith("avault:")

    (state / "deny").unlink()
    assert cli.cmd_vault_key_authorize(None) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "authorized"
