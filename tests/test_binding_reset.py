"""Exercise local-only reset recovery in an isolated data root; no connector or account."""
import pytest

from config.v2_config import V2Config, config_file_lock
from tests.test_pairing_recovery import pairing_host as pairing_host
from vibe import binding_admission as gate, binding_reset as reset, desktop_local_instance as local, remote_access as remote
from vibe.personal_claim_client import ClaimError


@pytest.fixture
def reset_host(pairing_host, monkeypatch):
    root, calls = pairing_host
    assert remote.pair('key_A', 'https://backend.test')['ok']
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.enabled = False
    cfg.save()
    local.ensure_installation_id()
    workspace = root / 'untouched-workspace'
    workspace.write_text('keep')
    monkeypatch.setattr(remote, 'stop', lambda _: {'ok': True})
    monkeypatch.setattr(remote, '_stop_direct_lifecycle_before_pairing_discard', lambda _: None)
    from vibe import direct_certificates
    monkeypatch.setattr(direct_certificates, 'discard_direct_certificates', lambda: None)
    with gate.admitted('reset'), config_file_lock():
        record = reset._seal(V2Config.load())
    return record, workspace, calls


@pytest.mark.parametrize('phase', reset._PHASES[:-1])
def test_restart_after_effect_before_phase_write(reset_host, phase, monkeypatch):
    record, workspace, calls = reset_host
    original = reset._save
    failed = False

    def interrupt(intent):
        nonlocal failed
        if not failed and intent['phase'] == reset._PHASES[reset._PHASES.index(phase) + 1]:
            failed = True
            raise OSError('fixture crash')
        original(intent)

    with monkeypatch.context() as patch:
        patch.setattr(reset, '_save', interrupt)
        with pytest.raises(OSError, match='fixture crash'):
            reset.resume()
    assert failed and gate.reset_in_progress() and reset._read()['phase'] == phase
    assert remote.pair('key_B', 'https://backend.test')['error'] == 'operation_in_progress'
    assert reset.resume() == {'state': 'unpaired'}
    cloud = V2Config.load().remote_access.vibe_cloud
    assert not any((cloud.enabled, cloud.instance_secret, cloud.instance_id, cloud.session_secret, cloud.direct))
    assert not gate.reset_in_progress() and local._installation_id() is None
    assert workspace.read_text() == 'keep' and len(calls) == 1


@pytest.mark.parametrize('damage', ['truncated', 'invalid-stage', 'replaced-identity', 'new-pairing'])
def test_worker_keeps_damaged_or_stale_intent_fenced(reset_host, damage):
    record, workspace, calls = reset_host
    if damage == 'truncated':
        gate.reset_intent_path().write_text('{')
    elif damage == 'invalid-stage':
        reset._save({**record, 'phase': 'unknown'})
    elif damage == 'replaced-identity':
        local._identity_path().unlink()
        local.ensure_installation_id()
    else:
        remote._pending_pairing_path().write_text('new pending record')
    before = local._identity_path().read_bytes()
    assert remote._drive_pending_pairing() == (True, 60)
    assert gate.reset_in_progress() and local._identity_path().read_bytes() == before
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
    assert workspace.read_text() == 'keep' and len(calls) == 1


def test_unbind_protocol_conflict_stays_fenced_across_worker_restart(reset_host):
    from vibe import binding_recovery
    from vibe.binding_unbind import root_stamp

    record, workspace, calls = reset_host
    operation = '52932f11-9742-49e5-94ae-ad97afcf966b'
    reset._save({**record, 'schema': 2, 'operation': operation, 'phase': 'repair',
                 'accepted': True, 'was_requested': False, 'root': root_stamp()})
    for _ in range(2):
        view = binding_recovery._resume_reset()
        assert view == {'state': 'reset_repair_required', 'reason_code': 'unbind_requires_attention'}
        assert reset._read()['operation'] == operation
    assert gate.reset_in_progress() and local._installation_id() is not None
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
    assert workspace.read_text() == 'keep' and len(calls) == 1


