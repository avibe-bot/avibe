"""Interactive recovery against a private root and a synthetic pairing issuer."""
import json
from uuid import uuid4

import pytest
from config.v2_config import V2Config, config_file_lock
from tests.test_binding_reset import cli_host as cli_host
from tests.test_pairing_recovery import pairing_host as pairing_host, _response
from vibe import binding_admission as gate, binding_recovery as recovery, binding_reset as reset
from vibe import desktop_local_instance as local, remote_access as remote, personal_claims as claims
from vibe.personal_claim_client import ClaimError


_product_stop = remote.stop


def _attempt(cfg, ident, *, pending=None):
    return remote.pair_personal_claim(key='vrp2_' + 'A' * 43, backend_url='https://backend.test', device='test',
        revision=local.claim_revision(cfg, ident, pending), installation=ident, subject='owner',
        claim_id=str(uuid4()), instance_id='inst_A', hostname='fixture.avibe.bot', binding_disposition='existing')


@pytest.mark.parametrize('status,code,terminal', [(409, 'registration_indeterminate', True),
    (400, 'invalid_pairing_key', True), (503, 'registration_indeterminate', False),
    (409, 'pairing_redeem_pending', False), (409, 'unknown', False)])
def test_exact_refusal_requires_new_explicit_handoff(cli_host, monkeypatch, status, code, terminal):
    cfg = V2Config.load()
    ident = local.ensure_installation_id()
    sends = []
    def issuer(_url, payload, **kw):
        sends.append(dict(payload))
        raise remote.BackendRequestError(status, {'error': code})
    monkeypatch.setattr(remote, '_json_request', issuer)
    assert not _attempt(cfg, ident)['ok']
    old, _ = remote._read_pending_pairing_record()
    assert recovery.resume_interactively() == {'state': 'pairing_recovery_required',
        'reason_code': 'issuer_replay_refused' if terminal else 'exact_replay_pending'}
    assert len(sends) == (1 if terminal else 2)
    assert old == remote._read_pending_pairing_record()[0]
    if terminal:
        assert claims._BoundSource.load(None).revision == local.claim_revision(cfg, ident, old)
        assert reset.reset_interactively(confirm=lambda: False) == {'state': 'declined'}
        assert remote._drive_pending_pairing() == (False, 0) and len(sends) == 1
        from vibe import binding_health
        with gate.admitted('address'):
            pass
        # Read through the same routes used by the page, including after service restart.
        from vibe import ui_server
        monkeypatch.setattr(claims, 'rebind_claims', claims.RebindClaims())
        browser = ui_server.app.test_client()
        base = '/api/remote-access/rebind'
        view = browser.get(base).get_json()
        assert view['binding_state'] == 'bound' and view['reason_code'] == 'issuer_replay_refused' and view['can_start']
        assert view['next_action'] == 'rebind'
        claim = browser.get(base + '/claims/' + old['personal_claim']['claim_id']).get_json()
        assert claim == {'state': 'error', 'code': 'issuer_replay_refused'}
        binding_health._set(cfg, rejected=True)
        assert claims.RebindClaims().overview()['binding_state'] == 'binding_rejected'
        binding_health._set(cfg, rejected=False)
        # Use the product's actual Off operation before explicit replacement.
        monkeypatch.setattr(remote, '_stop_connector', lambda *_: {'ok': True})
        monkeypatch.setattr(remote, '_record_device_access_switch', lambda *_: None)
        remote._connector_requested_path().touch()
        assert _product_stop(cfg)['ok'] and cfg.remote_access.vibe_cloud.enabled
        monkeypatch.setattr(remote, '_json_request', lambda *_a, **_kw: {**_response(), 'binding_disposition': 'existing'})
        monkeypatch.setattr(remote, 'start', lambda *_: pytest.fail('wrote back the user switch'))
        published = _attempt(cfg, ident, pending=old)
        assert published['ok'] and 'start' not in published and not remote._connector_requested_path().exists()
    else:
        assert sends[0] == sends[1]
        with pytest.raises(ClaimError, match='pairing_recovery_required'):
            claims._BoundSource.load(None)


def test_unconsumed_expired_key_leaves_no_journal(cli_host, monkeypatch):
    cfg = V2Config.load()
    ident = local.ensure_installation_id()
    def expired(*args, **kwargs):
        raise remote.BackendRequestError(400, {'error': 'pairing_key_expired'})
    monkeypatch.setattr(remote, '_json_request', expired)
    assert _attempt(cfg, ident)['error'] == 'pairing_key_expired'
    assert remote._read_pending_pairing_record() == (None, None)
    assert claims.RebindClaims().overview()['binding_state'] == 'bound'
    assert recovery.resume_interactively() == {'state': 'idle'}
    assert local._installation_id() == ident and V2Config.load().remote_access.vibe_cloud == cfg.remote_access.vibe_cloud


