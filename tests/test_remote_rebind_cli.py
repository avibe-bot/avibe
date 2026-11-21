"""`vibe remote rebind` at a terminal, in an isolated data root.

The backend functions are the real ones; avibe.bot is the contract peer from
``test_personal_claims``. No real account, connector, clipboard or keychain.
"""
import builtins
import json
import sys
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from tests.test_pairing_recovery import pairing_host as pairing_host
from tests.test_personal_claims import claim_peer as claim_peer
from vibe import binding_admission as gate, binding_reset, cli, desktop_local_instance as local
from vibe import personal_claims, remote_access, remote_rebind


class _Terminal:
    """Both ends are a terminal; what is printed still goes to the captured streams."""

    def __init__(self, stdin=True, stdout=True):
        self.stdin = SimpleNamespace(isatty=lambda: stdin)
        self.stdout = SimpleNamespace(isatty=lambda: stdout)

    @property
    def stderr(self):
        return sys.stderr


@pytest.fixture
def terminal(claim_peer, monkeypatch):
    """A terminal on a computer that is not bound, with scripted answers."""
    from vibe import direct_certificates

    tty = _Terminal()
    monkeypatch.setattr(remote_rebind, "sys", tty)
    monkeypatch.setattr(binding_reset, "sys", tty)
    monkeypatch.setattr(cli, "_cli_invocation_authority", lambda: None)
    monkeypatch.setattr(remote_access, "stop", lambda _: {"ok": True})
    monkeypatch.setattr(remote_access, "_stop_direct_lifecycle_before_pairing_discard", lambda _: None)
    monkeypatch.setattr(direct_certificates, "discard_direct_certificates", lambda: None)
    script = SimpleNamespace(answers=[], prompts=[], peer=claim_peer)

    def answer(prompt=""):
        script.prompts.append(prompt)
        print(prompt, end="")
        reply = script.answers.pop(0)
        if isinstance(reply, type) and issubclass(reply, BaseException):
            raise reply()
        return reply

    monkeypatch.setattr(builtins, "input", answer)
    return script


@pytest.fixture
def bound_terminal(terminal):
    assert remote_access.pair("key_A", "https://backend.test")["ok"]
    local.ensure_installation_id()
    return terminal


def _instance():
    return V2Config.load().remote_access.vibe_cloud.instance_id


def _no_link(monkeypatch):
    monkeypatch.setattr(personal_claims, "run_link", lambda **_: pytest.fail("a bind request was started"))


@pytest.mark.parametrize("kind", ["stdin", "stdout", "agent"])
def test_refuses_outside_a_terminal_before_reading_or_changing_anything(bound_terminal, monkeypatch, capsys, kind):
    if kind == "agent":
        monkeypatch.setattr(cli, "_cli_invocation_authority", lambda: object())
    else:
        monkeypatch.setattr(remote_rebind, "sys", _Terminal(**{kind: False}))
    monkeypatch.setattr(remote_rebind, "read_state", lambda: pytest.fail("state was read"))
    _no_link(monkeypatch)

    assert remote_rebind.run() == 1

    captured = capsys.readouterr()
    assert "It does not run from an Agent, a Task, a script or a pipe." in captured.err
    assert captured.err.rstrip().endswith("Nothing changed.") and not captured.out
    assert _instance() == "inst_A" and not bound_terminal.prompts


@pytest.mark.parametrize("answer", ["", "y", "no", "YES please", EOFError, KeyboardInterrupt])
def test_reset_question_declines_on_anything_but_yes(bound_terminal, monkeypatch, capsys, answer):
    bound_terminal.answers = [answer]
    _no_link(monkeypatch)

    assert remote_rebind.run() == 0

    out = capsys.readouterr().out
    assert len(bound_terminal.prompts) == 1 and out.rstrip().endswith("This computer's binding is as it was.")
    assert _instance() == "inst_A" and local._installation_id() and not gate.reset_in_progress()


def _walk(script, answers):
    script.answers = list(answers)
    return remote_rebind.run()


