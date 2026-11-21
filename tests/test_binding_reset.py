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