@pytest.mark.parametrize('damage,reason', [('record','reset_record_damaged'), ('config','binding_changed'), ('file','files_changed')])
def test_reauthorize_only_current_generation(cli_host, monkeypatch, damage, reason):
    cfg = V2Config.load()
    with gate.admitted('reset'), config_file_lock():
        reset._seal(cfg)
    if damage == 'record':
        gate.reset_intent_path().write_text('{')
    elif damage == 'config':
        cfg.remote_access.vibe_cloud.instance_secret = 'changed-key'
        cfg.save()
    else:
        local.ensure_installation_id()
    before = gate.reset_intent_path().read_bytes()
    view = recovery.resume_interactively()
    assert view == {'state': 'reset_repair_required', 'reason_code': reason}
    assert reset.reset_interactively(repair=True, confirm=lambda: False) == {'state': 'declined'}
    assert gate.reset_intent_path().read_bytes() == before
    def changed():
        gate.reset_intent_path().unlink()
        gate.reset_intent_path().write_bytes(before)
        return True
    with pytest.raises(ClaimError, match='local_binding_changed'):
        reset.reset_interactively(repair=True, confirm=changed)
    original = reset._save
    def save(record):
        assert gate.reset_in_progress()  # never release admission to an unpaired root mid-repair
        original(record)
    monkeypatch.setattr(reset, '_save', save)
    assert reset.reset_interactively(repair=True, confirm=lambda: True) == {'state':'unpaired'}
    assert not gate.reset_in_progress() and not V2Config.load().remote_access.vibe_cloud.instance_id
    assert recovery.resume_interactively() == {'state':'idle'}


def test_damaged_reset_protects_exact_pair_over_reset(cli_host, monkeypatch):
    cfg = V2Config.load()
    ident = local.ensure_installation_id()
    sends = []
    def issuer(_url, payload, **kw):
        sends.append(dict(payload))
        if len(sends) == 1:
            raise TimeoutError
        return {**_response(), 'binding_disposition': 'existing'}
    monkeypatch.setattr(remote, '_json_request', issuer)
    assert not _attempt(cfg, ident)['ok']
    gate.reset_intent_path().write_text('{')
    keep = recovery.resume_interactively()
    assert keep == {'state': 'reset_repair_required', 'reason_code': 'reset_record_damaged',
                    'next_action': 'confirm_resume_pairing', 'hostname': 'fixture.avibe.bot'}
    assert recovery.resume_interactively(confirm_pairing=lambda: pytest.fail('different host than displayed'),
              expected_hostname='other.invalid')['state'] == 'reset_repair_required'
    with pytest.raises(ClaimError, match='pairing_recovery_required'):
        reset.reset_interactively(repair=True, confirm=lambda: pytest.fail('wrong consent'))
    assert recovery.resume_interactively(confirm_pairing=lambda: False)['state'] == 'reset_repair_required'
    assert gate.reset_in_progress() and len(sends) == 1
    assert recovery.resume_interactively(confirm_pairing=lambda: True, expected_hostname=keep['hostname']) == {'state':'bound'}
    assert len(sends) == 2 and sends[0] == sends[1] and not gate.reset_in_progress()
    assert local._installation_id() == ident and remote._read_pending_pairing_record() == (None, None)


@pytest.mark.parametrize('url', [None, 'http://example.test', 'https://user@example.test',
                                 'https://example.test/path', 'https://example.test\n'])
def test_keep_prompt_omits_unusable_targets(url):
    record = {'phase': 'redeemed', 'pairing': {'public_url': url}}
    assert recovery._approved_hostname(record) is None
    # An unsealed field in a prepared Desktop request is not evidence of approval.
    assert recovery._approved_hostname({'phase': 'prepared', 'pairing': {'public_url': 'https://other.test'}}) is None


@pytest.mark.parametrize('old', ['truncated', 'other-instance', 'other-schema'])
def test_irrelevant_address_journal_does_not_reserve_current_pairing(cli_host, old):
    from vibe import instance_address as addr
    state = addr._empty_state('not-this-instance' if old == 'other-instance' else 'inst_A')
    state['journal'] = {'phase': 'intent'}
    if old == 'other-schema':
        state['schema_version'] += 1
    addr._state_path().write_text('{' if old == 'truncated' else json.dumps(state))
    assert claims.rebind_claims.overview()['can_start']
    assert remote.pair('key_B', 'https://backend.test')['ok']
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_B'


