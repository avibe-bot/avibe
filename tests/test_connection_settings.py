"""Disposable config/journal and connector doubles. No phone, public tunnel or wall-clock sleeps."""
import json
from contextlib import nullcontext
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
    records = {'active': rig.active}
    monkeypatch.setattr(ra, '_state_connector', records.get)
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: pid in (111, 222))
    monkeypatch.setattr(ra.runtime, 'stop_pid', lambda *args, **kwargs: pytest.fail('unexpected process stop'))
    def spawn(*args):
        records['candidate'] = {'pid': 222, 'started_at': 2, 'metrics_url': 'fixture-new'}
        return 222, 'fixture-new'
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(ra, '_wait_candidate_ready', lambda *args: True)
    events = []
    def promote(*args, **kwargs):
        events.append('promote')
        rig.active.update(records.pop('candidate'))
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


@pytest.mark.parametrize('same_connector', [True, False])
def test_restart_after_promotion_with_failed_verifying_write(rig, monkeypatch, same_connector):
    # Use the real promotion and on-disk connector state; no child process or
    # network is needed to lose the following journal write and restart.
    ra._replace_state_connector('active', dict(rig.active))
    monkeypatch.setattr(ra, '_state_connector', lambda key: ra._read_state().get(key))
    monkeypatch.setattr(ra, '_read_pid', lambda: ra._read_state()['pid'])
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: pid in (111, 222))
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    monkeypatch.setattr(ra, 'status', lambda cfg=None: {'running': True})
    candidate = {'pid': 222, 'started_at': 2, 'metrics_url': 'fixture-new'}
    def spawn(*args):
        ra._replace_state_connector('candidate', dict(candidate))
        return candidate['pid'], candidate['metrics_url']
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(ra, '_wait_candidate_ready', lambda *args: True)
    monkeypatch.setattr(ra, '_connector_ready_now', lambda *args: True)
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda old: True)
    monkeypatch.setattr(cr, '_public_check', lambda url: pytest.fail('verification was never journaled'))
    stopped = []
    monkeypatch.setattr(ra.runtime, 'stop_pid', lambda pid, timeout: stopped.append(pid) or True)
    save = cs.journal.save
    interrupted = []
    def lose_write(state):
        if (state.get('operation') or {}).get('state') == 'verifying':
            interrupted.append(ra._read_state()['active']['pid'])
            raise OSError('fixture: verification record not written')
        save(state)
    monkeypatch.setattr(cs.journal, 'save', lose_write)
    change = cr.prepare(rig.actor, 'protocol', 'http2')
    cr.apply(rig.actor, change)
    assert interrupted == [222] and not stopped
    if not same_connector:
        ra._replace_state_connector('active', dict(candidate, started_at=3, metrics_url='fixture-successor'))
    monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: rig.clock[0]))
    cr.recover_startup()
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    rig.clock[0] += cs.RESTORE_SECONDS + 1
    assert not cr.recover()
    assert stopped == ([222] if same_connector else [])
    assert rig.request.is_file()


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


def test_unknown_pin_replica_owner_defers_cleanup_not_runtime_monitoring(rig, monkeypatch, caplog):
    from vibe import project_access_sync

    candidate = dict(pid=222, metrics_url='fixture-replica', pin_attempt='fixture-attempt')
    slots = {'candidate': candidate, 'active': rig.active}
    workers = []
    pending = []
    monkeypatch.setattr(ra, '_state_connector', slots.get)
    monkeypatch.setattr(ra, '_RECOVERY_THREAD', None)
    monkeypatch.setattr(ra, '_QUALITY_MONITOR_STARTED', False)
    monkeypatch.setattr(ra, '_RECOVERY_STATE', {})
    monkeypatch.setattr(ra, '_QUALITY_CONTEXT', None)
    monkeypatch.setattr(ra, '_QUALITY_EVALUATOR', None)
    def quality_thread(*, target, args, kwargs, **unused):
        assert target is ra._quality_monitor_loop
        pending.append(kwargs['orphan_candidate_pending'])
        def launch():
            if len(pending) == 1:
                raise RuntimeError('fixture thread refused')
            workers.append('quality')
        return SimpleNamespace(start=launch)
    monkeypatch.setattr(ra, 'threading', SimpleNamespace(Thread=quality_thread, local=__import__('threading').local,
                                                        get_ident=__import__('threading').get_ident))
    for module, name in ((ra, 'start_status_heartbeat'), (ra, 'start_authorization_revision_polling'),
                         (project_access_sync, 'start_project_access_sync'), (ra, 'start_resource_acl_sync_polling')):
        monkeypatch.setattr(module, name, lambda cfg, name=name: workers.append(name))
    monkeypatch.setattr(ra, '_discard_candidate_connector', lambda *a, **kw: pytest.fail('ownership unknown'))
    monkeypatch.setattr(ra.runtime, 'stop_pid', lambda *a, **kw: pytest.fail('ownership unknown'))
    monkeypatch.setattr(ra, '_promote_candidate_connector', lambda *a, **kw: pytest.fail('ownership unknown'))
    cs.journal.path.parent.mkdir(exist_ok=True, parents=True)
    cs.journal.path.write_text('unreadable fixture')
    policy_workers = ['start_status_heartbeat', 'start_authorization_revision_polling',
                      'start_project_access_sync', 'start_resource_acl_sync_polling']
    assert not ra.start_runtime_monitoring(rig.cfg) and workers == policy_workers
    workers.clear()
    assert ra.start_runtime_monitoring(rig.cfg) and workers == ['quality', *policy_workers]
    assert ra.start_tunnel_quality_monitor()  # acknowledgement of the same worker, not a second launch
    assert pending == [True, True] and not ra._reconcile_orphan_candidate_when_free()
    assert caplog.text.count('Connection settings journal unavailable') == 1
    assert cs.journal.path.read_text() == 'unreadable fixture' and slots['active'] == rig.active
    # A subsequent tick can distinguish a previous worker from one still running.
    cs.journal.path.unlink()
    retired = []
    monkeypatch.setattr(ra, '_discard_candidate_connector', lambda pid, **kw: retired.append((pid, kw)))
    assert ra._reconcile_orphan_candidate_when_free()
    assert retired == [(222, {'grace_seconds': ra.RECOVERY_DRAIN_SECONDS, 'expected_url': 'fixture-replica'})]


def test_orphaned_pin_replica_grace_does_not_hold_up_startup(rig, monkeypatch, connector_clock):
    candidate = dict(pid=222, metrics_url='fixture-orphan', pin_attempt='previous-worker')
    slots = {'candidate': candidate, 'active': rig.active}
    monkeypatch.setattr(ra, '_state_connector', slots.get)
    monkeypatch.setattr(ra, '_RECOVERY_THREAD', None)
    def retire(pid, *, grace_seconds, expected_url):
        assert pid == candidate['pid'] and expected_url == candidate['metrics_url']
        assert grace_seconds == ra.RECOVERY_DRAIN_SECONDS
        connector_clock.sleep(2 * grace_seconds)  # slow stop and post-escalation wait
        slots.pop('candidate')
    monkeypatch.setattr(ra, '_discard_candidate_connector', retire)
    start = connector_clock.monotonic()
    assert not ra._reconcile_orphan_candidate_when_free(before_listen=True)
    assert connector_clock.monotonic() == start and slots['candidate'] is candidate
    assert ra._reconcile_orphan_candidate_when_free()  # deferred monitor tick, same lifecycle guard
    assert connector_clock.monotonic() - start == 70 and slots == {'active': rig.active}


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


