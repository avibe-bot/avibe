"""Connection-method settings: the browser submits once; the instance owns verification."""
from __future__ import annotations

import copy
import logging
import secrets
import threading
import time
from contextlib import contextmanager

from config import paths
from config.v2_config import V2Config, config_file_lock
from core.direct_transport_runtime import DirectTransportRuntimeManager
from vibe import connection_settings as cs, direct_lifecycle, remote_access as tunnel
from vibe.direct_rendezvous import rendezvous

log = logging.getLogger(__name__)
_probe = None
_probe_window = None  # pairing, connector identity, first observation time, waiting for first result
_worker_lock = threading.Lock()
_worker = None
_wake = threading.Event()
_probe_slot = threading.Lock()


def delegated_direct_change(authority, enabled):
    """CLI-only. IPC failure is not permission to fall back to an offline write."""
    import requests
    from vibe import runtime

    def admit(config):
        if (not authority.is_instance_owner or (authority.is_remote and not authority.instance_id)
                or (authority.instance_id and authority.instance_id != config.remote_access.vibe_cloud.instance_id)):
            raise cs.ConnectionError('connection_owner_required', 403)

    with runtime.ui_start_exclusion(timeout_seconds=3), config_file_lock():
        config = V2Config.load()
        admit(config)
        if runtime.recorded_ui_is_gone():
            if config.remote_access.vibe_cloud.direct != enabled:
                cs.journal.supersede(config, {'direct'})
                cs.journal.begin(config, 'direct', enabled)
                config.remote_access.vibe_cloud.direct = enabled
                config.save()
                cs.journal.finish(cs.journal.load(config), 'applied', 'delegated_cli')
            return {'ok': True, 'accepted': False}
        expected = cs.binding(config)
        origin = tunnel.origin_service_for_pairing(config)
    with requests.Session() as client:
        client.trust_env = False
        token = client.get(origin + '/api/csrf-token', timeout=(1, 3), allow_redirects=False)
        if token.status_code != 200:
            raise cs.ConnectionError('connection_state_unavailable', 503)
        response = client.patch(origin + '/api/devices/connection', json={'setting': 'direct', 'to': enabled},
                                headers={'Origin': origin, 'X-Vibe-CSRF-Token': token.json()['csrf_token'],
                                         'X-Vibe-Delegated-Change': '1', 'X-Vibe-Expected-Binding': expected},
                                timeout=(1, 5), allow_redirects=False)
        if response.status_code not in (200, 202):
            raise cs.ConnectionError(response.json().get('error', 'connection_state_unavailable'), response.status_code)
        return {'ok': True, 'accepted': response.status_code == 202}


def _active_id(active):
    return active.get('pid'), active.get('started_at'), active.get('metrics_url')


def observe_public_path(config, active, allowed, sample, began):
    global _probe, _probe_window
    now = cs.journal.clock()
    identity = (cs.relay_pairing(config), _active_id(active)) if config and active.get('pid') else None
    if not identity:
        _probe_window = None
    elif _probe_window is None or _probe_window[0] != identity or (not allowed and not _probe_window[2]):
        # Start the wait once per change or pause, not on every paused tick.
        _probe_window = (identity, now, True)
    if not allowed or not config:
        _probe = None
    elif sample is not None and identity:
        _probe_window = (identity, now, False)
        _probe = (*identity, began) if any(sample.successes) else None


def _waiting_for_connector_sample(config):
    window = _probe_window
    active = tunnel._state_connector('active') or {}
    if not window or not _requested(config) or not tunnel._is_cloudflared_pid(active.get('pid')):
        return False
    pair = cs.relay_pairing(config)
    return (window[0][0] == pair and 0 <= cs.journal.clock() - window[1] <= 30
            and (window[0][1] != _active_id(active) or window[2]))