@pytest.mark.parametrize('responses,final', [
    (('retry', 'pending', 'unbound'), 'unpaired'),
    (('pending', 'repair'), 'reset_repair_required'),
])
def test_hosted_worker_resumes_only_the_sealed_operation(reset_host, monkeypatch, responses, final):
    from vibe import binding_recovery, binding_unbind as unbind

    record, workspace, _ = reset_host
    op = '292784aa-366b-46fb-a109-bc646a25f6ce'
    reset._save({**record, 'schema': 2, 'operation': op, 'phase': 'cloud',
                 'accepted': False, 'was_requested': False, 'root': unbind.root_stamp()})
    observed = []
    def send(config, operation, *, accepted):
        observed.append((operation, config.remote_access.vibe_cloud.instance_id, accepted))
        return responses[len(observed) - 1]
    monkeypatch.setattr(unbind, 'send_original', send)
    for index, response in enumerate(responses):
        view = binding_recovery._resume_reset()
        if index < len(responses) - 1:
            assert view['state'] == 'unbinding'
            assert reset._read()['accepted'] is (response == 'pending')
            assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
    assert view['state'] == final
    assert [item[:2] for item in observed] == [(op, 'inst_A')] * len(responses)
    assert observed[-1][2] is True
    assert gate.reset_in_progress() is (final != 'unpaired')
    assert workspace.read_text() == 'keep'