@pytest.mark.parametrize('damage', [
    {'pinRoute': 'fixture'}, {'pinRoute': {'attempt': 'fixture'}},
    {'pinRoute': {'attempt': {'id': 'a', 'boot': 'b', 'pins': {}}, 'generation': 'g', 'phase': 'comparing',
                  'relaxed': ['protocol'], 'protocol': 'http2', 'remember': False, 'previous': 7}},
    {'pinAttempt': {'id': 'a', 'boot': 'b'}}, {'pinAttempt': {'id': 'a', 'boot': 'b', 'pins': {}}},
    {'pinRoute': {'attempt': {'id': 'a', 'boot': 'b', 'pins': {}, 'request': None}, 'generation': 'g',
                  'phase': 'comparing', 'relaxed': ['protocol'], 'protocol': 'http2', 'remember': False}},
    {'outcome': 'fixture'}, {'returnedToAutomatic': {'changes': 'x'}},
    {'verifiedPreference': []}])
def test_malformed_journal_records_degrade_and_monitoring_still_starts(rig, monkeypatch, damage):
    from vibe import ui_server
    state = {'version': 1, 'binding': cs.relay_pairing(rig.cfg), 'operation': None, 'outcome': None,
             'returnedToAutomatic': None, **damage}
    cs.journal.path.parent.mkdir(exist_ok=True, parents=True)
    cs.journal.path.write_text(json.dumps(state))
    with pytest.raises(cs.ConnectionError, match='connection_state_unavailable'):
        cs.journal.load(rig.cfg)
    starts = []
    monkeypatch.setattr(cs, 'adopt_host_enrollment', lambda: None)
    monkeypatch.setattr(cr, 'start', lambda: starts.append('worker'))
    monkeypatch.setattr(ra, 'start_runtime_monitoring', lambda config: starts.append('monitor'))
    ui_server._start_remote_monitors(rig.cfg, startup=True)
    assert starts == ['worker', 'monitor']


def test_unexpected_recovery_error_still_starts_worker_and_monitoring(rig, monkeypatch):
    from vibe import ui_server
    starts = []
    monkeypatch.setattr(cs, 'adopt_host_enrollment', lambda: None)
    monkeypatch.setattr(cr, 'recover_startup', lambda: (_ for _ in ()).throw(AttributeError('fixture')))
    monkeypatch.setattr(cr, 'start', lambda: starts.append('worker'))
    monkeypatch.setattr(ra, 'start_runtime_monitoring', lambda config: starts.append('monitor'))
    with pytest.raises(AttributeError):
        ui_server._start_remote_monitors(rig.cfg, startup=True)
    assert starts == ['worker', 'monitor']


def test_temporary_route_lookup_error_never_breaks_tunnel_start(rig, monkeypatch):
    cs.journal.path.parent.mkdir(exist_ok=True, parents=True)
    cs.journal.path.write_text(json.dumps({'version': 1, 'binding': cs.relay_pairing(rig.cfg), 'operation': None,
                                           'outcome': None, 'returnedToAutomatic': None}))
    monkeypatch.setattr(cr, '_active_pin_route', lambda *a: (_ for _ in ()).throw(KeyError('request')))
    assert cr.keep_temporary_connector(rig.cfg, 'fixture-binary', {'provider': 'fixture'}) is False


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


@pytest.mark.parametrize('past_bound', [False, True])
def test_rejected_replacement_resumes_previous_connector_repair(rig, monkeypatch, past_bound):
    old = cs.journal.begin(rig.cfg, 'protocol', 'quic')
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    cs.journal.verifying(rig.cfg, old['id'], 'rejected-generation')
    cs.journal.restore(cs.journal.load(rig.cfg), 'verification_failed')
    monkeypatch.setattr(cr, '_repair_runtime', lambda *args: False)
    if past_bound:
        rig.clock[0] += cs.RESTORE_SECONDS + 1
    assert not cr.recover()
    assert cs.value(V2Config.load(), 'protocol') == 'auto'
    new = cr.prepare(rig.actor, 'protocol', 'http2')
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cs.journal.applying(rig.cfg, old['id'])
    monkeypatch.setattr(ra, 'apply_settings', lambda *a, **kw: {'ok': False, 'detail': 'candidate_not_ready'})
    cr.apply(rig.actor, new)
    latest = outcome()
    assert (latest['id'], latest['result'], latest['reason']) == (new['id'], 'failed', 'candidate_not_ready')
    repairs = []
    def repair(cfg, op, at_bound):
        repairs.append((cs.value(cfg, 'protocol'), op.get('generation')))
        return True
    monkeypatch.setattr(cr, '_repair_runtime', repair)
    assert cr.recover()
    assert repairs == [('auto', 'rejected-generation')]
    assert outcome() == latest


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
        rig.clock[0] = at * 1.0004  # Ordinary 4 ms of jitter per 10 s monitor tick.
        records['candidate'] = candidate if busy else None
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state=pid_state,
                        sample=SimpleNamespace(registered_connections=count) if sampled else None, interval_seconds=10)
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


@pytest.fixture
def pin_fleet(rig, monkeypatch, connector_clock):
    """Distinct candidate and active metrics; every sample uses the connector's own clock."""
    slots = {'active': dict(rig.active)}
    starts = []
    fleet = SimpleNamespace(slots=slots, starts=starts, sample=None)
    def spawn(cfg, binary, protocol, *, pin_attempt=None):
        assert not slots.get('candidate') and not slots.get('draining')
        pid = 300 + len(starts)
        record = dict(pid=pid, started_at=connector_clock.monotonic(), metrics_url=f'fixture-{pid}',
                      pin_attempt=pin_attempt, requested_protocol=protocol, ipFamily=cs.value(cfg, 'ipFamily'), outbound=cs.value(cfg, 'outbound'))
        slots['candidate'] = record
        starts.append(record)
        return pid, record['metrics_url']
    def counts(url):
        if url == rig.active['metrics_url']:
            return 0
        record = next(r for r in slots.values() if r['metrics_url'] == url)
        return fleet.sample(record, connector_clock.monotonic() - record['started_at'])
    def discard(pid, *, grace_seconds=8, expected_url=None):
        candidate = slots['candidate']
        assert pid == candidate['pid'] and slots['active']['pid'] != pid
        if candidate['pin_attempt'] is not None:
            assert grace_seconds == ra.RECOVERY_DRAIN_SECONDS and expected_url == candidate['metrics_url']
        del slots['candidate']
    def promote(pid, **kwargs):
        candidate = slots.pop('candidate')
        assert candidate['pid'] == pid
        assert bool(kwargs.get('registered_only')) == (candidate['pin_attempt'] is not None)
        slots['draining'], slots['active'] = slots['active'], candidate
        return slots['draining']
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-binary')
    monkeypatch.setattr(ra, '_state_connector', slots.get)
    monkeypatch.setattr(ra, '_read_pid', lambda: slots['active']['pid'])
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: any(r['pid'] == pid for r in slots.values()))
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(cr, '_registered', counts)
    monkeypatch.setattr(ra, '_discard_candidate_connector', discard)
    monkeypatch.setattr(ra, '_promote_candidate_connector', promote)
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda old: slots.pop('draining') == old)
    return fleet


@pytest.fixture
def transport_outage(rig, monkeypatch, connector_clock, pin_fleet):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol, cloud.tunnel_token = 'quic', 'fixture-tunnel'
    rig.cfg.save()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    pin_fleet.sample = lambda record, age: int(record['requested_protocol'] == 'http2' and age >= 5)
    queue_outage(rig, connector_clock)
    return pin_fleet