def tunnel_healthy(config):
    probe = _probe
    active = tunnel._state_connector('active') or {}
    return bool(probe and config.remote_access.vibe_cloud.enabled and tunnel._connector_requested_path().is_file()
                and probe[:2] == (cs.relay_pairing(config), _active_id(active)) and 0 <= cs.journal.clock() - probe[2] <= 60
                and tunnel._is_cloudflared_pid(active.get('pid')))


def _bound(actor, config):
    if cs.binding(config) != actor.binding:
        raise cs.ConnectionError('connection_binding_changed', 403)


def relay_unavailable(config):
    if not cs.relay_enrolled(config):
        return 'relay_not_enrolled'
    # While direct is disabled this is only a preference. Actual publication
    # always goes through the normal permission and verified binary gates.
    if config.remote_access.vibe_cloud.direct:
        owner = direct_lifecycle.active_lifecycle()
        if not owner or owner.applied_generation(config) is None or not owner.status().direct_access_allowed:
            return 'direct_permission_required'
        if not DirectTransportRuntimeManager().resolve_binary(require_public_trial=True):
            return 'relay_transport_unavailable'
    return None


def view(actor):
    with config_file_lock():
        config = V2Config.load()
        _bound(actor, config)
        result = cs.journal.inspect(config)
        live = direct_lifecycle.status_for_config(config)
        cloud = config.remote_access.vibe_cloud
        direct = 'not-running'
        if not cloud.direct:
            direct = 'off'
        elif live['state'] == 'running':
            direct = 'ready'
        elif live['state'] == 'waiting-permission':
            direct = 'denied' if live['reason'] == 'permission_denied' else 'awaiting-permission'
        elif live['state'] == 'waiting-certificate':
            direct = 'awaiting-certificate'
        reason = relay_unavailable(config)
        result.update(settings=tunnel._remote_access_settings(config), direct=direct)
        if reason != 'relay_not_enrolled':
            result['relay'] = dict(enabled=cloud.public_relay_trial,
                                   connected=bool(cloud.public_relay_trial and direct == 'ready' and rendezvous.relay_connected()),
                                   canEnable=reason is None, unavailableReason=reason, ownerOnlyDirectWhenEnabled=True)
        else:
            for k in ('outcome', 'operation'):
                if result[k] and result[k]['setting'] == 'relay':
                    result[k] = None
        return result


def _switch_gate(actor, config, key, new):
    if key == 'relay' and not cs.relay_enrolled(config):
        raise cs.ConnectionError('relay_not_enrolled', 403)
    if not actor.local and config.remote_access.vibe_cloud.direct and (key == 'relay' or (key == 'direct' and not new)):
        if tunnel._connector_lane_reserved_locked() or cs.journal.owns_connector_lane() or _waiting_for_connector_sample(config):
            raise cs.ConnectionError('connection_change_busy')
        if not tunnel_healthy(config):
            raise cs.ConnectionError('tunnel_unhealthy')
    if key == 'relay' and new and (reason := relay_unavailable(config)):
        raise cs.ConnectionError(reason)


@contextmanager
def _admission_lane():
    try:
        with tunnel._connector_lifecycle(timeout=0):
            yield
    except tunnel._ConnectorLifecycleBusy as exc:
        raise cs.ConnectionError('connection_change_busy') from exc