def test_yes_resets_then_binds_only_after_the_account_and_domain_are_accepted(bound_terminal, capsys):
    """Scenario: AUTH-SETUP-212."""
    assert _walk(bound_terminal, ["yes", "y"]) == 0

    out = capsys.readouterr().out
    # What is lost is on screen before the one question that discards it.
    question = out.index("Type yes to bind as a new computer.")
    for loss in ("Remote access through fixture.avibe.bot stops at once.", "signed out", "loses it",
                 "The Avibe on avibe.bot is not deleted.", "No credential is changed",
                 "whoever holds the copy can still use it", "This cannot be undone."):
        assert out.index(loss) < question, loss
    # The browser way comes first and says what binding again does not do.
    assert out.index("1. Bind remote access again") < out.index("2. Bind as a new computer") < question
    assert "ssh -L 5123:127.0.0.1:5123" in out and "It does not change the tunnel credential." in out
    # The account and domain shown are the ones avibe.bot reported, before anything is accepted.
    link = out.index("https://avibe.bot/connect/ABCD234567")
    account = out.index("Account: actual@fixture.test")
    assert question < out.index("no longer bound to any Avibe") < link < account < out.index("[y/N]")
    assert out.index("Domain:  fixture.avibe.bot") > account
    assert "Bound. This computer's Avibe is at https://fixture.avibe.bot, owned by actual@fixture.test." in out
    assert bound_terminal.peer.decisions == ["owner-id"] and _instance() == "inst_A"
    # Names: thirty days in general words, nothing promised, and never the margin.
    assert "for 30 days" in out and "48 hours" not in out and "it is\nnot guaranteed" in out


@pytest.mark.parametrize("answer", ["", "n", "yes?", EOFError, KeyboardInterrupt])
def test_account_question_declines_and_leaves_the_computer_unbound(terminal, capsys, answer):
    assert _walk(terminal, [answer]) == 1

    out = capsys.readouterr().out
    assert "Account: actual@fixture.test" in out and "nothing was bound" in out
    assert out.rstrip().endswith("vibe remote rebind")
    assert terminal.peer.decisions == ["decline"] and not _instance()


def test_output_and_logs_carry_no_identity_or_proof(bound_terminal, capsys, caplog):
    cloud = V2Config.load().remote_access.vibe_cloud
    secrets = {local._installation_id(), cloud.instance_secret, cloud.tunnel_token, cloud.session_secret,
               bound_terminal.peer.key}
    caplog.set_level("DEBUG")

    assert _walk(bound_terminal, ["yes", "y"]) == 0

    claim = bound_terminal.peer.calls[-1][1]
    new = V2Config.load().remote_access.vibe_cloud
    secrets |= {local._installation_id(), claim["claim_id"], claim["code_verifier"], new.session_secret}
    captured = capsys.readouterr()
    shown = captured.out + captured.err + caplog.text
    assert len(secrets) >= 8 and all(secrets)
    assert [value for value in secrets if value in shown] == []


LINK_REFUSALS = [
    ("active_pairing_recent", "active in\nthe last 15 minutes", "delete the Avibe on avibe.bot and bind again"),
    ("paired_installation_loopback_required", "not through a link", "ssh -L 5123:127.0.0.1:5123"),
    ("control_plane_unreachable", "could not be reached, so nothing was bound", "vibe remote rebind"),
    ("rate_limited", "Wait 42\nseconds", "vibe remote rebind"),
    ("pairing_recovery_required", "may have completed on avibe.bot", "Do not start another one."),
    ("expired", "expired before it was approved", "vibe remote rebind"),
    ("fresh_claim_required", "lost after you accepted", "approve once more"),
    ("claim_in_progress", "already open", "Finish or cancel that one first."),
    ("name_taken", "taken before the binding finished", "choose another"),
    ("binding_unavailable", "gave no reason", "vibe remote rebind"),
    ("some_new_code", "did not complete this request", "(code: some_new_code)"),
    ("Not A Code!", "did not complete this request", "(code: unknown)"),
]


@pytest.mark.parametrize("code,truth,way_out", LINK_REFUSALS, ids=[row[0] for row in LINK_REFUSALS])
def test_each_link_refusal_says_what_is_true_and_the_way_out(terminal, monkeypatch, capsys, code, truth, way_out):
    monkeypatch.setattr(personal_claims, "run_link",
                        lambda **_: {"state": "error", "code": code, "retry_after_seconds": 42})

    assert remote_rebind.run() == 1

    out = capsys.readouterr().out
    assert truth in out and way_out in out
    # A computer with nothing left on it has nothing to reset, so nothing is asked.
    assert not terminal.prompts and "reset" not in out.lower()