@pytest.mark.parametrize('fault', ['preference', 'config', 'receipt'])
def test_pin_commit_survives_storage_fault(rig, monkeypatch, transport_outage, fault):
    # The only connector that can register is HTTP/2; IPv4 is unrelated.
    journal = cs.journal
    with monkeypatch.context() as fail:
        def unavailable(*args, **kwargs):
            raise OSError('injected storage failure')
        if fault == 'preference':
            fail.setattr(ra, '_set_preferred_protocol', unavailable)
        elif fault == 'config':
            fail.setattr(V2Config, 'save', unavailable)
        else:
            save = journal.save
            def receipt(state):
                if state.get('returnedToAutomatic'):
                    unavailable()
                return save(state)
            fail.setattr(journal, 'save', receipt)
        with pytest.raises(OSError):
            cr._consume_pin_attempt()
    route = journal.load(V2Config.load())['pinRoute']
    assert route['phase'] == 'committing' and route['generation'] == ra.connector_generation()
    before = len(transport_outage.starts)
    cr._consume_pin_attempt()
    current = V2Config.load()
    assert len(transport_outage.starts) == before and cs.value(current, 'protocol') == 'auto'
    assert cs.value(current, 'ipFamily') == '4' and ra._stored_preferred_protocol() == 'http2'
    state = journal.load(current)
    assert 'pinAttempt' not in state and 'pinRoute' not in state
    assert [c['setting'] for c in state['returnedToAutomatic']['changes']] == ['protocol']


@pytest.mark.parametrize('fence', ['generation', 'request', 'pair', 'edit'])
def test_superseded_generation_cannot_finish_pending_pin_write(rig, monkeypatch, transport_outage, fence):
    with monkeypatch.context() as fail:
        fail.setattr(ra, '_set_preferred_protocol', lambda p: (_ for _ in ()).throw(OSError('fixture')))
        with pytest.raises(OSError):
            cr._consume_pin_attempt()
    live = V2Config.load()
    if fence == 'generation':
        transport_outage.slots['active']['started_at'] += 1  # same PID and address are not the same process
    elif fence == 'request':
        rig.request.unlink()
    elif fence == 'pair':
        live.remote_access.vibe_cloud.instance_secret = 'replacement-pair'
        live.save()
    else:
        live.remote_access.vibe_cloud.transport_protocol = 'http2'
        live.save()
    cr._consume_pin_attempt()
    live = V2Config.load()
    state = cs.journal.load(live)
    assert cs.value(live, 'protocol') == ('http2' if fence == 'edit' else 'quic')
    assert not state.get('pinAttempt') and not state['returnedToAutomatic'] and not ra._stored_preferred_protocol()


@pytest.mark.parametrize('field,phase,page_ready,expected_seconds', [
    ('protocol', 'comparing', False, 60),   # both explicit transports time out on full HA
    ('outbound', 'confirming', False, 30),  # releasing just the address still leaves QUIC pinned
    ('protocol', 'comparing', True, 6),     # observed HTTP/2 can also let the Owner finish the repair
])
def test_owner_edit_preempts_attribution_without_retiring_active(rig, monkeypatch, connector_clock, pin_fleet,
                                                                 field, phase, page_ready, expected_seconds):
    fleet = pin_fleet
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol = 'quic'
    cloud.edge_bind_address = '192.0.2.1' if field == 'outbound' else ''
    rig.cfg.save()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    monkeypatch.setattr(ra, 'tunnel_quality_snapshot', lambda: {'protocol': 'http2'})
    queue_outage(rig, connector_clock)
    owner = []
    def sample(record, age):
        if record == fleet.slots.get('candidate') and not owner:
            state = cs.journal.load(V2Config.load())
            if (state.get('pinRoute') or {}).get('phase') == phase:
                assert cr.prepare(rig.actor, field, cs.value(rig.cfg, field)) is None
                with pytest.raises(cs.ConnectionError, match='binding_changed'):
                    cr.prepare(cs.Actor('wrong-pair'), field, cs.AUTOMATIC[field])
                monkeypatch.setattr(ra, '_connector_lane_reserved_locked', lambda: bool(fleet.slots.get('candidate') or fleet.slots.get('draining')))
                owner.append(cr.prepare(rig.actor, field, cs.AUTOMATIC[field]))
        return int(record['requested_protocol'] == 'http2' and not record['outbound'] and age >= 5)
    fleet.sample = sample
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cr._consume_pin_attempt()
    old_id = owner[0]['retiringPin']
    assert old_id and 'candidate' not in fleet.slots
    original = fleet.slots['active']
    assert original['requested_protocol'] == 'http2' and not original['outbound']
    assert not cs.journal.load(V2Config.load()).get('pinAttempt') and not ra._stored_preferred_protocol()
    # Use the actual page writer and its bounded full-HA poll. Only process
    # state, readiness and the independent public request are substituted.
    monkeypatch.setattr(ra, 'status', lambda *a, **kw: {'running': True, 'pid_state': 'cloudflared', 'pid': fleet.slots['active']['pid']})
    monkeypatch.setattr(ra, '_connector_ready_now', lambda pid, url: page_ready and
                        connector_clock.monotonic() - fleet.slots['candidate']['started_at'] >= 6)
    monkeypatch.setattr(cr, '_public_check', lambda url: True)
    before, started = len(fleet.starts), connector_clock.monotonic()
    cr.apply(rig.actor, owner[0])
    assert connector_clock.monotonic() - started == expected_seconds
    assert [r['requested_protocol'] for r in fleet.starts[before:]] == (
        ['http2'] if page_ready else ['http2', 'quic'] if field == 'protocol' else ['quic'])
    live = V2Config.load()
    state = cs.journal.load(live)
    assert not state['operation'] and not state['returnedToAutomatic']
    if page_ready:
        assert state['outcome']['result'] == 'applied' and cs.value(live, field) == cs.AUTOMATIC[field]
        assert not state.get('pinAttempt') and fleet.slots['active'] != original
        return
    assert state['outcome']['reason'] == 'candidate_not_ready' and cs.value(live, field) == cs.value(rig.cfg, field)
    assert state['pinRoute']['phase'] == 'comparing' and state['pinAttempt']['id'] != old_id
    assert state['pinAttempt'] == state['pinRoute']['attempt'] and fleet.slots['active'] == original
    cr._consume_pin_attempt()
    changes = cs.journal.inspect(V2Config.load())['returnedToAutomatic']['changes']
    assert {c['setting'] for c in changes} == ({'protocol', 'outbound'} if field == 'outbound' else {'protocol'})


@pytest.mark.parametrize('field,target', [('protocol', 'auto'), ('ipFamily', 'auto'), ('outbound', '192.0.2.10')])
@pytest.mark.parametrize('public_ok', [False, True])
def test_page_auto_verifies_explicit_opposite_and_remembers_winner(rig, monkeypatch, pin_fleet, field, target, public_ok):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol = 'quic' if field == 'protocol' else 'auto'
    rig.cfg.save()
    monkeypatch.setattr(ra, 'network_interfaces', lambda: {'interfaces': [{'address': '192.0.2.10'}]})
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    monkeypatch.setattr(ra, 'tunnel_quality_snapshot', lambda: {'protocol': 'quic'})
    monkeypatch.setattr(ra, 'status', lambda *a, **kw: {'running': True, 'pid_state': 'cloudflared'})
    monkeypatch.setattr(ra, '_connector_ready_now', lambda *args: pin_fleet.slots['candidate']['requested_protocol'] == 'http2')
    monkeypatch.setattr(cr, '_public_check', lambda url: public_ok)
    op = cr.prepare(rig.actor, field, target)
    cr.apply(rig.actor, op)
    assert [row['requested_protocol'] for row in pin_fleet.starts] == ['quic', 'http2']
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)  # cold start reads history, not memory
    state = cs.journal.load(V2Config.load())
    if public_ok:
        assert state['outcome']['reason'] == 'verified' and not state['operation']
        assert ra._initial_connector_protocol(V2Config.load()) == 'http2'
    else:
        assert state['operation']['state'] == 'restoring' and not ra._stored_preferred_protocol()


