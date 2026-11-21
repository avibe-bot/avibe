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
    monkeypatch.setattr(cr, '_probe_window', None)
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


def test_replaced_connector_is_busy_until_first_window_then_reports_health(rig):
    cr.observe_public_path(rig.cfg, rig.active, True, SimpleNamespace(successes=(True,)), rig.clock[0])
    rig.active['started_at'] += 1
    with pytest.raises(cs.ConnectionError, match='connection_change_busy'):
        cr.prepare(rig.actor, 'direct', False)
    cr.observe_public_path(rig.cfg, rig.active, True, None, rig.clock[0])
    with pytest.raises(cs.ConnectionError, match='connection_change_busy'):
        cr.prepare(rig.actor, 'direct', False)
    cr.observe_public_path(rig.cfg, rig.active, True, SimpleNamespace(successes=(False,)), rig.clock[0])
    with pytest.raises(cs.ConnectionError, match='tunnel_unhealthy'):
        cr.prepare(rig.actor, 'direct', False)


def test_paused_ticks_do_not_restart_first_sample_deadline(rig):
    cr.observe_public_path(rig.cfg, rig.active, True, SimpleNamespace(successes=(True,)), rig.clock[0])
    for dt, refusal in [(1, 'connection_change_busy'), (20, 'connection_change_busy'), (11, 'tunnel_unhealthy')]:
        rig.clock[0] += dt
        cr.observe_public_path(rig.cfg, rig.active, False, None, rig.clock[0])
        with pytest.raises(cs.ConnectionError, match=refusal):
            cr.prepare(rig.actor, 'direct', False)
    assert cs.value(V2Config.load(), 'direct') is True


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


@pytest.mark.parametrize('debt', [False, True])
@pytest.mark.parametrize('target', ['quic', 'http2'])
def test_change_cannot_build_on_rejected_pin_before_writeback(rig, monkeypatch, debt, target):
    op = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    state = cs.journal.load(rig.cfg)
    cs.journal.restore(state, 'verification_failed')
    if debt:
        state['debt'] = state['operation']
        cs.journal.finish(state, 'failed', 'storage_restore_failed')
    before = cs.journal.path.read_bytes()
    with pytest.raises(cs.ConnectionError, match='connection_change_busy'):
        cr.prepare(rig.actor, 'protocol', target)
    monkeypatch.setattr(ra, 'status', lambda *args: {})
    assert ra.apply_settings({'transport_protocol': target})['error'] == 'remote_access_settings_unavailable'
    assert cs.journal.path.read_bytes() == before
    monkeypatch.setattr(cr, '_repair_runtime', lambda *args: True)
    assert cr.recover() and outcome()['id'] == op['id']
    assert cs.value(V2Config.load(), 'protocol') == 'auto'


def test_probe_reserves_lane_for_legacy_and_precedes_switch_baseline(rig, monkeypatch):
    op = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    cs.journal.verifying(rig.cfg, op['id'], 'test-generation')
    monkeypatch.setattr(ra, 'status', lambda *args: {'running': True})
    assert ra.apply_settings({'edge_ip_version': '6'})['error'] == 'remote_access_settings_unavailable'
    with pytest.raises(cs.ConnectionError, match='connection_change_busy'):
        cr.prepare(rig.actor, 'direct', False)
    assert cs.value(V2Config.load(), 'ipFamily') == '4'
    assert cs.journal.load(rig.cfg)['operation']['id'] == op['id']


def test_lifecycle_contention_is_immediate_busy(rig, monkeypatch):
    def held(*, timeout):
        assert timeout == 0
        raise ra._ConnectorLifecycleBusy()
    monkeypatch.setattr(ra, '_connector_lifecycle', held)
    with pytest.raises(cs.ConnectionError, match='connection_change_busy') as exc:
        cr.prepare(rig.actor, 'direct', False)
    assert exc.value.status == 409 and not cs.journal.path.exists()


@pytest.mark.parametrize(('arguments', 'key', 'new'), [
    (['off'], 'direct', False), (['relay', 'public-trial', 'on'], 'relay', True),
])
def test_host_cli_reports_applied_even_with_unreadable_journal(rig, monkeypatch, arguments, key, new):
    from vibe import cli
    from vibe.authorization import current_invocation_authority

    assert current_invocation_authority() is None
    cs.journal.path.parent.mkdir(exist_ok=True, parents=True)
    cs.journal.path.write_text('broken optional journal')
    monkeypatch.setattr(ra, 'status', lambda _config: {'ok': True})
    monkeypatch.setattr(ra, 'live_direct_status', lambda *_args, **_kwargs: {})
    assert cs.value(V2Config.load(), key) is not new
    assert cli.cmd_remote_direct(cli.build_parser().parse_args(['remote', 'direct', *arguments])) == 0
    assert cs.value(V2Config.load(), key) is new
    assert cs.journal.path.read_text() == 'broken optional journal'


