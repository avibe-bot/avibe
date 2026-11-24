"""`vibe remote pair` against the real pairing backend: what the terminal says and what it sends.

Only the relay is replaced: ``Wire`` counts every ``/api/v1/pairing/redeem`` POST for a
saved, installation-bound request, and ``pairing_host`` counts those of a key typed now.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from tests.test_pairing_recovery import _response, pairing_host as pairing_host  # noqa: F401
from tests.test_desktop_local_instance import desktop_host as desktop_host  # noqa: F401
from tests.test_pairing_replay import setup_replay as setup_replay, view  # noqa: F401
from tests.test_personal_claims import claim_peer as claim_peer  # noqa: F401
from vibe import cli, pairing_replay as replay, remote_access as ra
from vibe.support_contact import SUPPORT_EMAIL

NEEDS_CHECK = ("This computer cannot tell whether Avibe Cloud accepted its pairing request, "
               "so it will not send it again, and a new key cannot replace it.\n"
               f"To have it checked, email Avibe support at {SUPPORT_EMAIL}. Do not delete Avibe's files.\n")
# Never offered where the result is not this computer's own first send of a key typed now.
KEY_OFFERS = ("get a new", "enter it here", "new_pairing_key", "fresh", "substitute", "vibe remote pair")


def run_pair(key=None, *, json_output=False):
    return cli.cmd_remote_pair(SimpleNamespace(
        pairing_key=key, backend_url="https://backend.test", device_name="fixture", json=json_output))


@pytest.fixture
def no_prompt(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **kw: pytest.fail("a saved request must not prompt"))


def lines(capsys):
    captured = capsys.readouterr()
    return captured.err


def assert_needs_check(err):
    assert err == NEEDS_CHECK
    assert not any(offer in err.lower() for offer in KEY_OFFERS)


def test_support_address_is_the_shared_constant():
    assert SUPPORT_EMAIL == "support@avibe.bot"
    assert "Contact Avibe support" not in NEEDS_CHECK


def record_typed_keys(monkeypatch):
    """After the saved request was sent: any further redeem is recorded and never reaches the network."""
    typed = []

    def record(url, payload, **kwargs):
        typed.append(payload.get("pairing_key"))
        raise AssertionError("no redeem may be sent here")

    monkeypatch.setattr(ra, "_json_request", record)
    return typed


# --- a saved, installation-bound request -------------------------------------------------


def test_scheduled_retry_is_left_to_the_service(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    post(payload)
    assert view()["replay_state"] == "scheduled"
    capsys.readouterr()
    assert run_pair() == 1
    err = lines(capsys)
    assert err.startswith("Avibe Cloud has not confirmed this pairing yet. Avibe will send the same request again at ")
    assert err.endswith(" while it is running; you do not need a new key.\nTo check on it: vibe remote status\n")
    assert len(wire.sent) == 1


def test_manual_continue_sends_the_same_request_exactly_once(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    post(payload)
    clock[0] = replay._state(ra._read_pending_pairing_record()[0])["deadline"] + 1
    assert view()["replay_state"] == "manual"
    capsys.readouterr()
    assert run_pair() == 1
    assert lines(capsys) == ("Avibe stopped retrying this pairing on its own. You can send the same request once more; "
                             "your key is not needed again:\n  vibe remote pair\n")
    assert len(wire.sent) == 2 and wire.sent[0] == wire.sent[1]
    wire.status, wire.body = 200, json.dumps(_response("continued")).encode()
    clock[0] += 61
    assert run_pair() in (0, 1)  # paired; the tunnel start is outside this test
    assert len(wire.sent) == 3 and wire.sent[2] == wire.sent[0]
    assert view() is None


@pytest.mark.parametrize("entered", [None, "another-key"])
def test_needs_check_sends_nothing_and_offers_no_key(setup_replay, monkeypatch, capsys, no_prompt, entered):
    wire, clock, payload, post = setup_replay
    post(payload)
    # What a crash leaves after the send was reserved and before its result was written.
    replay._save({**replay._state(ra._read_pending_pairing_record()[0]), "mode": "unsettled"})
    typed = record_typed_keys(monkeypatch)
    for advance in (0, 61, 901):
        clock[0] += advance
        capsys.readouterr()
        assert run_pair(entered) == 1
        assert_needs_check(lines(capsys))
    assert len(wire.sent) == 1 and typed == []


def test_needs_check_json_is_the_backend_payload(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    post(payload)
    replay._save({**replay._state(ra._read_pending_pairing_record()[0]), "mode": "unsettled"})
    capsys.readouterr()
    assert run_pair(json_output=True) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "pairing_needs_reconciliation" and out["replay_state"] == "needs_reconciliation"
    assert len(wire.sent) == 1


def test_cloud_pending_is_a_floor(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 409, b'{"error":"pairing_redeem_pending","retry_after_seconds":7}'
    post(payload)
    clock[0] += 3
    capsys.readouterr()
    assert run_pair() == 1
    assert lines(capsys) == "Avibe Cloud is still processing this pairing. Wait 4 seconds, then run the same command again.\n"
    assert len(wire.sent) == 1


def test_rate_limit_with_retries_left_is_the_scheduled_text(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    wire.status, wire.advice = 429, "90"
    post(payload)
    clock[0] += 60
    capsys.readouterr()
    assert run_pair() == 1
    assert lines(capsys).startswith("Avibe Cloud has not confirmed this pairing yet.")
    assert len(wire.sent) == 1


def test_rate_limit_after_the_window_names_the_wait(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    wire.status, wire.advice = 429, "1800"
    post(payload)
    clock[0] = replay._state(ra._read_pending_pairing_record()[0])["deadline"] + 1
    capsys.readouterr()
    assert run_pair() == 1
    err = lines(capsys)
    assert err.startswith("Avibe Cloud asked this computer to wait before trying again. Wait ")
    assert err.endswith(" seconds, then run the same command again.\n")
    assert len(wire.sent) == 1


def test_received_but_not_archived_without_a_receipt_needs_check(setup_replay, monkeypatch, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 200, json.dumps(_response("received")).encode()
    write = ra._write_pending_pairing_record

    def fail_archive(record):
        if record["phase"] == "redeemed":
            raise OSError("fixture: full")
        return write(record)

    with monkeypatch.context() as m:
        m.setattr(ra, "_write_pending_pairing_record", fail_archive)
        post(payload)
    typed = record_typed_keys(monkeypatch)
    for entered in (None, "another-key"):
        capsys.readouterr()
        assert run_pair(entered) == 1
        assert_needs_check(lines(capsys))
    assert len(wire.sent) == 1 and typed == []


def test_received_with_a_receipt_is_archived_here_without_sending(setup_replay, monkeypatch, capsys, no_prompt):
    import config.v2_config as config_module
    from config import paths

    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 200, json.dumps(_response("received")).encode()
    original = config_module.write_atomic
    remaining = [1]

    def fail_config_once(path, content, **kwargs):
        if str(path) == str(paths.get_config_path()) and remaining[0]:
            remaining[0] -= 1
            raise OSError("fixture config publication failure")
        return original(path, content, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(config_module, "write_atomic", fail_config_once)
        post(payload)
    assert ra._read_pending_pairing_record()[0]["phase"] == "redeemed"
    capsys.readouterr()
    assert run_pair("another-key") == 1
    assert lines(capsys) == ("Avibe Cloud accepted this pairing, but this computer has not finished saving it. "
                             "Finish here; nothing is sent to Avibe Cloud:\n  vibe remote pair\n")
    assert run_pair() in (0, 1)
    assert len(wire.sent) == 1
    assert ra.pending_pairing_status(None) is None


def test_ended_desktop_app_request_is_left_to_the_app(setup_replay, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    wire.status, wire.body = 400, b'{"error":"pairing_key_expired"}'
    post(payload)
    assert view()["replay_state"] == "terminated" and view()["operation_owner"] == "desktop_app"
    capsys.readouterr()
    assert run_pair() == 1
    err = lines(capsys)
    assert err == "This pairing is managed by the Avibe desktop app, which renews it.\n"
    assert not any(offer in err.lower() for offer in KEY_OFFERS)
    assert len(wire.sent) == 1


@pytest.mark.parametrize(("owner", "expected"), [
    ("personal_claim", "This sign-in has ended. Start again from the page or command that began it.\n"),
    ("cli", NEEDS_CHECK),
    (None, NEEDS_CHECK),
])
def test_ended_request_copy_follows_its_owner(monkeypatch, capsys, no_prompt, owner, expected):
    # Display only: which sentence an ended saved request gets. The sends are counted above.
    monkeypatch.setattr(ra, "pending_pairing_record_exists", lambda: True)
    monkeypatch.setattr(ra, "pair", lambda *a, **kw: {"ok": False, "error": "pairing_key_expired", "status": 400})
    monkeypatch.setattr(ra, "pending_pairing_status", lambda config: {
        "phase": "prepared", "can_resume": False, "replay_state": "terminated", "operation_owner": owner})
    assert run_pair() == 1
    assert lines(capsys) == expected


@pytest.mark.parametrize("error", ["invalid_pairing_key", "pairing_key_used"])
def test_a_replay_refused_as_invalid_or_used_needs_check(monkeypatch, capsys, no_prompt, error):
    # Only the backend's pairing_input_rejected names this command's own first send.
    monkeypatch.setattr(ra, "pending_pairing_record_exists", lambda: True)
    monkeypatch.setattr(ra, "pair", lambda *a, **kw: {"ok": False, "error": error, "status": 400})
    monkeypatch.setattr(ra, "pending_pairing_status", lambda config: None)
    assert run_pair() == 1
    assert_needs_check(lines(capsys))


def test_local_busy_is_not_pending_and_queues_nothing(setup_replay, monkeypatch, capsys, no_prompt):
    wire, clock, payload, post = setup_replay
    post(payload)
    clock[0] += 2
    entered, release = threading.Event(), threading.Event()
    respond = wire.getresponse

    def slow():
        entered.set()
        assert release.wait(20)
        return respond()

    monkeypatch.setattr(wire, "getresponse", slow)
    before = ra._pending_pairing_path().read_bytes()
    worker = threading.Thread(target=replay.tick)
    worker.start()
    try:
        assert entered.wait(10)
        capsys.readouterr()
        for entered_key in (None, "another-key"):
            assert run_pair(entered_key) == 1
            assert lines(capsys) == ("Another pairing step is running on this computer, so this command was not accepted. "
                                     "Nothing from it was sent or saved. Run the same command again in a moment.\n")
        assert ra._pending_pairing_path().read_bytes() == before
    finally:
        release.set()
        worker.join(15)
    assert len(wire.sent) == 2  # the service's own retry, and nothing from the terminal


# --- a key typed now ---------------------------------------------------------------------


def _answer(monkeypatch, status, error):
    def redeem(url, payload, **kwargs):
        raise ra.BackendRequestError(status, {"error": error})

    monkeypatch.setattr(ra, "_json_request", redeem)


def test_first_send_expired_then_hidden_prompt_for_the_new_key(pairing_host, monkeypatch, capsys):
    _, calls = pairing_host
    redeem = ra._json_request
    sent = []

    def expired_then_ok(url, payload, **kwargs):
        sent.append(payload["pairing_key"])
        if payload["pairing_key"] == "old-key":
            raise ra.BackendRequestError(400, {"error": "pairing_key_expired"})
        return redeem(url, payload, **kwargs)

    monkeypatch.setattr(ra, "_json_request", expired_then_ok)
    assert run_pair("old-key") == 1
    assert lines(capsys) == ("This pairing key expired before it was used. Get a new one at https://avibe.bot, "
                             "then enter it here:\n  vibe remote pair\n")
    assert not ra.pending_pairing_record_exists()
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: prompts.append(prompt) or "key_B")
    assert run_pair() == 0
    assert prompts == ["Paste pairing key (input hidden): "]
    assert sent == ["old-key", "key_B"]


@pytest.mark.parametrize("error", ["invalid_pairing_key", "pairing_key_used"])
def test_first_send_refused_key_is_checked_or_replaced(pairing_host, monkeypatch, capsys, error):
    _answer(monkeypatch, 400, error)
    assert run_pair("typo-key") == 1
    assert lines(capsys) == ("This pairing key was not accepted. Check it, or get a new one at https://avibe.bot, "
                             "then enter it here:\n  vibe remote pair\n")
    assert not ra.pending_pairing_record_exists()


def test_typed_key_with_unknown_outcome_is_never_replaced(pairing_host, monkeypatch, capsys, no_prompt):
    sent = []

    def unknown(url, payload, **kwargs):
        sent.append(payload["pairing_key"])
        raise ra.BackendRequestError(503, {"error": "backend_http_error"})

    monkeypatch.setattr(ra, "_json_request", unknown)
    assert run_pair("uncertain-key") == 1
    assert_needs_check(lines(capsys))
    for entered in (None, "uncertain-key", "key_B"):
        assert run_pair(entered) == 1
        assert_needs_check(lines(capsys))
    assert sent == ["uncertain-key"]


def test_a_pasted_key_leaves_an_unfinished_sign_in_alone(monkeypatch, capsys, request):
    from tests.test_personal_claim_journal import _claim
    from vibe import personal_claims as claims

    request.getfixturevalue("claim_peer")
    sends = []

    def rejected(url, payload, **kwargs):
        sends.append(payload.get("pairing_key"))
        raise ra.BackendRequestError(422, {"error": "pairing_request_failed"})

    monkeypatch.setattr(ra, "_json_request", rejected)
    assert _claim()["code"] == "pairing_recovery_required"
    evidence = [ra._pending_pairing_path(), replay._path(), claims._marker()]
    before = [path.read_bytes() for path in evidence]
    capsys.readouterr()
    assert run_pair("fixture-unrelated-key") == 1
    assert lines(capsys) == ("This key was not used. This computer is still finishing a sign-in that was started "
                             "in the Avibe app or a browser; continue it there.\n")
    assert [path.read_bytes() for path in evidence] == before
    assert len(sends) == 1 and "fixture-unrelated-key" not in sends