@pytest.mark.parametrize('same_generation', [True, False])
def test_verified_preference_write_failure_does_not_undo_owner_change(rig, monkeypatch, pin_fleet, same_generation):
    rig.cfg.remote_access.vibe_cloud.transport_protocol = 'quic'
    rig.cfg.save()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    monkeypatch.setattr(ra, 'tunnel_quality_snapshot', lambda: {'protocol': 'http2'})
    monkeypatch.setattr(ra, 'status', lambda *a, **kw: {'running': True, 'pid_state': 'cloudflared'})
    monkeypatch.setattr(ra, '_connector_ready_now', lambda *args: True)
    monkeypatch.setattr(cr, '_public_check', lambda url: True)
    op = cr.prepare(rig.actor, 'protocol', 'auto')
    def unavailable(protocol):
        raise OSError('fixture: history unwritable')
    with monkeypatch.context() as fail:
        fail.setattr(ra, '_set_preferred_protocol', unavailable)
        cr.apply(rig.actor, op)
        assert not cr.recover()
        live = V2Config.load()
        state = cs.journal.load(live)
        assert state['outcome']['reason'] == 'verified' and not state['operation'] and not state.get('debt')
        assert cs.value(live, 'protocol') == 'auto' and not ra._stored_preferred_protocol()
    if not same_generation:
        pin_fleet.slots['active']['started_at'] += 1
    assert cr.recover()
    assert not cs.journal.load(V2Config.load()).get('verifiedPreference')
    assert ra._stored_preferred_protocol() == ('http2' if same_generation else None)


@pytest.mark.parametrize('ending', ['lock_busy', 'promotion_rolled_back'])
@pytest.mark.parametrize('fence', ['same', 'stop', 'rebind', 'generation'])
def test_ineffective_owner_handover_finishes_and_rearms_only_its_route(rig, monkeypatch, transport_outage, ending, fence):
    fleet = transport_outage
    sample, owner = fleet.sample, []
    def counts(record, age):
        if not owner and record is fleet.slots.get('candidate') and len(fleet.starts) == 3:
            owner.append(cr.prepare(rig.actor, 'protocol', 'auto'))
        return sample(record, age)
    fleet.sample = counts
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cr._consume_pin_attempt()
    reservation, = owner
    old = cs.journal.load(V2Config.load())['pinRoute']['attempt']
    assert old['id'] == reservation['retiringPin']
    active = fleet.slots['active']
    monkeypatch.setattr(ra, 'status', lambda *a, **kw: {'running': True, 'pid_state': 'cloudflared'})
    monkeypatch.setattr(ra, '_connector_ready_now', lambda *args: True)
    def reject_promotion(*args, **kw):
        assert cs.value(V2Config.load(), 'protocol') == 'auto'  # after the real persist
        raise RuntimeError('candidate_not_ready')
    def busy(*args, **kw):
        raise ra._ConnectorLifecycleBusy()
    with monkeypatch.context() as fail:
        if ending == 'lock_busy':
            fail.setattr(ra, '_connector_lifecycle', busy)
        else:
            fail.setattr(ra, '_promote_candidate_connector', reject_promotion)
        cr.apply(rig.actor, reservation)
    live = V2Config.load()
    state = cs.journal.load(live)
    assert cs.value(live, 'protocol') == 'quic' and fleet.slots['active'] is active
    assert not state.get('pinAttempt') and not ra._stored_preferred_protocol()
    if ending == 'lock_busy':
        assert not state['operation'] and state['outcome']['result'] == 'failed'
        assert state['pinRoute']['resumeAfterFailure']
    else:
        assert state['operation']['writeStarted'] and state['operation']['state'] == 'restoring'
    if fence == 'stop':
        rig.request.unlink()
    elif fence == 'rebind':
        live.remote_access.vibe_cloud.instance_secret = 'replacement-pair'
        live.save()
    elif fence == 'generation':
        active['started_at'] += 1  # same PID and address, not the same generation
    monkeypatch.setattr(ra, '_running_signature', lambda pid: ra._runtime_signature(cr._pin_trial(live, ['protocol'], 'http2'), 'fixture-binary'))
    cr.recover()
    state = cs.journal.load(V2Config.load())
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cr._pin_live(old)
    if fence == 'same':
        assert not state['operation'] and not state.get('debt')
        assert state['outcome']['result'] == ('failed' if ending == 'lock_busy' else 'restored')
        assert state['pinAttempt']['id'] != old['id'] and state['pinRoute']['phase'] == 'comparing'
        cr._consume_pin_attempt()
        assert cs.value(V2Config.load(), 'protocol') == 'auto'
    else:
        assert not state.get('pinAttempt') and cs.value(V2Config.load(), 'protocol') == 'quic'


@pytest.mark.parametrize('retire_after,allowed', [(74.75, True), (75.25, False)])
def test_preempting_owner_waits_only_for_its_replica(rig, monkeypatch, connector_clock, transport_outage, retire_after, allowed):
    fleet = transport_outage
    sample, discard = fleet.sample, ra._discard_candidate_connector
    owner = []
    def counts(record, age):
        if not owner and record == fleet.slots.get('candidate') and len(fleet.starts) == 3:
            owner.append(cr.prepare(rig.actor, 'protocol', 'auto'))
        return sample(record, age)
    def held(pid, **kwargs):
        if not owner:
            discard(pid, **kwargs)
    fleet.sample = counts
    monkeypatch.setattr(ra, '_discard_candidate_connector', held)
    with pytest.raises(cs.ConnectionError, match='superseded'):
        cr._consume_pin_attempt()
    stopped = fleet.slots['candidate']
    assert stopped['pin_attempt'] == owner[0]['retiringPin']
    began = connector_clock.monotonic()
    def advance(seconds):
        rig.clock[0] += seconds
        if connector_clock.monotonic() - began >= retire_after and fleet.slots.get('candidate') is stopped:
            fleet.slots.pop('candidate')
    monkeypatch.setattr(connector_clock, 'sleep', advance)
    monkeypatch.setattr(ra, '_discard_candidate_connector', discard)
    monkeypatch.setattr(ra, 'status', lambda *a, **kw: {'running': True, 'pid_state': 'cloudflared', 'pid': fleet.slots['active']['pid']})
    monkeypatch.setattr(ra, '_connector_ready_now', lambda pid, url: True)
    monkeypatch.setattr(cr, '_public_check', lambda url: True)
    n = len(fleet.starts)
    cr.apply(rig.actor, owner[0])
    assert connector_clock.monotonic() - began == (retire_after if allowed else 75)
    live = V2Config.load()
    state = cs.journal.load(live)
    if allowed:
        assert len(fleet.starts) == n + 1 and state['outcome']['result'] == 'applied'
        assert cs.value(live, 'protocol') == 'auto' and not state.get('pinAttempt')
    else:
        assert len(fleet.starts) == n and fleet.slots['candidate'] is stopped
        assert state['outcome']['reason'] == 'candidate_cleanup_unavailable' and cs.value(live, 'protocol') == 'quic'
        assert state['pinAttempt']['id'] != owner[0]['retiringPin']
        advance(.25)
        cr._consume_pin_attempt()
        assert cs.value(V2Config.load(), 'protocol') == 'auto'