def test_reset_removes_local_remote_rows_even_after_config_was_cleared(cli_host):
    from sqlalchemy import select
    from storage.db import get_cached_sqlite_engine
    from storage.models import (instance_address_clearings as names, instance_address_clearing_receipts as receipts,
                                remote_access_authorizations as grants)
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud = reset._blank(cfg.remote_access.vibe_cloud)
    cfg.save()
    gate.reset_intent_path().write_text('{')
    engine = get_cached_sqlite_engine()
    with engine.begin() as conn:
        for instance in ('former-A', 'former-B'):
            conn.execute(names.insert().values(id=instance, instance_id=instance, hostname='example.invalid',
                         device_id='device', kind='browser', push_only=0, retired_at=1))
            conn.execute(receipts.insert().values(receipt_hash=instance, clearing_id=instance))
            conn.execute(grants.insert().values(id=instance, instance_id=instance, subject='owner', claims_json='{}', created_at=1))
    assert reset.reset_interactively(repair=True, confirm=lambda: True) == {'state':'unpaired'}
    with engine.connect() as conn:
        for table in (names, receipts, grants):
            assert not conn.execute(select(table)).first()


@pytest.mark.parametrize('damage', ['unreadable', 'old-format', 'source-changed', 'identity-damaged'])
def test_damaged_pair_and_unusable_reset_require_fresh_consent(cli_host, monkeypatch, damage):
    cfg = V2Config.load()
    ident = local.ensure_installation_id()
    sends = []
    def offline(*args, **kwargs):
        sends.append(True)
        raise TimeoutError
    monkeypatch.setattr(remote, '_json_request', offline)
    assert not _attempt(cfg, ident)['ok']
    if damage == 'unreadable':
        remote._pending_pairing_path().write_text('{')
    elif damage == 'old-format':
        remote._write_pending_pairing_record(remote._new_pairing_claim(cfg, 'https://backend.test', 'test'))
    elif damage == 'source-changed':
        cfg.remote_access.vibe_cloud.instance_secret = 'changed-key'
        cfg.save()
    else:
        local._identity_path().write_text('{')
    gate.reset_intent_path().write_text('{')
    assert 'next_action' not in recovery.resume_interactively()
    assert reset.reset_interactively(repair=True, confirm=lambda: False) == {'state': 'declined'}
    assert gate.reset_in_progress() and remote._pending_pairing_path().is_file()
    assert reset.reset_interactively(repair=True, confirm=lambda: True) == {'state': 'unpaired'}
    assert len(sends) == 1 and not local._identity_path().exists() and not gate.reset_in_progress()


@pytest.mark.parametrize('phase', ['redeemed', 'applied'])
@pytest.mark.parametrize('identity', ['missing', 'malformed', 'replaced'])
def test_sealed_enrollment_needs_its_installation_to_protect_replay(cli_host, monkeypatch, phase, identity):
    from tests.test_pairing_recovery import _fail_config_publication_once

    cfg = V2Config.load()
    original = local.ensure_installation_id()
    with monkeypatch.context() as fault:
        fault.setattr(remote, '_json_request', lambda *_a, **_kw: {**_response(), 'binding_disposition': 'existing'})
        if phase == 'redeemed':
            _fail_config_publication_once(fault)
        else:
            fault.setattr(remote, '_retire_pending_pairing', lambda **_: False)
        assert not _attempt(cfg, original)['ok']
    record, error = remote._read_pending_pairing_record()
    assert not error and record['phase'] == phase and record['personal_claim']['installation_id'] == original
    with pytest.raises(ClaimError, match='pairing_recovery_required'):
        reset.reset_interactively(confirm=lambda: pytest.fail('intact enrollment still owns recovery'))

    path = local._identity_path()
    path.unlink()
    if identity == 'malformed':
        path.write_text('{')
    elif identity == 'replaced':
        assert local.ensure_installation_id() != original
    monkeypatch.setattr(recovery, '_run_pair', lambda *_: pytest.fail('cannot publish for this installation'))
    assert recovery.resume_interactively() == {'state': 'pairing_recovery_required', 'reason_code': 'pairing_record_unusable'}
    repair = identity != 'missing'
    if repair:
        gate.reset_intent_path().write_text('{')
        assert 'next_action' not in recovery.resume_interactively()
    assert reset.reset_interactively(repair=repair, confirm=lambda: False) == {'state': 'declined'}
    assert remote._read_pending_pairing_record() == (record, None) and gate.reset_in_progress() == repair
    assert reset.reset_interactively(repair=repair, confirm=lambda: True) == {'state': 'unpaired'}
    assert remote._read_pending_pairing_record() == (None, None) and not path.exists()