def test_new_computer_after_the_loopback_refusal_is_a_separate_question_that_defaults_to_no(terminal, monkeypatch, capsys):
    local.ensure_installation_id()  # What an earlier binding leaves behind.
    before = local._installation_id()
    monkeypatch.setattr(personal_claims, "run_link",
                        lambda **_: {"state": "error", "code": "paired_installation_loopback_required"})

    assert _walk(terminal, [""]) == 1

    out = capsys.readouterr().out
    assert len(terminal.prompts) == 1 and "Type yes to bind as a new computer." in terminal.prompts[0]
    assert "This computer is not bound now." in out and "Nothing changed." in out
    assert local._installation_id() == before


def test_unexplained_refusal_infers_nothing_and_offers_no_reset(terminal, monkeypatch, capsys):
    # The identity exists from the first request, so it proves no earlier binding.
    local.ensure_installation_id()
    monkeypatch.setattr(personal_claims, "run_link", lambda **_: {"state": "error", "code": "binding_unavailable"})
    monkeypatch.setattr(binding_reset, "reset_interactively", lambda **_: pytest.fail("a reset was offered"))

    assert remote_rebind.run() == 1

    refusal = capsys.readouterr().out.split("avibe.bot refused", 1)[1]
    assert "gave no reason" in refusal and "Nothing was bound." in refusal and not terminal.prompts
    assert not any(word in refusal for word in ("before", "belongs", "new computer"))


def test_interrupt_while_waiting_says_where_the_computer_stands(terminal, monkeypatch, capsys):
    def interrupted(**_):
        raise KeyboardInterrupt

    monkeypatch.setattr(personal_claims, "run_link", interrupted)

    assert remote_rebind.run() == 1

    out = capsys.readouterr().out
    assert "Stopped. Nothing was bound; this computer is not bound." in out
    assert out.rstrip().endswith("vibe remote rebind")


def test_reset_stopped_midway_names_its_state_and_the_same_command_continues_it(bound_terminal, monkeypatch, capsys):
    with monkeypatch.context() as patch:
        patch.setattr(personal_claims, "run_link", lambda **_: pytest.fail("a bind request was started"))
        patch.setattr(binding_reset, "_advance", lambda _: (_ for _ in ()).throw(OSError("fixture crash")))
        assert _walk(bound_terminal, ["yes"]) == 1
    first = capsys.readouterr().out
    assert "The reset started and did not finish." in first and "stays off" in first
    assert first.rstrip().endswith("vibe remote rebind") and "fixture crash" not in first
    assert gate.reset_in_progress() and len(bound_terminal.prompts) == 1

    # The same command finishes that reset without asking again, then binds as usual.
    assert _walk(bound_terminal, ["y"]) == 0
    second = capsys.readouterr().out
    assert "Binding: a reset is unfinished" in second and "Finishing it now." in second
    assert "Type yes" not in second and second.index("no longer bound to any Avibe") < second.index("Open this link:")
    assert not gate.reset_in_progress() and bound_terminal.peer.decisions == ["owner-id"]


UNFINISHED = [
    ("pairing_recovery_required", {"state": "bound"}, None, "Finished. This computer is bound:", 0, False),
    ("pairing_recovery_required", {"state": "idle"}, "unpaired", "has been cleared", 0, True),
    ("pairing_recovery_required", {"state": "idle"}, "bound", "Finished. This computer is bound:", 0, False),
    ("pairing_recovery_required", {"state": "operation_in_progress"}, None, "working on it right now", 1, False),
    ("pairing_recovery_required", {"state": "pairing_recovery_required", "reason_code": "exact_replay_pending"},
     None, "It has not finished", 1, False),
    ("resetting", {"state": "unpaired"}, None, "no longer bound to any Avibe", 0, True),
    ("resetting", {"state": "resetting"}, None, "It could not finish yet.", 1, False),
    ("resetting", {"state": "operation_in_progress"}, None, "working on it right now", 1, False),
    ("resetting", {"state": "idle"}, "resetting", "working on it right now", 1, False),
    ("resetting", {"state": "mystery"}, None, "(code: mystery)", 1, False),
]