@pytest.mark.parametrize('entry', ['api', 'config', 'cli', 'ui_restart', 'lost_registration', 'metrics_unreadable'])
def test_start_entries_respect_registered_route_and_ui_restart(rig, monkeypatch, transport_outage, entry):
    from tests.ui_server_test_helpers import csrf_headers
    from vibe import cli, ui_server as ui
    # Force an unknown online comparison. The active HTTP/2 path must survive
    # normal starts even when called by a process with a different journal.boot.
    fleet = transport_outage
    sample = fleet.sample
    fleet.sample = lambda record, age: None if len(fleet.starts) == 3 and record == fleet.slots.get('candidate') else sample(record, age)
    cr._consume_pin_attempt()
    live = V2Config.load()
    active = fleet.slots['active']
    assert cr.view(rig.actor)['temporaryRoute'] == dict(phase='comparison_unavailable', protocol='http2', differences=['protocol'])
    tmp = cr._pin_trial(live, ['protocol'], 'http2')
    monkeypatch.setattr(ra, '_running_signature', lambda pid: ra._runtime_signature(tmp, 'fixture-binary'))
    monkeypatch.setattr(ra, '_running_connector_has_metrics', lambda pid: pid == active['pid'])
    monkeypatch.setattr(ra, '_ensure_cloudflared_pin', lambda config: None)
    monkeypatch.setattr(ra, 'status', lambda cfg=None: {'running': 'active' in fleet.slots, 'pid_state': 'cloudflared',
                                                       'pid': (fleet.slots.get('active') or {}).get('pid')})
    stopped = []
    def stop(cfg):
        assert entry in ('ui_restart', 'lost_registration')
        stopped.append(fleet.slots.pop('active')['pid'])
        return {'ok': True, 'running': False}
    monkeypatch.setattr(ra, '_stop_connector', stop)
    monkeypatch.setattr(ra, '_allocate_metrics_url', lambda: 'http://127.0.0.1:12345')
    monkeypatch.setattr(ra, '_clear_cloudflared_logs', lambda: None)
    monkeypatch.setattr(ra.runtime, 'spawn_background', lambda *a, **kw: 707)
    def publish(pid, cfg, binary, metrics_url, *, requested_protocol):
        fleet.slots['active'] = dict(pid=pid, metrics_url=metrics_url, started_at=rig.clock[0], requested_protocol=requested_protocol)
    monkeypatch.setattr(ra, '_write_state', publish)
    monkeypatch.setattr(ra, '_retire_legacy_cloudflared', lambda *args: None)
    started = []
    monkeypatch.setattr(ra, '_report_runtime_status_async', lambda cfg, **kwargs: started.append(kwargs['event']))
    monkeypatch.setattr(ui, '_ensure_remote_access_monitoring', lambda *args: None)
    monkeypatch.setattr(ui, '_remote_status_for_viewer', lambda result: result)
    if entry == 'cli':
        monkeypatch.setattr(cs, 'journal', cs.Journal())  # foreign process, same state files
        assert cli.cmd_remote_start(SimpleNamespace(json=False)) == 0
    elif entry == 'ui_restart':
        monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: rig.clock[0]))
        cr.recover_startup()
        assert 'pinRoute' not in cs.journal.load(live)
        ui._reconcile_remote_access_for_ui_start(live)
    elif entry == 'metrics_unreadable':
        with monkeypatch.context() as scrape:
            scrape.setattr(cr, '_registered', lambda url: None)
            assert not cr._pin_anchor(ra.connector_generation())  # still no attribution evidence
            assert not ra.start(live)['started']
    elif entry == 'lost_registration':
        fleet.sample = lambda record, age: 0
        assert ra.start(live)['started']
    else:
        client = ui.app.test_client()
        if entry == 'config':
            # Exercise the actual post-save reconcile dispatch, not any unrelated service lifecycle.
            monkeypatch.setattr(ui, '_save_config_and_runtime_decisions', lambda *a, **kw: (live, True, False, False, []))
        response = client.post('/api/config' if entry == 'config' else '/api/remote-access/start',
                               json={'remote_access': {}} if entry == 'config' else {}, headers=csrf_headers(client))
        assert response.status_code == 200
    assert started == ['start']
    assert cs.value(V2Config.load(), 'protocol') == 'quic'
    if entry in ('ui_restart', 'lost_registration'):
        assert stopped == [active['pid']] and fleet.slots['active']['pid'] == 707
        assert fleet.slots['active']['requested_protocol'] == 'quic'
    else:
        assert not stopped and fleet.slots['active'] == active
    assert not cr.keep_temporary_connector(live, 'different-binary', ra._runtime_signature(tmp, 'fixture-binary'))
    active['started_at'] += 1
    assert cr.view(rig.actor)['temporaryRoute'] is None


@pytest.mark.parametrize('receipt_written', [False, True])
def test_ui_restart_discards_transient_route_without_inventing_result(rig, monkeypatch, transport_outage, receipt_written):
    save = cs.journal.save
    def crash(state):
        if ((state.get('returnedToAutomatic') if receipt_written else (state.get('pinRoute') or {}).get('phase') == 'confirming')):
            raise OSError('simulated interruption')
        return save(state)
    monkeypatch.setattr(cs.journal, 'save', crash)
    with pytest.raises(OSError):
        cr._consume_pin_attempt()
    monkeypatch.setattr(cs, 'journal', cs.Journal(lambda: rig.clock[0]))
    cr.recover_startup()
    live = V2Config.load()
    state = cs.journal.load(live)
    assert not state.get('pinRoute') and not state.get('pinAttempt')
    assert cs.value(live, 'protocol') == ('auto' if receipt_written else 'quic')
    assert bool(state['returnedToAutomatic']) == receipt_written


@pytest.mark.parametrize('field,pinned', [('ipFamily', '4'), ('outbound', '192.0.2.1')])
@pytest.mark.parametrize('saved,automatic,expected_return', [(True, None, False), (False, True, True), (False, False, False), (None, None, False)])
def test_pin_recovery_requires_failed_saved_then_registered_automatic(rig, monkeypatch, connector_clock, pin_fleet, field, pinned, saved, automatic, expected_return):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.edge_ip_version = 'auto'
    setattr(cloud, cs.FIELDS[field], pinned)
    rig.cfg.save()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', 'http2')
    window = cs.PinFailureWindow(lambda: rig.clock[0])
    def tick(n, count=0):
        rig.clock[0] += n
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=count), interval_seconds=15)
    tick(0)
    for _ in range(11):
        tick(15)
    tick(14.9)
    assert not cs.journal.path.exists()
    tick(0.2)
    attempt = cs.journal.load(rig.cfg)['pinAttempt']
    assert cr.prepare(rig.actor, 'protocol', cs.value(rig.cfg, 'protocol')) is None
    assert cs.journal.load(rig.cfg)['pinAttempt'] == attempt
    def sample(record, elapsed):
        assert cs.value(V2Config.load(), field) == pinned
        result = saved if record[field] == pinned else automatic
        return None if result is None else int(result and elapsed >= 8.030)
    pin_fleet.sample = sample
    cr._consume_pin_attempt()
    relaxed = cs.AUTOMATIC[field]
    expected = [(pinned, 'http2')]
    if saved is False:
        expected += [(pinned, 'quic'), (relaxed, 'http2'),
                     (pinned, 'http2') if automatic else (relaxed, 'quic')]
        if automatic:
            expected.append((relaxed, 'http2'))  # confirm the fresh final dial, not the surviving registration
        assert pin_fleet.starts[1]['started_at'] - pin_fleet.starts[0]['started_at'] == pytest.approx(30)
    assert [(r[field], r['requested_protocol']) for r in pin_fleet.starts] == expected
    assert cs.value(V2Config.load(), field) == (relaxed if expected_return else pinned)
    assert bool(cs.journal.inspect(rig.cfg)['returnedToAutomatic']) == expected_return