def test_unreadable_settings_disable_only_new_feature(rig, monkeypatch):
    from vibe import ui_server
    cs.journal.path.parent.mkdir(exist_ok=True, parents=True)
    cs.journal.path.write_text('not json')
    with pytest.raises(cs.ConnectionError, match='connection_state_unavailable'):
        cr.prepare(rig.actor, 'protocol', 'http2')
    assert not cs.journal.owns_connector_lane()
    starts = []
    def cannot_enroll():
        raise OSError('fixture')
    monkeypatch.setattr(cs, 'adopt_host_enrollment', cannot_enroll)
    monkeypatch.setattr(cr, 'start', lambda: starts.append('worker'))
    monkeypatch.setattr(ra, 'start_runtime_monitoring', lambda config: starts.append('monitor'))
    ui_server._start_remote_monitors(rig.cfg, startup=True)
    assert starts == ['worker', 'monitor']
    monkeypatch.setattr(ra, 'status', lambda *args: {'running': True})
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    monkeypatch.setattr(ra, '_start_candidate_connector', lambda *args: (222, 'fixture-new'))
    monkeypatch.setattr(ra, '_wait_candidate_ready', lambda *args: True)
    monkeypatch.setattr(ra, '_promote_candidate_connector', lambda *args, **kwargs: {'pid': 111})
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda old: old['pid'] == 111)
    result = ra.apply_settings({'transport_protocol': 'http2'})
    assert result['ok'] and result['previous_connector_drained']
    assert cs.journal.path.read_text() == 'not json'


def test_candidate_not_ready_does_not_restore_untouched_connector(rig, monkeypatch):
    monkeypatch.setattr(ra, 'status', lambda *args: {'running': True})
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    monkeypatch.setattr(ra, '_start_candidate_connector', lambda *args: (222, 'fixture-new'))
    monkeypatch.setattr(ra, '_wait_candidate_ready', lambda *args: False)
    monkeypatch.setattr(ra, '_discard_candidate_connector', lambda *args: None)
    op = cr.prepare(rig.actor, 'protocol', 'quic')
    cr.apply(rig.actor, op)
    assert (outcome()['result'], outcome()['reason']) == ('failed', 'candidate_not_ready')
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    state = cs.journal.load(rig.cfg)
    assert not state['operation'] and not state.get('debt')


def test_explicit_new_intent_supersedes_recovery(rig):
    op = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    cs.journal.restore(cs.journal.load(rig.cfg), 'verification_failed')
    new = cr.prepare(rig.actor, 'protocol', 'http2')
    assert new['id'] != op['id']
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cs.journal.applying(rig.cfg, op['id'])


@pytest.mark.parametrize('restored_by', ['active', 'candidate', 'draining'])
def test_pin_outage_survives_ordinary_candidates_without_stealing_the_lane(rig, monkeypatch, restored_by):
    # With route optimization enabled, unregistered candidates repeatedly occupy
    # the lane for ~30 s. Their existence is not a successful connection.
    window = cs.PinFailureWindow(lambda: rig.clock[0])
    candidate = {'pid': 222, 'started_at': 2, 'metrics_url': 'fixture-candidate'}
    records = {'active': rig.active}
    monkeypatch.setattr(ra, '_state_connector', records.get)
    monkeypatch.setattr(ra, '_connector_lane_reserved_locked', lambda: any(records.get(k) for k in ('candidate', 'draining')))
    secondary_count = 0
    monkeypatch.setattr(cr, '_registered', lambda url: secondary_count)
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()

    def tick(at, *, busy=False, count=0, pid_state='cloudflared', sampled=True):
        rig.clock[0] = at
        records['candidate'] = candidate if busy else None
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state=pid_state,
                        sample=SimpleNamespace(registered_connections=count) if sampled else None, max_gap=22)
        return cs.journal.load(rig.cfg).get('pinAttempt')

    for at in range(0, 170, 10):
        assert tick(at, busy=at % 40 != 0) is None
    secondary_count = 1
    if restored_by == 'draining':
        records['draining'] = candidate
    assert tick(170, count=int(restored_by == 'active'), busy=restored_by == 'candidate') is None
    records.pop('draining', None)
    secondary_count = 0
    for at in range(180, 360, 10):
        # A single unobserved tick preserves the window, but neither adjoining
        # interval is counted. Exercise each source of an unknown active sample.
        assert tick(at, busy=at % 40 != 0,
                    count=None if at == 240 and restored_by == 'active' else 0,
                    sampled=not (at == 240 and restored_by == 'candidate'),
                    pid_state='unknown' if at == 240 and restored_by == 'draining' else 'cloudflared') is None
    assert tick(360) is None
    assert tick(370) is None
    assert tick(380, busy=True) is None  # Due, but the ordinary candidate still owns the lane.
    attempt = tick(390)
    assert attempt and attempt['pins'] == {'protocol': 'quic', 'ipFamily': '4'}
    assert cs.value(V2Config.load(), 'protocol') == 'quic'


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
    attempt = cs.journal.load(rig.cfg)['pinAttempt']
    assert cr.prepare(rig.actor, 'protocol', cs.value(rig.cfg, 'protocol')) is None
    assert cs.journal.load(rig.cfg)['pinAttempt'] == attempt
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