def prepare(actor, key, new):
    """Reserve before replying. No switch write or connector retirement in this path."""
    with _admission_lane(), tunnel._RECOVERY_LOCK, tunnel._CONNECTOR_LOCK, config_file_lock():
        config = V2Config.load()
        _bound(actor, config)
        if key == 'relay' and not cs.relay_enrolled(config):
            raise cs.ConnectionError('relay_not_enrolled', 403)
        state = cs.journal.load(config)
        cs.journal.guard_restore_target(config, state, {cs.FIELDS[key]})
        if cs.value(config, key) == new:
            old = state['operation'] or {}
            if old.get('setting') == key and old['state'] != 'restoring':
                raise cs.ConnectionError('connection_change_busy')
            return None  # reaffirming a value does not cancel an in-flight pin retry
        if key in cs.SWITCHES:
            _switch_gate(actor, config, key, new)
        if key in cs.AUTOMATIC:
            if tunnel._connector_lane_reserved_locked():
                raise cs.ConnectionError('connection_change_busy')
            pid_state = tunnel._cloudflared_pid_state(tunnel._read_pid())
            if pid_state == 'unknown':
                raise cs.ConnectionError('connection_state_unavailable', 503)
            if key in {'ipFamily', 'outbound'}:
                prospective = copy.deepcopy(config)
                setattr(prospective.remote_access.vibe_cloud, cs.FIELDS[key], new)
                if tunnel.edge_binding_error(prospective):
                    raise cs.ConnectionError('invalid_connection_change', 400)
            stopped = pid_state in {'dead', 'other'}
        else:
            stopped = False
        if key not in cs.SWITCHES and (key not in cs.AUTOMATIC or stopped):
            cs.journal.supersede(config, {cs.FIELDS[key]})
            if stopped:
                cs.journal.begin(config, key, new)
                state = cs.journal.load(config)
                state['operation']['unverifiedSave'] = True
                cs.journal.save(state)
            setattr(config.remote_access.vibe_cloud, cs.FIELDS[key], new)
            config.save()
            if stopped:
                cs.journal.finish(state, 'applied', 'saved_without_connector')
            return None
        op = cs.journal.begin(config, key, new, baseline=tunnel_healthy(config))
        _wake.set()
        return op


def _public_check(url):
    """Bound DNS as well as HTTP. A wedged resolver keeps one slot, not an unbounded queue."""
    if not url or not _probe_slot.acquire(False):
        return False
    deadline = cs.journal.clock() + 12
    done = threading.Event()
    passed = []

    def probe():
        try:
            with tunnel._public_probe_session() as client:
                for _ in range(3):
                    remaining = deadline - cs.journal.clock()
                    if remaining <= 0:
                        break
                    sample = tunnel.tunnel_quality.probe_request_path(client, url, attempts=1, timeout=min(4, remaining))
                    if any(sample.successes):
                        passed.append(cs.journal.clock())
                        break
        finally:
            done.set()
            _probe_slot.release()
    threading.Thread(target=probe, daemon=True, name='connection-settings-probe').start()
    done.wait(max(0, deadline - cs.journal.clock()))
    return bool(passed and passed[0] < deadline and cs.journal.clock() <= deadline)


def _sole_generation(op):
    return bool(op.get('generation') and op['generation'] == tunnel.connector_generation()
                and not tunnel._state_connector('candidate') and not tunnel._state_connector('draining')
                and tunnel._connector_requested_path().is_file())