# Every leg is (transport, retained IP family); all starts use the real bounded poll.
H4, Q4, HA, QA = ('http2', '4'), ('quic', '4'), ('http2', 'auto'), ('quic', 'auto')


@pytest.mark.parametrize('pin,hints,usable,working_family,search,control,released', [
    ('quic', ('quic', 'http2'), {'http2'}, '4', [Q4, H4], [Q4], {'protocol'}),
    ('http2', ('http2', 'quic'), {'quic'}, '4', [H4, Q4], [H4], {'protocol'}),
    ('auto', ('http2', 'quic'), {'http2'}, '4', [H4], [], set()),
    ('auto', ('http2', None), {'http2'}, '6', [H4, Q4, HA], [H4], {'ipFamily'}),
    ('auto', (None, 'http2'), {'http2'}, '4', [H4], [], set()),
    ('auto', (None, 'quic'), {'quic'}, '6', [Q4, H4, QA], [Q4], {'ipFamily'}),
    ('auto', (None, None), {'http2'}, '4', [H4], [], set()),
    ('auto', (None, None), {'quic'}, '4', [H4, Q4], [], set()),
    ('auto', (None, None), {'http2'}, '6', [H4, Q4, HA], [H4], {'ipFamily'}),
    ('auto', (None, None), {'quic'}, '6', [H4, Q4, HA, QA], [Q4], {'ipFamily'}),
    ('auto', (None, None), set(), '4', [H4, Q4, HA, QA], [], set()),
    ('quic', ('quic', None), set(), '4', [Q4, H4, QA, HA], [], set()),
])
def test_pin_transport_registration_and_restart(rig, monkeypatch, connector_clock, pin_fleet, pin, hints, usable, working_family, search, control, released):
    # Drive the real registration poll. On a UDP-blocked route explicit HTTP/2
    # first registered in 6.379 s; literal auto first registered in 96.674 s.
    # The reverse-protocol and address-family cases are contract simulations.
    rig.cfg.remote_access.vibe_cloud.transport_protocol = pin
    rig.cfg.save()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    preferred, observed = hints
    hist = ra._quality_history_path()
    hist.parent.mkdir(parents=True, exist_ok=True)
    ra.runtime.write_json(hist, {'schema_version': 2, 'preferred_protocol': preferred, 'context': {'keep': True}})
    if observed:
        ra.runtime.write_json(ra._quality_state_path(), {'schema_version': 2, 'protocol': observed})
    def sample(record, elapsed):
        # New observations after planning must not reorder the search.
        if elapsed == 0:
            ra.runtime.write_json(ra._quality_state_path(), {'schema_version': 2, 'protocol': 'http2'})
        current = V2Config.load()
        assert (cs.value(current, 'protocol'), cs.value(current, 'ipFamily')) == (pin, '4')
        assert not cs.journal.inspect(current)['returnedToAutomatic']
        protocol = record['requested_protocol']
        return int(protocol in usable and record['ipFamily'] in (working_family, 'auto')
                   and elapsed >= {'quic': 5., 'http2': 6.379}[protocol])
    pin_fleet.sample = sample
    window = cs.PinFailureWindow(lambda: rig.clock[0])
    for _ in range(13):
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=0), interval_seconds=15)
        rig.clock[0] += 15
    assert cs.journal.load(rig.cfg).get('pinAttempt')
    cr._consume_pin_attempt()
    legs = pin_fleet.starts
    assert [(r['requested_protocol'], r['ipFamily']) for r in legs] == search + control + (search[-1:] if released else [])
    assert [r['started_at'] - legs[0]['started_at'] for r in legs[:len(search)]] == [30 * i for i in range(len(search))]
    if not usable:
        assert connector_clock.monotonic() - legs[0]['started_at'] == 30 * len(search)
    current = V2Config.load()
    record = cs.journal.inspect(current)['returnedToAutomatic'] or {'changes': []}
    assert {c['setting'] for c in record['changes']} == released
    assert cs.value(current, 'protocol') == ('auto' if 'protocol' in released else pin)
    assert cs.value(current, 'ipFamily') == ('auto' if 'ipFamily' in released else '4')
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    remembered = usable and search[-1][0] != search[0][0]
    assert ra._stored_preferred_protocol() == (search[-1][0] if remembered else preferred)
    if remembered:
        assert ra._initial_connector_protocol(current) == search[-1][0]
    assert ra.runtime.read_json(hist)['context'] == {'keep': True}


def queue_outage(rig, clock):
    window = cs.PinFailureWindow(clock.monotonic)
    for _ in range(13):
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=0), interval_seconds=15)
        clock.sleep(15)


def test_repaired_binding_can_recover_retained_pin_without_prior_journal(rig, monkeypatch, connector_clock, pin_fleet):
    import ipaddress
    import time
    from tests.test_pairing_recovery import _response

    rig.cfg.remote_access.vibe_cloud.edge_ip_version = '4'
    rig.cfg.save()
    cs.journal.path.write_text('{')
    with monkeypatch.context() as pairing:
        pairing.setattr(ra, 'time', time)
        pairing.setattr(ra, '_resolve_pairing_backend_addresses', lambda *_: (ipaddress.ip_address('93.184.216.34'),))
        pairing.setattr(ra, '_json_request', lambda *_a, **_kw: _response('new-instance'))
        pairing.setattr(ra, '_continue_requested_after_pairing', lambda *_: None)
        pairing.setattr(ra, '_stop_direct_lifecycle_before_pairing_discard', lambda *_: None)
        pairing.setattr(ra, 'status', lambda *_: {'ok': True})
        assert ra.pair('fixture', 'https://backend.test')['ok']
    rig.cfg = V2Config.load()
    assert rig.cfg.remote_access.vibe_cloud.instance_id == 'new-instance'
    assert cs.value(rig.cfg, 'ipFamily') == '4' and not cs.journal.path.exists()
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', 'http2')
    queue_outage(rig, connector_clock)
    assert cs.journal.load(rig.cfg)['pinAttempt']['pins'] == {'ipFamily': '4'}
    pin_fleet.sample = lambda record, elapsed: int(record['ipFamily'] == 'auto' and elapsed >= 8.030)
    cr._consume_pin_attempt()
    assert cs.value(V2Config.load(), 'ipFamily') == 'auto'
    assert cs.journal.inspect(rig.cfg)['returnedToAutomatic']['changes']


def test_surviving_connection_does_not_prove_new_connections_work(rig, monkeypatch, connector_clock, pin_fleet):
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', 'http2')
    queue_outage(rig, connector_clock)
    start = connector_clock.monotonic()
    established = set()
    def sample(record, elapsed):
        # Admission depends only on time, never on family or transport.
        if elapsed >= 6 and 60 <= connector_clock.monotonic() - start < 68:
            established.add(record['pid'])
        return int(record['pid'] in established)
    pin_fleet.sample = sample
    cr._consume_pin_attempt()
    assert established == {pin_fleet.slots['active']['pid']}
    live = V2Config.load()
    assert cs.value(live, 'ipFamily') == '4'
    state = cs.journal.load(live)
    assert state['pinRoute']['phase'] == 'combination_unverified'
    assert not state.get('pinAttempt') and not state['returnedToAutomatic']


