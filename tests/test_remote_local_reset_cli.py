"""The local-only exit is a separate terminal command, never a cloud rebind."""
from vibe import binding_reset, cli, remote_rebind


def test_remote_reset_parser_and_dispatch(monkeypatch):
    import sys
    import pytest

    args = cli.build_parser().parse_args(['remote', 'reset'])
    assert args.remote_command == 'reset'
    calls = []
    monkeypatch.setattr(binding_reset, 'reset_local_only_interactively',
                        lambda *, confirm: calls.append(callable(confirm)) or {'state': 'declined'})
    monkeypatch.setattr(remote_rebind, '_interactive', lambda: True)
    monkeypatch.setattr(sys, 'argv', ['vibe', 'remote', 'reset'])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0 and calls == [True]


import pytest  # noqa: E402

from config.v2_config import V2Config  # noqa: E402
from tests.test_binding_reset import cli_host as cli_host  # noqa: E402,F401
from tests.test_pairing_recovery import pairing_host as pairing_host  # noqa: E402,F401
from vibe import binding_admission as gate, binding_unbind, desktop_local_instance as local  # noqa: E402


@pytest.fixture
def terminal(cli_host, monkeypatch):
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.backend_url = 'https://avibe.bot'
    cfg.save()
    local.ensure_installation_id()
    monkeypatch.setattr(remote_rebind, '_interactive', lambda: True)
    answers, sends = [], []
    monkeypatch.setattr('builtins.input', lambda prompt='': answers.pop(0))
    return answers, sends


def _replies(monkeypatch, sends, *outcomes):
    monkeypatch.setattr(binding_unbind, 'send_original',
                        lambda cfg, op, **kw: sends.append(op) or outcomes[len(sends) - 1])


@pytest.mark.parametrize('evidence,line', [
    ({'accepted': True, 'dispatched': True}, 'has already received'),
    ({'accepted': False, 'dispatched': True}, 'may already have been sent'),
    ({'accepted': False, 'dispatched': False}, None),
])
def test_local_reset_says_only_what_is_known_about_a_sent_request(terminal, capsys, evidence, line):
    answers, _ = terminal
    with gate.admitted('reset'):
        binding_reset.seal_unbind(binding_reset.unbind_snapshot())
    binding_reset._save({**binding_reset._read(), **evidence})
    answers.append('')
    assert remote_rebind.run_reset() == 0
    out = capsys.readouterr().out
    for known in ('has already received', 'may already have been sent'):
        assert (known in out) is (known == line)
    assert 'Nothing is deleted on avibe.bot by this command' in out
    assert gate.reset_in_progress() and V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'


def test_terminal_as_new_deletes_first_then_clears_once(terminal, monkeypatch, capsys):
    answers, sends = terminal
    _replies(monkeypatch, sends, 'unbound')
    answers.append('yes')
    assert remote_rebind._offer_unbind(remote_rebind._context()) == 'deleted'
    out = capsys.readouterr().out
    assert 'This deletes fixture.avibe.bot on avibe.bot first.' in out and 'may belong to another account' in out
    assert "This computer's binding was removed." in out and len(sends) == 1
    assert not gate.reset_in_progress() and local._installation_id() is None


@pytest.mark.parametrize('second', ['yes', ''])
def test_not_applied_needs_its_own_local_consent(terminal, monkeypatch, capsys, second):
    answers, sends = terminal
    _replies(monkeypatch, sends, 'invalid_credential')
    answers.extend(['yes', second])
    outcome = remote_rebind._offer_unbind(remote_rebind._context())
    out = capsys.readouterr().out
    assert 'it no longer accepts this computer\'s key' in out and len(sends) == 1
    assert not gate.reset_in_progress()
    if second:
        assert outcome == 'reset' and local._installation_id() is None
    else:
        assert outcome == 'refused' and local._installation_id() is not None
        assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'


def test_refused_key_keeps_the_local_only_question(terminal, monkeypatch, capsys):
    from vibe import binding_health
    answers, sends = terminal
    _replies(monkeypatch, sends)
    monkeypatch.setattr(binding_health, 'rejected_at', lambda cfg: 1.0)
    answers.append('')
    assert remote_rebind._offer_unbind(remote_rebind._context()) == 'declined'
    assert 'The Avibe on avibe.bot is not deleted.' in capsys.readouterr().out and not sends