def apply(actor, reservation):
    key, new, op_id = (reservation[k] for k in ('setting', 'to', 'id'))
    try:
        if key in cs.SWITCHES:
            if not actor.local:
                # The ASGI body was sent; give the old direct stream a chance
                # to carry it before retiring the endpoint. No locks held here.
                time.sleep(1.0)
            with tunnel._connector_lifecycle(), tunnel._RECOVERY_LOCK, tunnel._CONNECTOR_LOCK, config_file_lock():
                config = V2Config.load()
                _bound(actor, config)
                cs.journal.applying(config, op_id)
                _switch_gate(actor, config, key, new)
                setattr(config.remote_access.vibe_cloud, cs.FIELDS[key], new)
                config.save()
                cs.journal.finish(cs.journal.load(config), 'applied', 'saved')
                direct_lifecycle.notify_config_changed(config)
            return
        result = tunnel.apply_settings({cs.FIELDS[key]: new}, operation_id=op_id, expected_binding=actor.binding)
        if not result.get('ok'):
            reason = ('candidate_not_ready' if result.get('detail') == 'candidate_not_ready'
                      else 'connection_change_busy' if result.get('error') == 'remote_access_settings_unavailable'
                      else 'settings_not_applied')
            raise cs.ConnectionError(reason)
        if not result.get('connector_replaced'):
            return  # connector stopped before apply; saved in the writer transaction
        with config_file_lock():
            config = V2Config.load()
            op = cs.journal.owned(cs.journal.load(config), op_id, 'verifying')
            url = tunnel._public_health_url(config)
        ok = result.get('previous_connector_drained') and _sole_generation(op) and _public_check(url)
        with tunnel._connector_lifecycle(), config_file_lock():
            config = V2Config.load()
            state = cs.journal.load(config)
            op = cs.journal.owned(state, op_id, 'verifying')
            if ok and _sole_generation(op) and cs.value(config, key) == new and cs.journal.clock() < op['until']:
                cs.journal.finish(state, 'applied', 'verified')
            else:
                cs.journal.restore(state, _failure(op))
    except Exception as exc:
        with config_file_lock():
            config = V2Config.load()
            state = cs.journal.load(config)
            op = state['operation']
            if op and op['id'] == op_id and op['state'] != 'restoring':
                if key in cs.SWITCHES:
                    saved = cs.value(config, key) == new
                    cs.journal.finish(state, 'applied' if saved else 'failed', 'saved' if saved else getattr(exc, 'code', 'apply_failed'))
                elif op['state'] == 'applying' and not op.get('writeStarted') and cs.value(config, key) == op['from']:
                    # No saved change or promotion: there is nothing to restore.
                    cs.journal.finish(state, 'failed', getattr(exc, 'code', 'settings_not_applied'))
                else:
                    cs.journal.restore(state, _failure(op))
    finally:
        _wake.set()


def _failure(op):
    return 'verification_failed' if op['baseline'] else 'verification_unavailable'


def _repair_storage(config, op):
    key = op['setting']
    actual = cs.value(config, key)
    if actual == op['from']:
        return True
    if actual != op['to']:
        return False
    setattr(config.remote_access.vibe_cloud, cs.FIELDS[key], op['from'])
    config.save()
    return True


def _requested(config):
    return config.remote_access.vibe_cloud.enabled and tunnel._connector_requested_path().is_file()


def _repair_runtime(config, op, at_bound):
    if not _requested(config):
        return True
    with tunnel._RECOVERY_LOCK:
        if tunnel._connector_lane_reserved_locked():
            return False
    pid = tunnel._read_pid()
    pid_state = tunnel._cloudflared_pid_state(pid)
    binary = tunnel._resolve_binary(config)
    if not binary or pid_state == 'unknown':
        return False
    if pid_state == 'cloudflared':
        if tunnel._running_signature(pid) == tunnel._runtime_signature(config, binary):
            active = tunnel._state_connector('active') or {}
            return active.get('pid') == pid and (_registered(active.get('metrics_url')) or 0) > 0
        if not op.get('generation') or op['generation'] != tunnel.connector_generation():
            return False  # never retire a connector we cannot attribute to this change
        if at_bound:
            if op.get('generation') and op['generation'] == tunnel.connector_generation():
                tunnel.runtime.stop_pid(pid, timeout=8)  # leave request in place for exited-connector recovery
            return False
    elif at_bound:
        return False
    tunnel._start(config, record_request=False)
    return False  # next observed registration, not spawn success, completes recovery


def _registered(url):
    if not url:
        return None
    try:
        return tunnel.tunnel_quality.scrape_metrics(url).registered_connections
    except Exception:
        return None