@pytest.mark.parametrize('switch', ['start', 'stop'])
def test_user_switch_cannot_modify_intent_while_reset_owns_it(tmp_path, monkeypatch, switch):
    monkeypatch.setenv('AVIBE_HOME', str(tmp_path))
    path = gate.reset_intent_path()
    request = remote._connector_requested_path()
    assert path.is_relative_to(tmp_path) and request.is_relative_to(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('sealed')  # Even an unreadable marker must keep switch writers out.
    request.parent.mkdir(parents=True, exist_ok=True)
    request.touch()
    called = []
    monkeypatch.setattr(remote, '_ensure_cloudflared_pin', lambda _: None)
    monkeypatch.setattr(remote, '_start', lambda *a, **kw: called.append('start') or {'ok': True})
    monkeypatch.setattr(remote, '_stop_connector', lambda *a: called.append('stop') or {'ok': True})
    monkeypatch.setattr(remote, '_record_device_access_switch', lambda *a: None)
    result = getattr(remote, switch)(object())
    assert not result['ok'] and result['error'] == 'reset_in_progress'
    assert request.exists() and not called


def test_unbind_seal_requires_current_preview_and_never_dispatches(pairing_host, monkeypatch):
    from vibe import binding_unbind as unbind

    assert remote.pair('key_A', 'https://backend.test')['ok']
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.backend_url = 'https://avibe.bot'
    cfg.save()
    expected = reset.unbind_snapshot()
    monkeypatch.setattr(unbind, 'send_original', lambda *a, **kw: pytest.fail('dispatch inside sealing'))
    cfg.remote_access.vibe_cloud.instance_secret = 'fixture-new-key'
    cfg.save()
    with pytest.raises(ClaimError, match='local_binding_changed'):
        reset.seal_unbind(expected)
    assert not gate.reset_in_progress()
    expected = reset.unbind_snapshot()
    reset.seal_unbind(expected)
    record = reset._read()
    assert record['phase'] == 'cloud' and record['schema'] == 2
    assert unbind.valid_operation(record['operation']) and not record['accepted']
    assert 'fixture-new-key' not in gate.reset_intent_path().read_text()
    with pytest.raises(ClaimError, match='operation_in_progress'):
        reset.seal_unbind(expected)
    assert reset._read() == record


@pytest.mark.parametrize(('phase', 'requested'), [('invalid_credential', False), ('ineligible', True)])
def test_rejected_unbind_restores_intent_without_connecting_or_sending(reset_host, monkeypatch, phase, requested):
    from vibe import binding_unbind as unbind

    record, workspace, _ = reset_host
    cfg = V2Config.load()
    assert cfg.remote_access.vibe_cloud.enabled is False
    op = '5b81d74b-ad22-42b3-9c20-f3eb2c2d11b9'
    reset._save({**record, 'schema': 2, 'operation': op, 'phase': phase,
                 'accepted': False, 'was_requested': requested, 'root': unbind.root_stamp()})
    monkeypatch.setattr(unbind, 'send_original', lambda *a, **kw: pytest.fail('resent after refusal'))
    monkeypatch.setattr(remote, 'start', lambda *a: pytest.fail('connected after refusal'))
    expected = {'state': 'not_unbound', 'reason_code': phase}
    assert reset.resume() == {**expected, 'operation': op}
    assert unbind.last_result(cfg) == expected
    # A generic status visible to the device page must not enable local-only consent for another op.
    assert unbind.last_result(cfg, operation=op) == expected
    assert unbind.last_result(cfg, operation='fee333ee-ed9c-42fd-bace-1ee148a67c56') is None
    assert not gate.reset_in_progress()
    assert remote._connector_requested_path().exists() is requested
    assert reset._stamp(V2Config.load().remote_access.vibe_cloud) == record['source']
    assert workspace.read_text() == 'keep' and local._installation_id() is not None


@pytest.mark.parametrize('reply', ['lose_first_response', 'html_404', 'html_502', 'wrong_op_200'])
def test_worker_replays_only_transport_loss_not_protocol_failure(pairing_host, monkeypatch, reply):
    import http.client
    import json
    from types import SimpleNamespace
    from vibe import binding_unbind as unbind, direct_certificates

    assert remote.pair('key_A', 'https://backend.test')['ok']
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.backend_url = 'https://avibe.bot'
    cfg.save()
    local.ensure_installation_id()
    monkeypatch.setattr(remote, 'stop', lambda _: {'ok': True})
    monkeypatch.setattr(remote, '_stop_direct_lifecycle_before_pairing_discard', lambda _: None)
    monkeypatch.setattr(direct_certificates, 'discard_direct_certificates', lambda: None)
    monkeypatch.setattr(remote, '_validated_backend_proxy_url', lambda _: None)
    posts = []

    class Connection:
        def connect(self):
            pass

        def request(self, method, path, *, body, headers):
            assert (method, path) == ('POST', '/api/v1/paired-instance/unbind')
            posts.append((body, headers['X-Vibe-Device-Secret']))

        def getresponse(self):
            first = len(posts) == 1
            if reply == 'lose_first_response' and first:
                raise http.client.RemoteDisconnected()
            if reply in ('html_502', 'html_404') and first:
                # A gateway page, or an old service without the route: not the issuer's answer.
                return SimpleNamespace(status=502 if reply == 'html_502' else 404, read=lambda size: b'<html>x</html>',
                                       getheader=lambda key: {'Content-Type': 'text/html'}.get(key))
            if reply == 'wrong_op_200':
                status, data = 200, {'status': 'unbound', 'operation_id': '41d8da35-78cd-43e9-b016-f29971164963'}
            else:
                status, data = 200, {'status': 'unbound', **json.loads(posts[-1][0])}
            return SimpleNamespace(status=status, read=lambda size: json.dumps(data).encode(),
                                   getheader=lambda key: {'Content-Type': 'application/json'}.get(key))

        def close(self):
            pass

    monkeypatch.setattr(remote, '_validated_backend_connections', lambda *a: [Connection()])
    reset.seal_unbind(reset.unbind_snapshot())
    op = reset._read()['operation']
    assert remote._drive_pending_pairing()[0]
    first = unbind.device_state(V2Config.load())['state']
    # Lost replies and pages that are not the issuer's prove nothing: the same request goes again.
    assert first == ('reset_repair_required' if reply == 'wrong_op_200' else 'unbinding')
    assert V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
    assert remote._drive_pending_pairing()[0]
    second = unbind.device_state(V2Config.load())['state']
    if reply != 'wrong_op_200':
        assert second == 'unbound' and len(posts) == 2 and posts[0] == posts[1]
        assert unbind.last_result(V2Config.load()) == {'state': 'unbound', 'host': 'fixture.avibe.bot', 'release_at': None}
        assert unbind.last_result(cfg) is None
    else:
        assert second == 'reset_repair_required' and len(posts) == 1 and gate.reset_in_progress()
    assert json.loads(posts[0][0]) == {'operation_id': op}


def test_refused_unbind_reopens_the_direct_path_it_fenced(reset_host, monkeypatch):
    from types import SimpleNamespace
    from vibe import binding_unbind as unbind, direct_lifecycle

    record, _, _ = reset_host
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.direct = True
    cfg.save()
    lifecycle = SimpleNamespace(released=[])
    lifecycle.release_pairing_suspension = lifecycle.released.append
    monkeypatch.setattr(direct_lifecycle, 'active_lifecycle', lambda: lifecycle)
    # The attempt's own stop answers with token 5; only that token is released.
    monkeypatch.setattr(remote, '_stop_direct_lifecycle_before_pairing_discard', lambda cfg: 5)
    monkeypatch.setattr(unbind, 'send_original', lambda *a, **kw: 'ineligible')
    reset._save({**reset._new_record(V2Config.load()), 'schema': 2, 'operation': '7a0a3a3e-9f43-4a52-9c1e-1d1a4f3b6c20',
                 'phase': 'cloud', 'accepted': False, 'was_requested': True, 'root': unbind.root_stamp()})
    assert reset.resume()['state'] == 'not_unbound' and lifecycle.released == [5]


def test_lifecycle_release_clears_only_a_pairing_suspension():
    from types import SimpleNamespace
    from vibe.direct_lifecycle import DirectAccessLifecycle

    woke = []
    state = SimpleNamespace(_shutdown=False, _pairing_suspended=True, _suspension_token=2, _permission_allowed=False,
                            _permission_received_at=1.0, _permission_reason='pairing_changed', _resume_required=False,
                            _wake=SimpleNamespace(set=lambda: woke.append(1)))
    DirectAccessLifecycle._release_pairing_suspension(state, 1)  # A later pairing change stopped it since.
    assert state._pairing_suspended and not woke
    DirectAccessLifecycle._release_pairing_suspension(state, 2)
    assert not state._pairing_suspended and state._permission_allowed is None and state._resume_required and woke
    DirectAccessLifecycle._release_pairing_suspension(state, 2)
    assert len(woke) == 1


def test_worker_finishes_authorized_reset_including_private_health_store(reset_host, monkeypatch):
    from storage import mobile_store
    _, workspace, calls = reset_host
    # Prove the separate mobile database is in the fixture home before writing any values.
    assert mobile_store.mobile_store_path().is_relative_to(workspace.parent)
    monkeypatch.setattr(mobile_store, 'publish_mobile_schedule_changed', lambda: None)
    store = mobile_store.MobileStore()
    with store.transaction() as conn:
        for device in ('fixture-phone', 'fixture-tablet'):
            conn.execute('INSERT INTO devices(id, credential_hash, instance_id, owner, name, app_version, metrics,'
                         ' created_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?)',
                         (device, device, 'inst_A', 'owner', device, '1', '[]', '2026-11-01', '2026-11-02'))
            conn.execute('INSERT INTO health_days VALUES(?,?,?,?,?)', (device, 'fixture', '2026-11-01', '{}', 'now'))
        conn.execute('INSERT INTO pairings VALUES(?,?,?,?,?)', ('code', 'request', 'inst_A', 'owner', 'later'))
    assert remote._drive_pending_pairing() == (True, 1)
    assert not gate.reset_in_progress() and local._installation_id() is None
    assert not V2Config.load().remote_access.vibe_cloud.instance_id
    with store.transaction() as conn:
        assert conn.execute('SELECT count(*) FROM devices WHERE revoked_at IS NOT NULL').fetchone()[0] == 2
        for table in ('health_days', 'pairings'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert workspace.read_text() == 'keep' and len(calls) == 1


@pytest.fixture
def cli_host(pairing_host, monkeypatch):
    from types import SimpleNamespace
    from vibe import cli, direct_certificates

    assert remote.pair('key_A', 'https://backend.test')['ok']
    monkeypatch.setattr(reset, 'sys', SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True),
                                                      stdout=SimpleNamespace(isatty=lambda: True)))
    monkeypatch.setattr(cli, '_cli_invocation_authority', lambda: None)
    monkeypatch.setattr(remote, 'stop', lambda _: {'ok': True})
    monkeypatch.setattr(remote, '_stop_direct_lifecycle_before_pairing_discard', lambda _: None)
    monkeypatch.setattr(direct_certificates, 'discard_direct_certificates', lambda: None)
    return pairing_host