@pytest.mark.parametrize("start,resumed,reread,sentence,code,binds", UNFINISHED)
def test_unfinished_work_is_continued_not_restarted(terminal, monkeypatch, capsys, start, resumed, reread, sentence, code, binds):
    states = iter([start, reread])
    monkeypatch.setattr(remote_rebind, "read_state", lambda: (next(states), False))
    monkeypatch.setattr(remote_rebind, "_resume", lambda **_: resumed)
    monkeypatch.setattr(binding_reset, "reset_interactively", lambda **_: pytest.fail("a reset was offered"))
    linked = []
    monkeypatch.setattr(remote_rebind, "_link", lambda ctx, **kw: linked.append(kw) or 0)

    assert remote_rebind.run() == code

    assert sentence in capsys.readouterr().out
    assert bool(linked) is binds and not terminal.prompts


def _unfinished_attempt(monkeypatch, issuer):
    """A binding accepted on avibe.bot whose saving on this computer was cut short."""
    from uuid import uuid4

    sends = []

    def request(_url, payload, **_kw):
        sends.append(dict(payload))
        return issuer(len(sends))

    monkeypatch.setattr(remote_access, "_json_request", request)
    cfg, ident = V2Config.load(), local.ensure_installation_id()
    result = remote_access.pair_personal_claim(
        key="vrp2_" + "A" * 43, backend_url="https://backend.test", device="test",
        revision=local.claim_revision(cfg, ident, None), installation=ident, subject="owner",
        claim_id=str(uuid4()), instance_id="inst_A", hostname="fixture.avibe.bot", binding_disposition="existing")
    assert not result["ok"]
    return sends


def test_an_attempt_avibe_bot_refused_for_good_is_not_called_unrecorded_and_needs_a_new_step(bound_terminal, monkeypatch, capsys):
    def refuse(_n):
        raise remote_access.BackendRequestError(409, {"error": "registration_indeterminate"})

    sends = _unfinished_attempt(monkeypatch, refuse)
    _no_link(monkeypatch)

    assert _walk(bound_terminal, [""]) == 0

    out = capsys.readouterr().out
    assert "Binding: an earlier attempt is unfinished" in out and "has refused that attempt" in out
    assert "That does not mean avibe.bot recorded nothing of it." in out and "a new step from you" in out
    # The new step is offered, never taken: the same two ways, the same question, default no.
    assert out.index("a new step from you") < out.index("1. Bind remote access again") < out.index("Type yes")
    assert len(sends) == 1 and _instance() == "inst_A"


def test_completing_an_earlier_binding_is_its_own_question_never_the_reset_question(bound_terminal, monkeypatch, capsys):
    from tests.test_pairing_recovery import _response

    def issuer(n):
        if n == 1:
            raise TimeoutError
        return {**_response(), "binding_disposition": "existing"}

    sends = _unfinished_attempt(monkeypatch, issuer)
    gate.reset_intent_path().write_text("{")
    before = (gate.reset_intent_path().read_bytes(), remote_access._pending_pairing_path().read_bytes())
    _no_link(monkeypatch)
    monkeypatch.setattr(binding_reset, "reset_interactively", lambda **_: pytest.fail("the reset question was used"))

    for answer in ("", "yes please", EOFError, KeyboardInterrupt):
        assert _walk(bound_terminal, [answer]) == 0
        out = capsys.readouterr().out
        assert "Two unfinished things are on this computer" in out and "It resets nothing." in out
        assert "its record on this computer is damaged." in out
        assert "Type yes to bind as a new computer" not in out and "You lose:" not in out
        assert out.rstrip().endswith("remote access on this computer stays off.")
        assert before == (gate.reset_intent_path().read_bytes(), remote_access._pending_pairing_path().read_bytes())
    assert len(sends) == 1 and all("Complete the earlier binding" in prompt for prompt in bound_terminal.prompts)

    assert _walk(bound_terminal, ["y"]) == 0
    out = capsys.readouterr().out
    assert "The unfinished reset was withdrawn." in out and "Finished. This computer is bound: https://fixture.avibe.bot" in out
    assert len(sends) == 2 and sends[0] == sends[1] and not gate.reset_in_progress() and _instance() == "inst_A"