@pytest.mark.parametrize('final_result', ['registered', 'zero', 'unknown', 'promotion_failed'])
def test_retain_working_replica_if_combined_settings_unconfirmed(rig, monkeypatch, connector_clock, pin_fleet, final_result):
    # The original egress address is gone. Both transports initially fail with
    # it. By comparison time QUIC is usable again; the combined QUIC/no-bind
    # result must register before the address alone can be released.
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol, cloud.edge_bind_address = 'quic', '192.0.2.1'
    rig.cfg.save()
    queue_outage(rig, connector_clock)
    def sample(record, age):
        leg = pin_fleet.starts.index(record) + 1
        if leg == 7 and final_result == 'unknown':
            return None
        return int(age >= 5 and (leg in (4, 5) or (leg == 7 and final_result in ('registered', 'promotion_failed'))))
    pin_fleet.sample = sample
    promote = ra._promote_candidate_connector
    def switch(pid, **kwargs):
        if final_result == 'promotion_failed' and len(pin_fleet.starts) == 7:
            raise RuntimeError('injected pre-promotion failure')
        return promote(pid, **kwargs)
    monkeypatch.setattr(ra, '_promote_candidate_connector', switch)
    if final_result == 'promotion_failed':
        with pytest.raises(RuntimeError, match='injected'):
            cr._consume_pin_attempt()
    cr._consume_pin_attempt()
    live = V2Config.load()
    assert cs.value(live, 'protocol') == 'quic' and cs.value(live, 'ipFamily') == '4'
    slots = pin_fleet.slots
    assert 'candidate' not in slots and 'draining' not in slots
    state = cs.journal.load(live)
    assert not state.get('pinAttempt')
    if final_result == 'registered':
        assert cs.value(live, 'outbound') == '' and slots['active'] == pin_fleet.starts[6]
        assert [c['setting'] for c in state['returnedToAutomatic']['changes']] == ['outbound']
    else:
        assert cs.value(live, 'outbound') == '192.0.2.1' and slots['active'] == pin_fleet.starts[3]
        assert not state['returnedToAutomatic']
        assert cr.view(rig.actor)['temporaryRoute']['phase'] == 'combination_unverified'


@pytest.mark.parametrize('working,decision_age', [(True, 5), (False, 30)])
def test_comparison_stops_on_windows_only_after_decision(rig, monkeypatch, transport_outage, connector_clock, working, decision_age):
    fleet = transport_outage
    sample, discard = fleet.sample, ra._discard_candidate_connector
    stopped = []
    def counts(record, elapsed):
        if len(fleet.starts) >= 3 and record == fleet.slots.get('candidate'):
            return int(working and elapsed >= 5)
        return sample(record, elapsed)
    def stop_windows(pid, *, timeout):
        assert pid != fleet.slots['active']['pid']
        stopped.append((pid, connector_clock.monotonic()))
        return True
    def retire(pid, **kwargs):
        assert ra.runtime.stop_pid(pid, timeout=kwargs['grace_seconds'])
        discard(pid, **kwargs)
    monkeypatch.setattr(ra.runtime, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(ra.runtime, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(ra.runtime, '_terminate_process_windows', stop_windows)
    monkeypatch.setattr(ra, '_discard_candidate_connector', retire)
    fleet.sample = counts
    cr._consume_pin_attempt()
    comparison = fleet.starts[2]
    assert [when - comparison['started_at'] for pid, when in stopped if pid == comparison['pid']] == [decision_age]


@pytest.mark.parametrize('unknown_leg', range(1, 7))
def test_no_protocol_hint_unobservable_outbound_candidate_stops_recovery(rig, monkeypatch, connector_clock, unknown_leg):
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.edge_ip_version, cloud.edge_bind_address = '4', '192.0.2.1'
    rig.cfg.save()
    window = cs.PinFailureWindow(connector_clock.monotonic)
    for _ in range(13):
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=0), interval_seconds=15)
        connector_clock.sleep(15)
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    monkeypatch.setattr(ra, '_resolve_binary', lambda cfg: 'fixture-bin')
    starts, discarded = [], []
    def spawn(cfg, binary, protocol, *, pin_attempt):
        starts.append((protocol, cs.value(cfg, 'ipFamily'), cs.value(cfg, 'outbound')))
        return len(starts), 'fixture-metrics'
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(cr, '_registered', lambda url: None if len(starts) == unknown_leg else 0)
    def discard(pid, *, grace_seconds, expected_url):
        assert grace_seconds == ra.RECOVERY_DRAIN_SECONDS and expected_url == 'fixture-metrics'
        discarded.append(pid)
    monkeypatch.setattr(ra, '_discard_candidate_connector', discard)
    monkeypatch.setattr(ra, '_promote_candidate_connector', lambda *a, **kw: pytest.fail('unobservable candidate promoted'))
    start = connector_clock.monotonic()
    cr._consume_pin_attempt()
    expected = [(p, fam, out) for fam, out in [('4', '192.0.2.1'), ('4', ''), ('auto', '')] for p in ('http2', 'quic')]
    assert starts == expected[:unknown_leg]
    assert discarded == list(range(1, unknown_leg + 1))
    assert connector_clock.monotonic() - start == 30 * unknown_leg
    current = V2Config.load()
    assert cs.value(current, 'outbound') == '192.0.2.1' and not cs.journal.inspect(current)['returnedToAutomatic']
    assert not ra._stored_preferred_protocol()


@pytest.mark.parametrize('usable_leg', [1, 2, 3, 4, 5, 6, None])
def test_registration_search_tries_saved_family_before_releasing_it(rig, monkeypatch, connector_clock, usable_leg):
    """An expired outbound pin must not hide a working IPv4-only route."""
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol, cloud.edge_bind_address = 'quic', '192.0.2.1'
    rig.cfg.save()
    window = cs.PinFailureWindow(connector_clock.monotonic)
    for _ in range(13):
        cr.observe_pins(window, rig.cfg, active=rig.active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=0), interval_seconds=15)
        connector_clock.sleep(15)
    attempts = []
    class FirstPromotion(Exception):
        pass
    candidate_slot = {}
    monkeypatch.setattr(ra, '_state_connector', lambda slot: candidate_slot if slot == 'candidate' else (rig.active if slot == 'active' else None))
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: pid in (rig.active['pid'], candidate_slot.get('pid')))
    def spawn(cfg, binary, protocol, *, pin_attempt):
        attempts.append((protocol, cs.value(cfg, 'ipFamily'), cs.value(cfg, 'outbound'), connector_clock.monotonic()))
        candidate_slot.update(pid=200 + len(attempts), started_at=connector_clock.monotonic(), metrics_url='fixture-metrics')
        return candidate_slot['pid'], candidate_slot['metrics_url']
    def count(url):
        assert cs.value(V2Config.load(), 'outbound') == '192.0.2.1'
        return int(len(attempts) == usable_leg and connector_clock.monotonic() - attempts[-1][-1] >= 6.379)
    def promote(*args, **kwargs):
        raise FirstPromotion  # Stop exactly at the first promotion, before any attribution work.
    monkeypatch.setattr(ra, '_resolve_binary', lambda config: 'fixture-bin')
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(ra, '_discard_candidate_connector', lambda *a, **kw: None)
    monkeypatch.setattr(cr, '_registered', count)
    monkeypatch.setattr(ra, '_promote_candidate_connector', promote)
    start = connector_clock.monotonic()
    with (pytest.raises(FirstPromotion) if usable_leg else nullcontext()):
        cr._consume_pin_attempt()
    routes = [(p, f, out) for f, out in [('4', '192.0.2.1'), ('4', ''), ('auto', '')] for p in ('quic', 'http2')]
    expected = routes[:usable_leg]
    assert [row[:3] for row in attempts] == expected
    assert [row[3] - start for row in attempts] == [30 * i for i in range(len(expected))]
    assert connector_clock.monotonic() - start == (30 * (usable_leg - 1) + 7 if usable_leg else 180)
    assert cs.value(V2Config.load(), 'ipFamily') == '4'
    assert not cs.journal.inspect(V2Config.load())['returnedToAutomatic']