@pytest.mark.parametrize(('phase', 'entry'), [('repair', 'rebind'), ('repair', 'reset'), ('cloud', 'reset')])
def test_terminal_replaces_cloud_marker_locally_without_new_operation(cli_host, monkeypatch, phase, entry):
    from vibe import binding_unbind as unbind
    cfg = V2Config.load()
    op = '060bb169-2fa6-498d-9a4f-bf269a448b95'
    reset._save({**reset._new_record(cfg), 'schema': 2, 'operation': op, 'phase': phase,
                 'accepted': True, 'was_requested': True, 'root': unbind.root_stamp()})
    monkeypatch.setattr(unbind, 'send_original', lambda *a, **kw: pytest.fail('local override sent a request'))
    snapshots = []
    save = reset._save
    def observe(record):
        snapshots.append((record['schema'], record['phase'], record['operation']))
        save(record)
    monkeypatch.setattr(reset, '_save', observe)
    if entry == 'rebind':
        decision = lambda consent: reset.reset_interactively(confirm=consent, repair=True)
    else:
        decision = lambda consent: reset.reset_local_only_interactively(confirm=consent)
    assert decision(lambda: False) == {'state': 'declined'} and not snapshots
    assert decision(lambda: True) == {'state': 'unpaired'}
    assert snapshots[0][:2] == (1, 'retire')
    assert {operation for _, _, operation in snapshots} == {op.replace('-', '')}
    assert not gate.reset_in_progress() and not V2Config.load().remote_access.vibe_cloud.instance_id


