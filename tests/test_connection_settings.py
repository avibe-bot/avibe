"""Disposable config/journal and connector doubles. No phone, public tunnel or wall-clock sleeps."""
from types import SimpleNamespace

import pytest

from config.v2_config import V2Config
from vibe import connection_settings as cs, connection_runtime as cr, remote_access as ra


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    cfg = V2Config.default()
    cloud = cfg.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.direct = True
    cloud.instance_id, cloud.instance_secret, cloud.session_secret = 'inst-test', 'pairing-test', 'cookie-test'
    cloud.public_url = 'https://test.invalid'
    cfg.save()
    clock = [10.0]
    monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: clock[0]))
    monkeypatch.setattr(cr, '_probe', None)
    active = {'pid': 111, 'started_at': 1, 'metrics_url': 'fixture-active'}
    monkeypatch.setattr(ra, '_state_connector', lambda key: active if key == 'active' else None)
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: pid == active['pid'])
    monkeypatch.setattr(ra, '_read_pid', lambda: active['pid'])
    monkeypatch.setattr(ra, '_cloudflared_pid_state', lambda pid: 'cloudflared' if pid else 'dead')
    monkeypatch.setattr(ra, '_connector_lane_reserved_locked', lambda: False)
    monkeypatch.setattr(cr.direct_lifecycle, 'notify_config_changed', lambda cfg: None)
    req = ra._connector_requested_path()
    req.parent.mkdir(parents=True, exist_ok=True)
    req.touch()
    return SimpleNamespace(cfg=cfg, actor=cs.Actor(cs.binding(cfg)), clock=clock, active=active, request=req)


def outcome():
    return cs.journal.inspect(V2Config.load())['outcome']


@pytest.mark.parametrize('local,current,target,allowed', [(False, True, False, False), (False, False, True, True), (True, True, False, True)])
def test_endpoint_gate_and_no_automatic_switch_reversal(rig, monkeypatch, local, current, target, allowed):
    rig.cfg.remote_access.vibe_cloud.direct = current
    rig.cfg.save()
    actor = cs.Actor(cs.binding(rig.cfg), local)
    if not allowed:
        with pytest.raises(cs.ConnectionError, match='tunnel_unhealthy'):
            cr.prepare(actor, 'direct', target)
        assert not cs.journal.path.exists()
        assert cs.value(V2Config.load(), 'direct') is current
        return
    op = cr.prepare(actor, 'direct', target)
    assert cs.value(V2Config.load(), 'direct') is current  # before the response task
    def elapse(seconds):
        assert cs.value(V2Config.load(), 'direct') is current
        rig.clock[0] += seconds

    monkeypatch.setattr(cr, 'time', SimpleNamespace(sleep=elapse))
    started = rig.clock[0]
    cr.apply(actor, op)
    assert rig.clock[0] - started == (0 if local else 1)
    assert outcome()['result'] == 'applied'
    rig.clock[0] += 3600
    cr.recover_startup()
    assert cs.value(V2Config.load(), 'direct') is target


@pytest.mark.parametrize('key', ['direct', 'relay'])
def test_remote_switch_rechecks_health_after_allowing_response_to_drain(rig, monkeypatch, key):
    # A sample can age out during the grace period. Do not retire the endpoint
    # based on the pre-response sample; the journal lets the page read the refusal.
    if key == 'relay':
        cs.enroll_from_host(rig.cfg)
        rig.cfg.remote_access.vibe_cloud.public_relay_trial = True
        rig.cfg.save()
    cr.observe_public_path(rig.cfg, rig.active, True, SimpleNamespace(successes=(True,)), rig.clock[0])
    rig.clock[0] += 59.5
    op = cr.prepare(rig.actor, key, False)

    def elapse(seconds):
        assert cs.value(V2Config.load(), key) is True
        rig.clock[0] += seconds

    monkeypatch.setattr(cr, 'time', SimpleNamespace(sleep=elapse))
    cr.apply(rig.actor, op)
    assert cs.value(V2Config.load(), key) is True
    assert (outcome()['id'], outcome()['reason']) == (op['id'], 'tunnel_unhealthy')


def test_one_fresh_success_only_for_this_connector(rig):
    sample = SimpleNamespace(successes=(False, True, False))
    cr.observe_public_path(rig.cfg, rig.active, True, sample, rig.clock[0])
    assert cr.tunnel_healthy(rig.cfg)
    rig.active['started_at'] += 1
    assert not cr.tunnel_healthy(rig.cfg)
    cr.observe_public_path(rig.cfg, rig.active, True, sample, rig.clock[0])
    rig.clock[0] += 61
    assert not cr.tunnel_healthy(rig.cfg)


