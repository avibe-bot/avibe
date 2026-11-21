"""Pairing publication retires only predecessor authority, once per registry."""
import pytest
from sqlalchemy import select

from config.v2_config import V2Config
from storage import device_registry as registry, web_push_service as push
from storage.db import get_cached_sqlite_engine
from storage.models import remote_device_sessions as sessions
from tests.test_pairing_recovery import _fail_config_publication_once, pairing_host as pairing_host
from vibe import remote_access


@pytest.mark.parametrize('damaged', [False, True])
def test_reset_marker_excludes_both_writers_before_network(pairing_host, damaged):
    from vibe import binding_admission as admission, desktop_local_instance as local

    marker = admission.reset_intent_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    if damaged:
        marker.symlink_to(marker.with_name('nonexistent-reset-target'))
    else:
        marker.write_text('{}')
    with pytest.raises(admission.OperationBusy):
        with admission.admitted('address'):
            pytest.fail('rename acquired admission during reset')
    with pytest.raises(local.LocalInstanceError, match='operation_in_progress'):
        local.prepare('fixture-account', 'old-revision')
    assert remote_access.pair('key_A', 'https://backend.test')['error'] == 'operation_in_progress'
    assert remote_access._drive_pending_pairing() == (True, 60)
    assert not pairing_host[1] and not local._identity_path().exists()


def test_same_id_publication_crash_cannot_resurrect_old_or_retire_new(pairing_host, monkeypatch):
    base = 'https://backend.test'
    assert remote_access.pair('key_A', base)['ok']
    old = remote_access.device_binding(V2Config.load())
    engine = get_cached_sqlite_engine()

    def enroll(binding, sid):
        identity = {'browser_session_id': sid, 'auth_time': 1, 'sub': 'owner', 'email': 'fixture@example.test', 'exp': 101}
        registry.record_session(binding, identity, 'owner', {'kind': 'browser'}, expires_at=101, now=2, fresh=True)

    def subscribe(user, label):
        with engine.begin() as conn:
            push.upsert_subscription(conn, user_key=user, payload={'endpoint': 'https://example.invalid/' + label,
                                                                  'keys': {'p256dh': 'fixture', 'auth': 'fixture'}})

    enroll(old, 'old')
    subscribe('remote:owner', 'unattested')
    subscribe('local', 'local')
    _fail_config_publication_once(monkeypatch)
    assert remote_access.pair('rotate-in-place', base)['error'] == 'pairing_save_failed_after_redeem'
    record, error = remote_access._read_pending_pairing_record()
    assert not error and record['source_registry_binding'] == old and record['phase'] == 'redeemed'
    assert remote_access.device_binding(V2Config.load()) == old
    with engine.connect() as conn:
        assert registry.fenced(conn, old, 'owner', 'never-registered', 3, 4)
        assert conn.scalar(select(sessions.c.session_id).where(sessions.c.binding == old)) is None
        assert not push.count_enabled(conn, user_key='remote:owner')
        assert push.count_enabled(conn, user_key='local') == 1
    with pytest.raises(PermissionError, match='session_revoked'):
        enroll(old, 'late-login')
    assert remote_access._drive_pending_pairing() == (True, 1)
    new = remote_access.device_binding(V2Config.load())
    assert new != old
    enroll(new, 'current')
    subscribe('remote:owner', 'unattested')
    registry.retire_binding(old, now=5)
    with engine.connect() as conn:
        assert conn.scalar(select(sessions.c.session_id).where(sessions.c.binding == new)) == 'current'
        assert push.count_enabled(conn, user_key='remote:owner') == push.count_enabled(conn, user_key='local') == 1