@pytest.mark.parametrize('kind', ['stdin', 'stdout', 'delegated', 'http'])
def test_cli_requires_local_interactive_context_before_prompt(cli_host, monkeypatch, kind):
    from vibe import cli, ui_request_context

    if kind in ('stdin', 'stdout'):
        monkeypatch.setattr(getattr(reset.sys, kind), 'isatty', lambda: False)
    elif kind == 'delegated':
        monkeypatch.setattr(cli, '_cli_invocation_authority', lambda: object())
    else:
        monkeypatch.setattr(ui_request_context, 'has_request_context', lambda: True)
    with pytest.raises(ClaimError, match='local_operator_required'):
        reset.reset_interactively(confirm=lambda: pytest.fail('prompt reached'))
    assert not gate.reset_in_progress() and V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'


@pytest.mark.parametrize('answer', [False, 1, 'yes', 'eof', 'changed', True])
def test_cli_confirmation_binds_even_damaged_files(cli_host, answer):
    local._identity_path().write_bytes(b'broken installation')
    remote._pending_pairing_path().write_bytes(b'broken journal')

    def confirm():
        if answer == 'eof':
            raise EOFError
        if answer == 'changed':
            remote._pending_pairing_path().write_bytes(b'another attempt')
            return True
        return answer

    if answer == 'changed':
        with pytest.raises(ClaimError, match='local_binding_changed'):
            reset.reset_interactively(confirm=confirm)
    else:
        view = reset.reset_interactively(confirm=confirm)
        assert view == {'state': 'unpaired' if answer is True else 'declined'}
    assert not gate.reset_in_progress() and len(cli_host[1]) == 1
    assert local._identity_path().exists() is (answer is not True)
    assert remote._pending_pairing_path().exists() is (answer is not True)
    assert V2Config.load().remote_access.vibe_cloud.instance_id == ('' if answer is True else 'inst_A')


def test_two_senders_share_publication_lease(tmp_path):
    from storage.lock import MigrationFileLock, MigrationLockTimeout, try_shared_file_lease
    path = tmp_path / 'egress.lock'
    with try_shared_file_lease(path):
        with try_shared_file_lease(path):
            with pytest.raises(MigrationLockTimeout):
                with MigrationFileLock(path, timeout_seconds=0):
                    pytest.fail('publisher admitted during sends')
    with MigrationFileLock(path, timeout_seconds=0):
        assert try_shared_file_lease(path) is None