@pytest.mark.parametrize('target', [True, False])
def test_relay_enrollment_precedes_other_checks_and_noop(rig, target):
    with pytest.raises(cs.ConnectionError, match='relay_not_enrolled') as e:
        cr.prepare(rig.actor, 'relay', target)
    assert e.value.status == 403 and not cs.journal.path.exists()


def test_old_true_is_adopted_once_per_install_even_after_repairing(rig):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.public_relay_trial = True
    rig.cfg.save()
    cs.adopt_host_enrollment()
    assert cs.relay_enrolled(rig.cfg)
    cloud.instance_secret = 'next-pairing'
    rig.cfg.save()
    cs.adopt_host_enrollment()
    assert not cs.relay_enrolled(rig.cfg)


@pytest.mark.parametrize('ok,drained,baseline,expected', [(True, True, False, 'verified'), (False, True, False, 'verification_unavailable'), (False, True, True, 'verification_failed'), (True, False, True, 'verification_failed')])
def test_instance_verifies_after_drain_not_with_the_browser(rig, monkeypatch, ok, drained, baseline, expected):
    monkeypatch.setattr(cr, 'tunnel_healthy', lambda cfg: baseline)
    monkeypatch.setattr(ra, 'status', lambda config=None: {'running': True})
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    monkeypatch.setattr(ra, '_start_candidate_connector', lambda *args: (222, 'fixture-new'))
    monkeypatch.setattr(ra, '_wait_candidate_ready', lambda *args: True)
    events = []
    def promote(*args, **kwargs):
        events.append('promote')
        rig.active.update(pid=222, started_at=2, metrics_url='fixture-new')
        return {'pid': 111}
    monkeypatch.setattr(ra, '_promote_candidate_connector', promote)
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda c: events.append('drain') or drained)
    def probe(url):
        assert url == 'https://test.invalid/health'
        assert cs.journal.owns_connector_lane()
        events.append('probe')
        return ok
    monkeypatch.setattr(cr, '_public_check', probe)
    op = cr.prepare(rig.actor, 'protocol', 'http2')
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    cr.apply(rig.actor, op)
    assert events == ['promote', 'drain'] + (['probe'] if drained else [])
    if expected != 'verified':
        monkeypatch.setattr(cr, '_repair_runtime', lambda *args: True)
        cr.recover()
    assert outcome()['reason'] == expected
    assert cs.value(V2Config.load(), 'protocol') == ('http2' if expected == 'verified' else 'auto')


def test_no_connector_saves_without_start_or_probe(rig, monkeypatch):
    monkeypatch.setattr(ra, '_read_pid', lambda: None)
    monkeypatch.setattr(cr, '_public_check', lambda *a: pytest.fail('no connector to verify'))
    assert cr.prepare(rig.actor, 'ipFamily', 'auto') is None
    assert outcome()['reason'] == 'saved_without_connector'
    assert outcome()['from'] == '4' and outcome()['to'] == 'auto'


def test_interrupted_change_reopens_journal_and_debt_finishes(rig, monkeypatch):
    cs.journal.begin(rig.cfg, 'protocol', 'quic')
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    old_id = cs.journal.inspect(rig.cfg)['operation']['id']
    monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: rig.clock[0]))
    cr.recover_startup()
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    monkeypatch.setattr(cr, '_repair_runtime', lambda *a: False)
    rig.clock[0] += cs.RESTORE_SECONDS + 1
    cr.recover()
    assert outcome()['reason'] == 'saved_restored_runtime_pending'
    assert cs.journal.inspect(rig.cfg)['operation'] is None
    monkeypatch.setattr(cr, '_repair_runtime', lambda *a: True)
    cr.recover()
    assert (outcome()['id'], outcome()['result']) == (old_id, 'restored')


@pytest.mark.parametrize('address,previous,target', [('192.0.2.1', '4', '6'), ('2001:db8::1', '6', '4')])
def test_stopped_connector_refuses_family_incompatible_with_saved_outbound(rig, monkeypatch, address, previous, target):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.edge_ip_version, cloud.edge_bind_address = previous, address
    rig.cfg.save()
    monkeypatch.setattr(ra, '_read_pid', lambda: None)
    monkeypatch.setattr(ra, 'network_interfaces', lambda: {'interfaces': [{'address': address}]})
    with pytest.raises(cs.ConnectionError, match='invalid_connection_change') as error:
        cr.prepare(rig.actor, 'ipFamily', target)
    assert error.value.status == 400
    assert not cs.journal.path.exists()
    assert cs.value(V2Config.load(), 'ipFamily') == previous