def recover(*, startup=False):
    if not cs.journal.path.exists():
        return True
    with tunnel._connector_lifecycle(timeout=0):
        with config_file_lock():
            config = V2Config.load()
            state = cs.journal.load(config)
            # The foreground request still gets a bounded outcome if reconciliation
            # is busy. Startup storage repair itself does not need the connector lock.
            if state.get('pinAttempt', {}).get('boot', cs.journal.boot) != cs.journal.boot:
                state.pop('pinAttempt')
                cs.journal.save(state)
            op = state['operation']
            if op and op['state'] != 'restoring':
                if op['boot'] == cs.journal.boot and cs.journal.clock() < op['until']:
                    return False
                if op['setting'] in cs.SWITCHES or op.get('unverifiedSave'):
                    saved = cs.value(config, op['setting']) == op['to']
                    cs.journal.finish(state, 'applied' if saved else 'failed', 'saved' if saved else 'interrupted')
                    return True
                cs.journal.restore(state, 'restart' if startup else _failure(op))
            debt = not op
            op = op or state.get('debt')
            if not op:
                return True
            at_bound = debt or op['boot'] != cs.journal.boot or cs.journal.clock() >= op['until']
            try:
                saved = _repair_storage(config, op)
            except (OSError, ValueError):
                if at_bound and not debt:
                    state['debt'] = op
                    cs.journal.finish(state, 'failed', 'storage_restore_failed')
                return False
            if not saved:
                cs.journal.supersede(config, {cs.FIELDS[op['setting']]})
                return True
        if startup:
            return False  # no network I/O before startup reconciliation
        try:
            success = _repair_runtime(config, op, at_bound)
        except (OSError, RuntimeError, tunnel._ConnectorLifecycleBusy):
            success = False
        with config_file_lock():
            live = V2Config.load()
            state = cs.journal.load(live)
            current = state.get('debt' if debt else 'operation') or {}
            if current.get('id') != op['id'] or cs.value(live, op['setting']) != op['from']:
                return True
            if debt and success:
                state.pop('debt')
                if state['outcome'] and state['outcome']['id'] == op['id']:
                    state['outcome'].update(result='restored', reason=op['reason'], at=cs.now())
                cs.journal.save(state)
            elif not debt and success:
                cs.journal.finish(state, 'restored', op['reason'])
            elif not debt and at_bound:
                state['debt'] = op
                cs.journal.finish(state, 'failed', 'saved_restored_runtime_pending')
            return success


def recover_startup():
    # The admission lock might be held by a separate CLI. Still start the
    # recovery worker and ordinary monitor; their next tick rechecks intent.
    try:
        recover(startup=True)
    except tunnel._ConnectorLifecycleBusy:
        _wake.set()

def _request_identity():
    try:
        st = tunnel._connector_requested_path().stat()
        return [st.st_dev, st.st_ino, st.st_mtime_ns]
    except OSError:
        return None


def observe_pins(window, config, *, active, pid_state, sample, max_gap):
    pins = {k: cs.value(config, k) for k, auto in cs.AUTOMATIC.items() if cs.value(config, k) != auto} if config and _requested(config) else {}
    request = _request_identity() if pins else None
    identity = (cs.relay_pairing(config), tuple(pins.items()), request) if request else None
    down = pid_state in {'dead', 'other'} or (pid_state == 'cloudflared' and sample is not None and sample.registered_connections == 0)
    if not window.observe(identity, disconnected=down and not tunnel._state_connector('candidate') and not tunnel._state_connector('draining'), max_gap=max_gap):
        return
    with tunnel._connector_lifecycle(timeout=0), tunnel._RECOVERY_LOCK, config_file_lock():
        live = V2Config.load()
        if (not _requested(live) or cs.relay_pairing(live) != identity[0] or _request_identity() != request
                or not _match_pins(live, pins) or tunnel._connector_lane_reserved_locked()
                or _active_id(tunnel._state_connector('active') or {}) != _active_id(active)):
            return
        state = cs.journal.load(live)
        if state['operation'] or state.get('pinAttempt'):
            return
        state['pinAttempt'] = dict(id=secrets.token_hex(16), pins=pins, request=request, boot=cs.journal.boot)
        cs.journal.save(state)
        window.identity = None
        _wake.set()


def _match_pins(config, pins):
    return all(cs.value(config, k) == pins.get(k, auto) for k, auto in cs.AUTOMATIC.items())


