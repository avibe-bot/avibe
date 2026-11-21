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


def test_stale_reset_cannot_clear_other_generation(reset_host):
    _, workspace, _ = reset_host
    cfg = V2Config.load()
    cfg.remote_access.vibe_cloud.instance_secret = 'fixture-new'
    cfg.save()
    with pytest.raises(ClaimError, match='local_binding_changed'):
        reset.resume()
    assert gate.reset_in_progress() and workspace.read_text() == 'keep'
    assert V2Config.load().remote_access.vibe_cloud.instance_secret == 'fixture-new'