@pytest.mark.parametrize('entry', ['page', 'legacy'])
@pytest.mark.parametrize('past_bound', [False, True])
def test_noop_preserves_pending_runtime_repair(rig, monkeypatch, past_bound, entry):
    op = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    cs.journal.restore(cs.journal.load(rig.cfg), 'verification_failed')
    if past_bound:
        rig.clock[0] += cs.RESTORE_SECONDS + 1
    monkeypatch.setattr(cr, '_repair_runtime', lambda *args: False)
    assert cr.recover() is False
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    if entry == 'page':
        assert cr.prepare(rig.actor, 'protocol', 'auto') is None
    else:
        monkeypatch.setattr(ra, 'status', lambda config=None: {})
        assert ra.apply_settings({'transport_protocol': 'auto'})['settings_applied'] is False
    monkeypatch.setattr(cr, '_repair_runtime', lambda *args: True)
    assert cr.recover()
    assert (outcome()['id'], outcome()['result']) == (op['id'], 'restored')


def test_explicit_new_intent_supersedes_recovery(rig):
    op = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    cs.journal.restore(cs.journal.load(rig.cfg), 'verification_failed')
    new = cr.prepare(rig.actor, 'protocol', 'http2')
    assert new['id'] != op['id']
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cs.journal.applying(rig.cfg, op['id'])


@pytest.mark.parametrize('saved,automatic,expected', [(True, None, '4'), (False, True, 'auto'), (False, False, '4'), (None, None, '4')])
def test_pin_recovery_requires_failed_saved_then_registered_automatic(rig, monkeypatch, saved, automatic, expected):
    window = cs.PinFailureWindow(lambda: rig.clock[0])
    def tick(n, count=0):
        rig.clock[0] += n
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=count), max_gap=20)
    tick(0)
    for _ in range(11):
        tick(15)
    tick(14.9)
    assert not cs.journal.path.exists()
    tick(0.2)
    assert cs.journal.load(rig.cfg)['pinAttempt']
    attempts = []
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    def spawn(cfg, binary, protocol):
        assert cs.value(V2Config.load(), 'ipFamily') == '4'
        attempts.append(cs.value(cfg, 'ipFamily'))
        return len(attempts), 'fixture'
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(cr, '_wait_registered', lambda pid, url: saved if pid == 1 else automatic)
    monkeypatch.setattr(ra, '_discard_candidate_connector', lambda pid: None)
    monkeypatch.setattr(ra, '_promote_candidate_connector', lambda pid, **kw: {})
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda old: True)
    cr._consume_pin_attempt()
    assert attempts == (['4', 'auto'] if saved is False else ['4'])
    assert cs.value(V2Config.load(), 'ipFamily') == expected
    assert bool(cs.journal.inspect(rig.cfg)['returnedToAutomatic']) == (expected == 'auto')


def test_storage_restore_retries_after_bound_and_restart(rig, monkeypatch):
    cs.journal.begin(rig.cfg, 'protocol', 'quic')
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    cs.journal.restore(cs.journal.load(rig.cfg), 'verification_failed')
    rig.clock[0] += cs.RESTORE_SECONDS + 1
    original = V2Config.save
    def refuse_save(*args):
        raise OSError('fixture: read-only storage')
    monkeypatch.setattr(V2Config, 'save', refuse_save)
    assert cr.recover() is False
    assert outcome()['reason'] == 'storage_restore_failed'
    assert cs.journal.load(rig.cfg)['operation'] is None
    assert cs.value(V2Config.load(), 'protocol') == 'quic'
    monkeypatch.setattr(V2Config, 'save', original)
    monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: rig.clock[0]))
    cr.recover_startup()
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    monkeypatch.setattr(cr, '_repair_runtime', lambda *args: True)
    assert cr.recover()
    assert outcome()['result'] == 'restored'


@pytest.mark.parametrize('same_generation', [True, False])
def test_retire_only_proven_failed_connector_without_withdrawing_request(rig, monkeypatch, same_generation):
    stopped = []
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-bin')
    monkeypatch.setattr(ra, '_running_signature', lambda pid: {'current': 'bad'})
    monkeypatch.setattr(ra.runtime, 'stop_pid', lambda pid, timeout: stopped.append(pid) or True)
    operation = {'generation': ra.connector_generation() if same_generation else 'superseded'}
    assert not cr._repair_runtime(rig.cfg, operation, at_bound=True)
    assert stopped == ([rig.active['pid']] if same_generation else [])
    assert rig.request.is_file()


def test_observation_gap_registration_or_identity_resets_window(rig):
    w = cs.PinFailureWindow(lambda: rig.clock[0])
    for i in range(12):
        assert not w.observe(('a',), disconnected=True, max_gap=16)
        rig.clock[0] += 15
    rig.clock[0] += 170
    assert not w.observe(('a',), disconnected=True, max_gap=16)
    assert not w.observe(('a',), disconnected=False, max_gap=16)
    assert not w.observe(('b',), disconnected=True, max_gap=16)