def _hold_old_response(pipe):
    from vibe import instance_address as addr

    host = 'old-app.avibe.bot'
    binding = addr.current_binding()
    addr._authority = addr._Authority(binding.pairing, 1, {host: None}, addr._monotonic(), addr._wall())
    pipe.send(addr.begin_cleanup_response(host))
    try:
        action = pipe.recv()
        if action in ('finish', 'wait-for-publication'):
            addr.end_cleanup_response(host)
            if action == 'wait-for-publication':
                pipe.send('released')
                pipe.recv()
            pipe.send(addr.begin_cleanup_response(host))
    finally:
        pipe.close()


@pytest.mark.parametrize('crash', [False, True])
def test_reset_waits_for_other_process_cleanup(cli_host, monkeypatch, crash):
    import multiprocessing
    from vibe import instance_address as addr

    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_hold_old_response, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() is True
        with gate.admitted('reset'), config_file_lock():
            record = reset._seal(V2Config.load())
        monkeypatch.setattr(addr, '_DRAIN_TIMEOUT_SECONDS', 0.05)
        assert remote._drive_pending_pairing() == (True, 60)
        assert reset._read() == record and V2Config.load().remote_access.vibe_cloud.instance_id == 'inst_A'
        if crash:
            process.terminate()
        else:
            parent.send('finish')
            assert parent.poll(5) and parent.recv() is False
        process.join(5)
        assert not process.is_alive()
        assert remote._drive_pending_pairing() == (True, 1)
        assert not gate.reset_in_progress() and not V2Config.load().remote_access.vibe_cloud.instance_id
        assert len(cli_host[1]) == 1
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()


def test_same_id_publication_waits_for_other_process_cleanup(cli_host, monkeypatch):
    import multiprocessing
    from vibe import instance_address as addr

    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_hold_old_response, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() is True
        before = addr.current_binding()
        monkeypatch.setattr(addr, '_DRAIN_TIMEOUT_SECONDS', 0.05)
        assert remote.pair('rotate-in-place', 'https://backend.test')['error'] == 'pairing_save_failed_after_redeem'
        assert addr.current_binding() == before
        assert remote._read_pending_pairing_record()[0]['phase'] == 'redeemed'
        parent.send('wait-for-publication')
        assert parent.poll(5) and parent.recv() == 'released'
        assert remote._drive_pending_pairing() == (True, 1)
        after = addr.current_binding()
        assert after.instance_id == before.instance_id and after.pairing != before.pairing
        parent.send('check')
        assert parent.poll(5) and parent.recv() is False and len(cli_host[1]) == 2
    finally:
        parent.close()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.parametrize('damaged', [False, True])
def test_reset_overview_and_ingress_stay_fenced(cli_host, monkeypatch, damaged):
    from vibe import direct_lifecycle, personal_claims
    cfg = V2Config.load()
    assert direct_lifecycle._is_paired(cfg)
    with gate.admitted('reset'), config_file_lock():
        reset._seal(cfg)
    if damaged:
        gate.reset_intent_path().write_text('{')
    monkeypatch.setattr(remote, '_read_pid', lambda: None)
    view = personal_claims.rebind_claims.overview()
    assert view['binding_state'] == ('reset_repair_required' if damaged else 'resetting')
    assert not view['can_start'] and view['attempt'] is None and view['next_action'] is None
    assert not direct_lifecycle._is_paired(cfg)
    monkeypatch.setattr(remote, '_resolve_binary', lambda *_: pytest.fail('must not spawn a connector'))
    assert remote._start(cfg, record_request=True)['error'] == 'reset_in_progress'
    with pytest.raises(RuntimeError, match='reset_in_progress'):
        remote._start_candidate_connector(cfg, 'not-spawned', 'auto')
    assert gate.reset_in_progress() and len(cli_host[1]) == 1


def test_stale_reset_cannot_clear_other_generation(reset_host):
    _, workspace, _ = reset_host
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.instance_secret = 'fixture-new'
    cfg.save()
    with pytest.raises(ClaimError, match='local_binding_changed'):
        reset.resume()
    assert gate.reset_in_progress() and workspace.read_text() == 'keep'
    assert V2Config.load().remote_access.vibe_cloud.instance_secret == 'fixture-new'