def _pin_live(attempt):
    config = V2Config.load()
    state = cs.journal.load(config)
    if (state.get('pinAttempt') != attempt or not _requested(config) or _request_identity() != attempt['request']
            or not _match_pins(config, attempt['pins']) or attempt['boot'] != cs.journal.boot):
        raise cs.ConnectionError('connection_change_superseded')
    return config, state


def _wait_registered(pid, url):
    deadline = cs.journal.clock() + tunnel.RECOVERY_READY_TIMEOUT_SECONDS
    last = None
    while cs.journal.clock() < deadline:
        process = tunnel._cloudflared_pid_state(pid)
        if process in {'dead', 'other'}:
            return False
        if process != 'cloudflared':
            return None
        last = _registered(url)
        if last and last > 0:
            return True
        time.sleep(1)
    return False if last == 0 else None


def _pin_candidates(attempt):
    """Failure of saved pins immediately precedes auto; a blind outage never writes auto."""
    for automatic in (False, True):
        pid = None
        try:
            with tunnel._connector_lifecycle(), tunnel._RECOVERY_LOCK, config_file_lock():
                config, _ = _pin_live(attempt)
                binary = tunnel._resolve_binary(config)
                if not binary or tunnel._connector_lane_reserved_locked():
                    return
                trial = copy.deepcopy(config)
                if automatic:
                    for key in attempt['pins']:
                        setattr(trial.remote_access.vibe_cloud, cs.FIELDS[key], cs.AUTOMATIC[key])
                pid, url = tunnel._start_candidate_connector(trial, binary, 'auto' if automatic else tunnel._configured_protocol(config))
            ready = _wait_registered(pid, url)
            if ready is not True:
                if ready is None:
                    return
                continue
            with tunnel._connector_lifecycle(), tunnel._CONNECTOR_LOCK:
                with config_file_lock():
                    live, state = _pin_live(attempt)
                    if tunnel._resolve_binary(live) != binary or tunnel._runtime_signature(config, binary) != tunnel._runtime_signature(live, binary):
                        return
                replaced = tunnel._promote_candidate_connector(pid, runtime_signature=tunnel._runtime_signature(trial, binary), registered_only=True)
                pid = None
                if automatic:
                    with config_file_lock():
                        live, state = _pin_live(attempt)
                        changes = []
                        for key, old in attempt['pins'].items():
                            setattr(live.remote_access.vibe_cloud, cs.FIELDS[key], cs.AUTOMATIC[key])
                            changes.append(dict(setting=key, **{'from': old, 'to': cs.AUTOMATIC[key]}))
                        state['returnedToAutomatic'] = dict(at=cs.now(), reason='tunnel_unreachable', changes=changes)
                        live.save()
                        state.pop('pinAttempt')
                        cs.journal.save(state)
            tunnel._drain_tracked_connector(replaced)
            return
        finally:
            if pid is not None:
                tunnel._discard_candidate_connector(pid)


def _consume_pin_attempt():
    if not cs.journal.path.exists():
        return
    with config_file_lock():
        attempt = cs.journal.load(V2Config.load()).get('pinAttempt')
    if not attempt:
        return
    try:
        _pin_candidates(attempt)
    finally:
        with config_file_lock():
            state = cs.journal.load(V2Config.load())
            if state.get('pinAttempt') == attempt:
                state.pop('pinAttempt')
                cs.journal.save(state)


def start():
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        home = paths.get_state_dir()
        def run():
            backoff = 1
            while paths.get_state_dir() == home:
                _wake.clear()
                try:
                    _consume_pin_attempt()
                    idle = recover()
                except Exception:
                    idle = False
                    log.debug('Connection recovery deferred', exc_info=True)
                backoff = 1 if idle else min(10, backoff * 2)
                _wake.wait(30 if idle else backoff)
        _worker = threading.Thread(target=run, name='connection-settings-recovery', daemon=True)
        _worker.start()