@pytest.mark.parametrize('action', ['pair', 'reset'])
@pytest.mark.parametrize('replacement_journal', [False, True])
def test_tunnel_cutover_recovers_only_its_prepublication_journal(cli_host, monkeypatch, action, replacement_journal):
    from vibe import connection_settings as cs
    cfg = V2Config.load()
    cloud = cfg.remote_access.vibe_cloud
    cloud.transport_protocol = 'http2'
    cloud.edge_ip_version = '6'
    cloud.public_relay_trial = True
    cfg.save()
    cs.enroll_from_host(cfg)
    op = cs.journal.begin(cfg, 'protocol', 'quic')
    cloud.transport_protocol = 'quic'
    cfg.save()
    cs.journal.verifying(cfg, op['id'], 'test-generation')
    version = cs.journal.file_version()
    class PowerLoss(BaseException):
        pass
    with monkeypatch.context() as cut:
        cut.setattr(cs.journal, 'retire_version', lambda _: (_ for _ in ()).throw(PowerLoss()))
        with pytest.raises(PowerLoss):
            if action == 'pair':
                remote.pair('key_B', 'https://backend.test')
            else:
                reset.reset_interactively(confirm=lambda: True)
    new = V2Config.load()
    assert new.remote_access.vibe_cloud.instance_id == ('inst_B' if action == 'pair' else '')
    assert cs.value(new, 'protocol') == 'http2' and cs.value(new, 'ipFamily') == '6'
    assert not cs.relay_enrolled(new) and cs.journal.file_version() == version
    if replacement_journal:
        cs.journal.begin(new, 'protocol', 'quic')
        replacement = cs.journal.path.read_bytes()
    assert recovery.resume_interactively()['state'] == ('bound' if action == 'pair' else 'unpaired')
    if replacement_journal:
        assert cs.journal.path.read_bytes() == replacement
    else:
        assert not cs.journal.path.exists()


@pytest.mark.parametrize('action', ['reset', 'pair'])
def test_tunnel_cutover_before_config_write_preserves_old_authority(cli_host, monkeypatch, action):
    from tests.test_pairing_recovery import _fail_config_publication_once
    from vibe import connection_settings as cs
    cfg = V2Config.load()
    op = cs.journal.begin(cfg, 'ipFamily', '6')
    cfg.remote_access.vibe_cloud.edge_ip_version = '6'
    cfg.save()
    cs.journal.verifying(cfg, op['id'], 'old-generation')
    original = cs.journal.path.read_bytes()
    with monkeypatch.context() as cut:
        _fail_config_publication_once(cut)
        if action == 'pair':
            assert not remote.pair('key_B', 'https://backend.test')['ok']
        else:
            assert reset.reset_interactively(confirm=lambda: True)['state'] == 'resetting'
    live = V2Config.load()
    assert live.remote_access.vibe_cloud.instance_id == cfg.remote_access.vibe_cloud.instance_id
    assert cs.value(live, 'ipFamily') == '6' and cs.journal.path.read_bytes() == original
    # #4114 or an independent setting may complete before cutover is retried.
    live.remote_access.vibe_cloud.edge_ip_version = 'auto'
    live.save()
    assert recovery.resume_interactively()['state'] == ('bound' if action == 'pair' else 'unpaired')
    assert cs.value(V2Config.load(), 'ipFamily') == 'auto' and not cs.journal.path.exists()


def _hold_admission(pipe):
    with gate.admitted('reset'):
        pipe.send('held')
        pipe.recv()


def test_cli_never_claims_worker_completion_or_waits_on_other_process(cli_host):
    import multiprocessing
    context = multiprocessing.get_context('spawn')
    with gate.admitted('reset'), config_file_lock():
        reset._seal(V2Config.load())
    parent, child = context.Pipe()
    worker = context.Process(target=_hold_admission, args=(child,))
    worker.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'held'
        assert recovery.resume_interactively() == {'state':'operation_in_progress'}
        assert reset._read()['phase'] == 'retire'
        parent.send('release')
        worker.join(5)
        assert not worker.is_alive()
        assert recovery.resume_interactively() == {'state':'unpaired'}
        assert recovery.resume_interactively() == {'state':'idle'}
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)
        parent.close()


def test_stop_busy_keeps_retryable_reset(cli_host, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(remote, 'stop', lambda *_: {'ok':False, 'error':'remote_access_busy'})
        assert reset.reset_interactively(confirm=lambda: True) == {'state':'resetting'}
        assert recovery.resume_interactively() == {'state':'resetting'}
        assert reset._read()['phase'] == 'retire'
    assert recovery.resume_interactively() == {'state':'unpaired'}