@pytest.mark.parametrize('comparison', ['missing', 'registered', 'failed'])
def test_transport_rescue_requires_online_comparison_before_pin_write(rig, monkeypatch, connector_clock, comparison):
    """A live HTTP/2 rescue alone never justifies dropping QUIC, IPv4, or egress."""
    cloud = rig.cfg.remote_access.vibe_cloud
    cloud.transport_protocol, cloud.edge_bind_address = 'quic', '192.0.2.1'
    rig.cfg.save()
    active, slots, starts, retired = dict(rig.active), {}, [], []
    slots['active'] = active
    def spawn(config, binary, protocol, *, pin_attempt):
        pid = 300 + len(starts)
        rec = {'pid': pid, 'started_at': connector_clock.monotonic(), 'metrics_url': f'fixture-{pid}',
               'requested_protocol': protocol}
        slots['candidate'] = rec
        starts.append((rec, cs.value(config, 'ipFamily'), cs.value(config, 'outbound')))
        return pid, rec['metrics_url']
    def counts(url):
        if url == slots['active']['metrics_url']:
            return int(slots['active']['pid'] != active['pid'])
        rec = slots['candidate']
        assert url == rec['metrics_url']
        if len(starts) == 3 and comparison == 'missing':
            return None
        works = rec['requested_protocol'] == 'http2' or len(starts) >= 3 and comparison == 'registered'
        return int(works and connector_clock.monotonic() - rec['started_at'] >= 6.379)
    def promote(pid, **kwargs):
        old, candidate = slots['active'], slots.pop('candidate')
        assert pid == candidate['pid']
        if len(starts) == 2:
            assert (cs.value(V2Config.load(), 'protocol'), cs.value(V2Config.load(), 'ipFamily')) == ('quic', '4')
        slots['active'] = candidate
        slots['draining'] = old
        return old
    def discard(pid, **kwargs):
        assert pid == slots['candidate']['pid'] and pid != slots['active']['pid']
        retired.append(pid)
        slots.pop('candidate')
    monkeypatch.setattr(ra, '_PREFERRED_PROTOCOL', None)
    monkeypatch.setattr(ra, '_state_connector', slots.get)
    monkeypatch.setattr(ra, '_read_pid', lambda: slots['active']['pid'])
    monkeypatch.setattr(ra, '_is_cloudflared_pid', lambda pid: any(s['pid'] == pid for s in slots.values()))
    monkeypatch.setattr(ra, '_resolve_binary', lambda config: 'fixture-bin')
    monkeypatch.setattr(ra, '_start_candidate_connector', spawn)
    monkeypatch.setattr(ra, '_promote_candidate_connector', promote)
    monkeypatch.setattr(ra, '_drain_tracked_connector', lambda old: bool(slots.pop('draining', None)))
    monkeypatch.setattr(ra, '_discard_candidate_connector', discard)
    monkeypatch.setattr(cr, '_registered', counts)
    window = cs.PinFailureWindow(connector_clock.monotonic)
    for _ in range(13):
        cr.observe_pins(window, rig.cfg, active=active, pid_state='cloudflared',
                        sample=SimpleNamespace(registered_connections=0), interval_seconds=15)
        connector_clock.sleep(15)
    cr._consume_pin_attempt()
    assert [row[0]['requested_protocol'] for row in starts[:3]] == ['quic', 'http2', 'quic']
    current = V2Config.load()
    assert all(row[1:] == ('4', '192.0.2.1') for row in starts)
    assert (cs.value(current, 'ipFamily'), cs.value(current, 'outbound')) == ('4', '192.0.2.1')
    record = cs.journal.inspect(current)['returnedToAutomatic']
    if comparison == 'failed':
        assert cs.value(current, 'protocol') == 'auto' and [change['setting'] for change in record['changes']] == ['protocol']
    else:
        assert cs.value(current, 'protocol') == 'quic' and not record
    assert slots['active']['requested_protocol'] == ('quic' if comparison == 'registered' else 'http2')


@pytest.fixture
def connector_clock(rig, monkeypatch):
    def advance(seconds):
        rig.clock[0] += seconds
    clock = SimpleNamespace(monotonic=cs.journal.clock, sleep=advance)
    for module in (cr, ra, ra.runtime):
        monkeypatch.setattr(module, 'time', clock)
    return clock


@pytest.mark.parametrize('mode,transition,expected', [
    ('registration', 6.379, True), ('registration', 29, True), ('registration', 30.01, False),
    ('full_ha', 7, True), ('full_ha', 8.01, False),
])
def test_connector_readiness_poll_budget(rig, monkeypatch, connector_clock, mode, transition, expected):
    # One edge registration is sufficient for pin recovery. Startup instead
    # waits for four; do not equate the quick tunnel's first registration with HA.
    start = connector_clock.monotonic()
    def counts(*args):
        return int(connector_clock.monotonic() - start >= transition)
    if mode == 'registration':
        monkeypatch.setattr(cr, '_registered', counts)
        result = cr._wait_registered(rig.active['pid'], 'fixture')
        budget = 30
    else:
        def metrics(url):
            return SimpleNamespace(ready=True, ha_connections=3 + counts())
        monkeypatch.setattr(ra.tunnel_quality, 'scrape_metrics', metrics)
        result = ra._wait_connector_ready(rig.active['pid'], 'fixture', timeout_seconds=ra.START_PREFERRED_READY_TIMEOUT_SECONDS)
        budget = 8
    assert result is expected
    if not expected:
        assert connector_clock.monotonic() - start == budget


@pytest.mark.parametrize('duration,expected', [(3.607, True), (4.002, False)])
def test_public_probe_three_attempts_share_one_deadline(rig, monkeypatch, connector_clock, duration, expected):
    # Local Incus HTTP origin: the third reply completed at 10.821 s; a
    # different run reached 12.006 s. These aren't Internet latency samples.
    timeouts = []
    def observe(client, url, *, attempts, timeout):
        assert url == 'https://test.invalid/health' and attempts == 1
        timeouts.append(timeout)
        connector_clock.sleep(duration)
        return SimpleNamespace(successes=[len(timeouts) == 3])
    monkeypatch.setattr(ra, '_public_probe_session', lambda: nullcontext(None))
    monkeypatch.setattr(ra.tunnel_quality, 'probe_request_path', observe)
    assert cr._public_check('https://test.invalid/health') is expected
    assert len(timeouts) == 3 and all(0 < n <= 4 for n in timeouts)


@pytest.mark.parametrize('term_exit,escalates', [(0.376, False), (34.6, False), (35.4, True)])
def test_old_connector_grace_before_escalation(rig, monkeypatch, connector_clock, term_exit, escalates):
    # Exercise the actual POSIX drain loop, but intercept *every* OS action.
    # A 35 s grace bounds TERM, not the whole switchover or even its KILL wait.
    runtime = ra.runtime
    pid = rig.active['pid']
    signals = []
    start = connector_clock.monotonic()
    def send(target, signum):
        assert target == pid
        signals.append((signum, connector_clock.monotonic() - start))
    def alive(target):
        assert target == pid
        return connector_clock.monotonic() - start < term_exit and not any(s == runtime.signal.SIGKILL for s, _ in signals)
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(name='posix', kill=send))
    monkeypatch.setattr(runtime, 'pid_alive', alive)
    monkeypatch.setattr(runtime, 'get_process_command', lambda target: 'fixture-connector')
    monkeypatch.setattr(runtime, 'write_shutdown_intent', lambda *args, **kwargs: None)
    monkeypatch.setattr(ra, '_read_state', lambda: {})
    assert ra._drain_tracked_connector({'pid': pid})
    assert [s for s, _ in signals] == [runtime.signal.SIGTERM] + ([runtime.signal.SIGKILL] if escalates else [])
    if escalates:
        assert 35 <= signals[-1][1] < 35.21


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