@pytest.mark.parametrize("damage,category", [
    ("record", "its record on this computer is damaged."),
    ("config", "this computer's binding changed after it was started."),
])
def test_a_reset_that_cannot_continue_is_replaced_only_by_asking_for_a_new_reset(bound_terminal, monkeypatch, capsys, damage, category):
    from config.v2_config import config_file_lock

    cfg = V2Config.load()
    with gate.admitted("reset"), config_file_lock():
        binding_reset._seal(cfg)
    if damage == "record":
        gate.reset_intent_path().write_text("{")
    else:
        cfg.remote_access.vibe_cloud.instance_secret = "changed-key"
        cfg.save()
    before = gate.reset_intent_path().read_bytes()

    with monkeypatch.context() as patch:
        _no_link(patch)
        assert _walk(bound_terminal, [""]) == 0
    out = capsys.readouterr().out
    assert "Binding: a reset cannot finish" in out and category in out
    # It reads as a reset of this computer, with everything a reset says, not as a repair.
    assert "What follows is a new reset of this computer" in out and "your earlier answer does not count" in out
    assert out.index("You lose:") < out.index("This cannot be undone.") < out.index("Type yes to bind as a new computer.")
    assert "repair" not in out.lower()
    assert gate.reset_intent_path().read_bytes() == before and _instance() == "inst_A"

    assert _walk(bound_terminal, ["yes", "y"]) == 0
    out = capsys.readouterr().out
    assert out.index("no longer bound to any Avibe") < out.index("Open this link:")
    assert not gate.reset_in_progress() and bound_terminal.peer.decisions == ["owner-id"]


def test_a_reset_that_cannot_be_replaced_safely_says_so_and_asks_nothing(bound_terminal, monkeypatch, capsys):
    from config.v2_config import config_file_lock

    with gate.admitted("reset"), config_file_lock():
        binding_reset._seal(V2Config.load())
    gate.reset_intent_path().write_text("{")
    remote_access._pending_pairing_path().write_text("not a pairing record")
    _no_link(monkeypatch)

    assert _walk(bound_terminal, []) == 1

    out = capsys.readouterr().out
    assert "nor replaced by a new one safely from here" in out and "docs.avibe.bot" in out
    assert gate.reset_in_progress() and _instance() == "inst_A" and not bound_terminal.prompts


def test_status_start_and_pair_say_what_the_binding_needs(bound_terminal, monkeypatch, capsys):
    json_off = SimpleNamespace(json=False)
    monkeypatch.setattr(remote_rebind, "read_state", lambda: ("binding_rejected", True))
    assert cli.cmd_remote_status(json_off) == 0
    assert "  Binding: refused by avibe.bot. See: vibe remote rebind" in capsys.readouterr().out
    assert cli.cmd_remote_status(SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["binding_state"] == "binding_rejected"

    for error, sentence in (("binding_rejected", "avibe.bot is refusing this computer's binding."),
                            ("reset_in_progress", "a reset of this computer's binding has not finished.")):
        monkeypatch.setattr(remote_access, "start", lambda error=error: {"ok": False, "error": error})
        assert cli.cmd_remote_start(json_off) == 1
        err = capsys.readouterr().err
        assert sentence in err and "vibe remote rebind" in err and error not in err

    # Pairing over a computer whose remote access was off keeps it off: not a failed start.
    monkeypatch.setattr(remote_access, "pair", lambda *_: {"ok": True, "paired": True, "enabled": False,
                                                           "pairing": {"ok": True}})
    args = SimpleNamespace(json=False, pairing_key="key_B", backend_url="https://backend.test", device_name="avibe")
    assert cli.cmd_remote_pair(args) == 0
    captured = capsys.readouterr()
    assert "Remote access was off on this computer and stays off." in captured.out and not captured.err
    assert cli._CLI_COMMAND_FLOORS[("remote", "rebind")] == "owner"
