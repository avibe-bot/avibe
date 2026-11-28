"""Owner continuity uses synthetic private stores and registry links only."""
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from core.handlers import mobile_health
from core.mobile_health_contract import MobileError
from storage.db import get_cached_sqlite_engine
from storage.mobile_store import MobileStore
from storage.models import device_health_links
from tests.test_mobile_health import (  # noqa: F401
    IDENTITY, clock, device_for, mobile_http, pair_device, store, upload_payload,
)
from vibe import cli, cli_mobile, permissions
from vibe.remote_access import device_binding
from config.v2_config import V2Config

SUBJECT = "owner-SUBJECT"


def subject_identity(email=IDENTITY['owner'], subject=SUBJECT):
    return {**IDENTITY, 'owner': email, 'owner_subject': subject}


def new_pair(store, identity):
    req, _ = store.create_request('health.connect', identity)
    code = store.issue_pairing(req['id'], identity)
    paired = store.pair(dict(pairing_code=code['pairing_code'], device_name='Synthetic',
                             platform='ios', app_version='1', metrics=['steps', 'sleep']), identity)
    return paired, req


def link_device(mid, *, subject=SUBJECT, cleanup=None, binding=None):
    from storage.importer import ensure_sqlite_state
    ensure_sqlite_state()
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(insert(device_health_links).values(
            mobile_id=mid, binding=binding or device_binding(V2Config.load()), subject=subject,
            device_id='synthetic-phone', linked_at='2026-11-18T00:00:00+00:00', cleanup_pending=cleanup))


def counts(store):
    with store.transaction() as conn:
        return tuple(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('health_days', 'metric_sync', 'pairings'))


def test_subject_survives_changed_or_absent_email(store):
    identity = subject_identity()
    paired, req = new_pair(store, identity)
    store.upload(store.authenticate(paired['credential'], identity), upload_payload())
    before = counts(store)
    for email in ('changed@example.test', ''):
        changed = subject_identity(email)
        assert store.authenticate(paired['credential'], changed)['id'] == paired['device_id']
        assert store.request_for_identity(req['id'], changed)['id'] == req['id']
        request, created = store.create_request('health.connect', changed)
        assert request['id'] == req['id'] and not created
        assert counts(store) == before


def test_subject_change_with_identical_email_revokes(store):
    paired, _ = new_pair(store, subject_identity())
    store.upload(store.authenticate(paired['credential'], subject_identity()), upload_payload())
    with pytest.raises(MobileError) as exc:
        store.authenticate(paired['credential'], subject_identity(subject='other'))
    assert exc.value.status == 403
    assert counts(store)[:2] == (0, 0)


def test_linked_legacy_migration_preserves_all_tables(store, mobile_http):
    _, projection = mobile_http
    paired, request = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    store.cancel_request(request['id'])
    expand, _ = store.create_request('health.connect', IDENTITY)
    store.issue_pairing(expand['id'], IDENTITY)
    before = counts(store)
    link_device(paired['device_id'])
    projection['access']['owner'] = {'user_id': SUBJECT, 'email': None}
    assert mobile_health.current_identity(store)['owner_subject'] == SUBJECT
    with store.transaction() as conn:
        for table in ('devices', 'pairings', 'requests'):
            assert {r[0] for r in conn.execute(f'SELECT owner_subject FROM {table}')} == {SUBJECT}
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
    assert counts(store) == before
    assert mobile_health.current_identity(MobileStore(store.path))['owner_subject'] == SUBJECT
    assert store.authenticate(paired['credential'], subject_identity(''))['id'] == paired['device_id']


@pytest.mark.parametrize('proof', ['absent', 'different', 'cleanup', 'binding'])
def test_unproven_migration_pauses_without_deletion(store, mobile_http, proof):
    _, projection = mobile_http
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    if proof != 'absent':
        link_device(paired['device_id'], subject='different' if proof == 'different' else SUBJECT,
                    cleanup=1 if proof == 'cleanup' else None,
                    binding='wrong' if proof == 'binding' else None)
    projection['access']['owner']['user_id'] = SUBJECT
    before = counts(store)
    with pytest.raises(MobileError) as exc:
        mobile_health.current_identity(store)
    assert exc.value.code == 'instance_eligibility_unavailable' and exc.value.status == 503
    assert exc.value.details['reason'] == 'legacy_owner_unproven'
    assert counts(store) == before and store.active_device()


def test_bound_store_never_downgrades_to_old_cloud(store, mobile_http):
    _, projection = mobile_http
    paired, _ = new_pair(store, subject_identity())
    store.upload(store.authenticate(paired['credential'], subject_identity()), upload_payload())
    projection['access']['owner'] = {'email': 'different@example.test'}
    before = counts(store)
    with pytest.raises(MobileError) as exc:
        mobile_health.current_identity(store)
    assert exc.value.details['reason'] == 'cloud_owner_subject_unsupported'
    assert counts(store) == before and store.active_device()


def test_unavailable_status_does_not_open_private_store(monkeypatch, tmp_path, capsys):
    from tests.ui_server_test_helpers import _save_config
    _save_config(tmp_path)
    monkeypatch.setattr(cli_mobile, 'MobileStore', lambda: pytest.fail('private store opened'))
    args = cli.build_parser().parse_args(['mobile', 'health', 'status'])
    assert cli_mobile.cmd_mobile(args) == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result['read_allowed'] is False and result['sync_state'] == 'unknown'
    assert 'devices' not in result and 'metrics' not in result


def test_live_subject_status_after_upload_without_read(store, mobile_http):
    _, projection = mobile_http
    projection['access']['owner'] = {'user_id': SUBJECT, 'email': None}
    paired, _ = new_pair(store, subject_identity(''))
    store.upload(store.authenticate(paired['credential'], subject_identity('')), upload_payload())
    args = cli.build_parser().parse_args(['mobile', 'health', 'status'])
    result = cli_mobile.execute(args)
    assert result['read_allowed'] is True and result['connected']


def test_browser_and_push_owner_checks_use_subject_with_null_email():
    context = SimpleNamespace(is_instance_owner=True, is_remote=True, instance_id=IDENTITY['id'],
                              subject=SUBJECT, email=None)
    assert mobile_health.context_is_owner(context, subject_identity(''))
    assert not mobile_health.context_is_owner(context, subject_identity(subject='other'))


def test_ticket_redemption_matches_subject_with_null_email(install):
    from storage import device_health_tickets as tickets
    from tests.test_device_health_cleanup import phone
    from tests.test_instance_device_registry import client, report
    store = MobileStore()
    identity = subject_identity('', 'owner')
    paired, _ = new_pair(store, identity)
    cookie, viewer, _ = phone(install, 'subject-phone')
    token = tickets.issue(store, {'id': paired['device_id']}, viewer[0])['health_binding_ticket']
    response = report(client(cookie), 'subject-phone', health_binding_ticket=token)
    assert response.status_code == 200


def test_catalog_subject_scope_survives_email_change(install):
    import time
    from storage import device_catalog, device_health_tickets as tickets
    from tests.test_device_health_cleanup import phone
    from tests.test_instance_device_registry import client, report
    store = MobileStore()
    identity = subject_identity('old@example.test', 'owner')
    paired, _ = new_pair(store, identity)
    cookie, viewer, _ = phone(install, 'subject-phone')
    token = tickets.issue(store, {'id': paired['device_id']}, viewer[0])['health_binding_ticket']
    assert report(client(cookie), 'subject-phone', health_binding_ticket=token).status_code == 200
    with get_cached_sqlite_engine().connect() as conn:
        rows, active, _ = device_catalog.snapshot(conn, viewer, IDENTITY['id'], 'new@example.test',
                                                  int(time.time()), health_identity=identity)
    assert any(paired['device_id'] in mids for mids in active.values())
    assert any(f['kind'] == 'health' for row in rows.values() for f in row['functions'])


from tests.test_instance_device_registry import install  # noqa: F401, E402


def test_schema_migration_fences_released_writer_without_deleting_data(store, monkeypatch):
    from storage import mobile_store
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    before = counts(store)
    with store.transaction() as conn:
        indexes = list(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql LIKE '%owner_subject%'"))
        for row in indexes:
            conn.execute(f'DROP INDEX "{row[0]}"')
        for table in ('devices', 'requests', 'pairings'):
            conn.execute(f'ALTER TABLE {table} DROP COLUMN owner_subject')
        conn.execute('PRAGMA user_version=1')
    upgraded = MobileStore(store.path)
    with upgraded.transaction() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        assert conn.execute('SELECT owner_subject FROM devices').fetchone()[0] is None
    with monkeypatch.context() as old:
        old.setattr(mobile_store, 'SCHEMA_VERSION', 1)  # Exact released constructor version fence.
        with pytest.raises(MobileError) as exc:
            MobileStore(store.path)
        assert (exc.value.code, exc.value.status) == ('contract_unsupported', 426)
    assert counts(MobileStore(store.path)) == before


def test_migration_transaction_rolls_back_and_retries(store, mobile_http):
    import sqlite3
    _, projection = mobile_http
    paired, _ = pair_device(store)
    link_device(paired['device_id'])
    projection['access']['owner']['user_id'] = SUBJECT
    with store.transaction() as conn:
        conn.execute("CREATE TRIGGER interrupt_owner BEFORE UPDATE OF owner_subject ON requests "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic interruption'):
        mobile_health.current_identity(store)
    with store.transaction() as conn:
        assert conn.execute('SELECT owner_subject FROM devices').fetchone()[0] is None
        conn.execute('DROP TRIGGER interrupt_owner')
    assert mobile_health.current_identity(MobileStore(store.path))['owner_subject'] == SUBJECT


def test_pending_pairing_registry_proves_request_and_code(store, mobile_http):
    from storage.device_health_tickets import digest
    _, projection = mobile_http
    req, _ = store.create_request('health.connect', IDENTITY)
    code = store.issue_pairing(req['id'], IDENTITY)
    from storage.importer import ensure_sqlite_state
    ensure_sqlite_state()
    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(insert(device_health_links).values(mobile_id='reserved', subject=SUBJECT,
                     binding=device_binding(V2Config.load()), device_id='phone', linked_at='2026-11-18',
                     code_hash=digest(code['pairing_code'])))
    projection['access']['owner']['user_id'] = SUBJECT
    identity = mobile_health.current_identity(store)
    result = store.pair(dict(pairing_code=code['pairing_code'], device_name='Synthetic',
                            platform='ios', app_version='1', metrics=['steps']), identity)
    assert store.authenticate(result['credential'], identity)['owner_subject'] == SUBJECT


def test_concurrent_enrollment_fences_delayed_subject_answer(store, mobile_http, monkeypatch):
    _, projection = mobile_http
    projection['access']['owner']['user_id'] = SUBJECT
    def delayed(_config):
        paired, _ = new_pair(store, subject_identity(subject='new-owner'))
        store.upload(store.authenticate(paired['credential'], subject_identity(subject='new-owner')), upload_payload())
        return SimpleNamespace(source='live', projection=projection)
    monkeypatch.setattr(permissions, 'get_current_permissions', delayed)
    with pytest.raises(MobileError) as exc:
        mobile_health.current_identity(store)
    assert exc.value.status == 503
    assert counts(store)[:2] == (2, 2)
    assert store.active_device()['owner_subject'] == 'new-owner'


@pytest.mark.parametrize('value', [None, '', ' ', 1, ' padded '])
def test_malformed_subject_never_falls_back_to_matching_email(store, mobile_http, value):
    _, projection = mobile_http
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    projection['access']['owner']['user_id'] = value
    with pytest.raises(MobileError) as exc:
        mobile_health.current_identity(store)
    assert exc.value.details['reason'] == 'owner_record_missing'
    assert counts(store)[:2] == (2, 2)


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_owner_cli_is_english_while_http_uses_ui_language(store, mobile_http, capsys, language):
    import json
    from vibe.i18n import t
    from vibe.ui_mobile_routes import mobile_error_response
    _, projection = mobile_http
    config = V2Config.load()
    config.language = language
    config.save()
    projection['access']['owner']['user_id'] = None
    args = cli.build_parser().parse_args(['mobile', 'health', 'read', '--metric', 'steps'])
    assert cli_mobile.cmd_mobile(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['code'] == 'instance_eligibility_unavailable'
    assert result['details']['reason'] == 'owner_record_missing'
    assert result['error'] == t('mobile.health.owner.owner_record_missing', 'en')
    assert 'next_action' not in result
    error = MobileError(result['code'], 503, details=result['details'])
    assert json.loads(mobile_error_response(error).body)['error'] == t('mobile.health.owner.owner_record_missing', language)
    status_args = cli.build_parser().parse_args(['mobile', 'health', 'status'])
    assert cli_mobile.cmd_mobile(status_args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['diagnostic']['error'] == result['error']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('answer', ['same', 'other', 'old-cloud', 'offline'])
def test_deferred_signout_owner_proof_preserves_or_revokes_exact_pairing(store, mobile_http, monkeypatch, legacy, answer):
    import time
    from storage import device_health_owner_checks as checks
    from vibe.account_session_revoke import pairing_epoch
    _, projection = mobile_http
    if legacy:
        paired, _ = pair_device(store)
        device = device_for(store, paired)
    else:
        paired, _ = new_pair(store, subject_identity())
        device = store.authenticate(paired['credential'], subject_identity())
    store.upload(device, upload_payload())
    link_device(paired['device_id'])
    config = V2Config.load()
    with get_cached_sqlite_engine().begin() as conn:
        checks.queue(conn, binding=device_binding(config), epoch=pairing_epoch(config), subject=SUBJECT,
                     device='synthetic-phone', email=IDENTITY['owner'], now=int(time.time()))
    if answer in ('same', 'other'):
        projection['access']['owner'] = {'user_id': SUBJECT if answer == 'same' else 'other', 'email': None}
    elif answer == 'offline':
        monkeypatch.setattr(permissions, 'get_current_permissions', lambda *_: SimpleNamespace(source='cache'))
    checks.run_once(lambda: config)
    should_revoke = answer == 'same' or (legacy and answer == 'old-cloud')
    assert bool(store.active_device()) is not should_revoke
    assert counts(store)[:2] == ((0, 0) if should_revoke else (2, 2))


def test_native_notification_uses_authorization_subject_not_claim_email(store, mobile_http, monkeypatch):
    from core import web_push
    from storage import remote_access_authorization_service as authorizations, web_push_service
    from storage.importer import ensure_sqlite_state
    ensure_sqlite_state()
    for index, subject in enumerate((SUBJECT, 'other')):
        authorizations.upsert_scoped(
            reference=None, instance_id=IDENTITY['id'], subject=subject, email='same@example.test',
            scope_kind='instance', scope_ref=IDENTITY['id'], authorization_state='current',
            last_checked_at=1, updated_at=1,
            claims={'vibe_instance_id': IDENTITY['id'], 'vibe_instance_role': 'owner',
                    'vibe_instance_access_source': 'owner'})  # Actual claims exclude sub/email.
        with get_cached_sqlite_engine().begin() as conn:
            web_push_service.upsert_subscription(conn, user_key=f'remote:{subject}', device_id=subject,
                payload={'endpoint': 'https://avibe.bot/api/push/apns/' + str(index) * 43,
                         'keys': {'p256dh': 'fake-key', 'auth': 'fake-auth'}})
    sent = []
    monkeypatch.setattr(web_push, 'send_web_push', lambda **kwargs: sent.append(kwargs))
    request, _ = store.create_request('health.connect', subject_identity(''))
    mobile_health.notify_phone(store, request, subject_identity(''), caller='Synthetic')
    assert [x['subscription']['user_key'] for x in sent] == [f'remote:{SUBJECT}']


def test_stale_connect_cannot_expand_another_subjects_device(store):
    paired, req = new_pair(store, subject_identity(subject='current'))
    store.upload(store.authenticate(paired['credential'], subject_identity(subject='current')), upload_payload())
    with pytest.raises(MobileError) as exc:
        store.create_request('health.connect', subject_identity(subject='prior'))
    assert exc.value.code == 'instance_eligibility_unavailable'
    assert store.show_request(req['id'])['state'] == 'queued'
    assert counts(store)[:2] == (2, 2)


@pytest.mark.parametrize('new_cloud', [False, True])
@pytest.mark.parametrize('new_instance', [False, True])
def test_cloud_instance_version_matrix(store, mobile_http, new_cloud, new_instance):
    from tests.fixtures.health_owner_v1 import current_identity as released_gate
    _, projection = mobile_http
    if new_cloud:
        projection['access']['owner']['user_id'] = SUBJECT
    identity = (mobile_health.current_identity if new_instance else released_gate)(store)
    assert identity['owner'] == IDENTITY['owner']
    assert identity.get('owner_subject') == (SUBJECT if new_cloud and new_instance else None)
    paired, req = new_pair(store, identity)
    assert store.authenticate(paired['credential'], identity)['id'] == paired['device_id']
    assert store.show_request(req['id'])['state'] == 'queued'


def test_released_projection_parser_accepts_additive_owner_subject_and_null_email():
    from tests.test_permissions import _complete_projection
    projection = _complete_projection()
    projection['access']['owner'].update(user_id=SUBJECT, email=None)
    parsed = permissions._validated_projection(projection, projection['instance']['id'])
    assert parsed['access']['owner']['user_id'] == SUBJECT
    assert parsed['access']['owner']['email'] is None


def test_real_cli_browser_pair_upload_and_devices_share_subject(install, monkeypatch, clock):
    from tests.test_device_health_cleanup import phone
    from tests.test_instance_device_registry import client, listing, ORIGIN
    from tests.ui_server_test_helpers import csrf_headers
    install.remote_access.vibe_cloud.instance_secret = 'fixture-runtime-credential'
    install.save()
    projection = {'instance': {'id': IDENTITY['id']}, 'access': {'owner': {'user_id': 'owner', 'email': None}}}
    monkeypatch.setattr(permissions, 'get_current_permissions', lambda *_: SimpleNamespace(source='live', projection=projection))
    cookie, _, target = phone(install, 'real-flow-phone')
    c = client(cookie)
    connect = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'connect', '--no-callback']))
    request = connect['request']
    response = c.get('/api/mobile/requests/' + request['id'], base_url=ORIGIN)
    assert response.status_code == 200
    approval = c.post('/api/mobile/pairings', json={'request_id': request['id']}, base_url=ORIGIN,
                      headers=csrf_headers(c, ORIGIN))
    assert approval.status_code == 200
    paired = c.post('/api/mobile/device/v1/pair', json=dict(pairing_code=approval.get_json()['pairing_code'],
                    device_name='Synthetic phone', platform='ios', app_version='1', metrics=['steps', 'sleep']))
    assert paired.status_code == 200
    bearer = {'Authorization': 'Bearer ' + paired.get_json()['credential']}
    uploaded = c.put('/api/mobile/device/v1/health/days', json=upload_payload(), headers=bearer)
    assert uploaded.status_code == 200
    # No health read precedes status or the Devices projection.
    status = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'status']))
    assert status['read_allowed'] and status['connected']
    projection['access']['owner']['email'] = 'changed@example.test'
    row = next(r for r in listing(c)['mine'] if r['id'] == target)
    assert any(f['kind'] == 'health' and f['on'] for f in row['functions'])
    assert c.get('/api/mobile/requests/' + request['id'], base_url=ORIGIN).status_code == 200
    projection['access']['owner']['user_id'] = 'next-owner'
    refused = c.put('/api/mobile/device/v1/health/days', json=upload_payload(seq=2), headers=bearer)
    assert refused.status_code == 403 and refused.get_json()['code'] == 'device_revoked'
    assert counts(MobileStore())[:2] == (0, 0)


@pytest.mark.parametrize('command', ['read', 'connect', 'status', 'upload'])
def test_unproven_owner_blocks_consumers_and_status_hides_metadata(store, mobile_http, command):
    c, projection = mobile_http
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    projection['access']['owner']['user_id'] = SUBJECT
    if command == 'upload':
        result = c.put('/api/mobile/device/v1/health/days', json=upload_payload(seq=2),
                       headers={'Authorization': 'Bearer ' + paired['credential']})
        assert result.status_code == 503
        assert result.get_json()['details']['reason'] == 'legacy_owner_unproven'
    else:
        args = cli.build_parser().parse_args(['mobile', 'health', command])
        if command == 'status':
            result = cli_mobile.execute(args)
            assert result['read_allowed'] is False and result['sync_state'] == 'unknown'
            assert set(result) == {'kind', 'read_allowed', 'sync_state', 'diagnostic'}
            assert paired['device_id'] not in str(result) and '987654321' not in str(result)
        else:
            with pytest.raises(MobileError) as exc:
                cli_mobile.execute(args)
            assert (exc.value.code, exc.value.status) == ('instance_eligibility_unavailable', 503)
    assert counts(store)[:2] == (2, 2)


def test_read_audit_projection_matches_subject_after_email_change(install):
    from storage import device_activity_feed as feed, device_health_tickets as tickets
    from tests.test_device_health_cleanup import phone
    from tests.test_instance_device_registry import client, report
    from vibe.authorization import AuthorizationContext
    store = MobileStore()
    identity = subject_identity('prior@example.test', 'owner')
    paired, _ = new_pair(store, identity)
    cookie, viewer, target = phone(install, 'audit-phone')
    token = tickets.issue(store, {'id': paired['device_id']}, viewer[0])['health_binding_ticket']
    assert report(client(cookie), 'audit-phone', health_binding_ticket=token).status_code == 200
    store.read(['steps'], '2026-11-18', '2026-11-18', caller='synthetic-agent')
    context = AuthorizationContext(instance_role='owner', subject='owner', is_remote=True)
    with get_cached_sqlite_engine().connect() as conn:
        page = feed.list_page(conn, viewer, context, identity['id'], 'new@example.test', limit=100,
                              raw_cursor=None, device=target, health_identity={**identity, 'owner': ''})
        denied = feed.list_page(conn, viewer, context, identity['id'], 'new@example.test', limit=100,
                                raw_cursor=None, device=target, health_identity={})
    assert any(x['kind'] == 'healthRead' for x in page['items'])
    assert not any(x['kind'] == 'healthRead' for x in denied['items'])


def test_verified_status_never_queries_health_value_rows(store, mobile_http, monkeypatch):
    _, projection = mobile_http
    projection['access']['owner']['user_id'] = SUBJECT
    paired, _ = new_pair(store, subject_identity())
    store.upload(store.authenticate(paired['credential'], subject_identity()), upload_payload())
    monkeypatch.setattr(MobileStore, '_trim_days', lambda *_: pytest.fail('status read health value rows'))
    result = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'status']))
    assert result['read_allowed'] and result['metrics']['steps']['synced_at']


@pytest.mark.parametrize('live_email', [None, 'changed@example.test'])
def test_local_host_health_attribution_and_revoke_use_subject(install, live_email):
    import time
    from storage import device_catalog, device_health_tickets as tickets
    from tests.test_device_health_cleanup import phone
    from tests.test_instance_device_registry import client, report
    store = MobileStore()
    identity = subject_identity(live_email or '', 'owner')
    paired, _ = new_pair(store, subject_identity('old@example.test', 'owner'))
    cookie, viewer, target = phone(install, 'host-health-phone')
    token = tickets.issue(store, {'id': paired['device_id']}, viewer[0])['health_binding_ticket']
    assert report(client(cookie), 'host-health-phone', health_binding_ticket=token).status_code == 200
    local = (viewer[0], None, True, None)
    with get_cached_sqlite_engine().begin() as conn:
        rows, active, unlinked = device_catalog.snapshot(conn, local, identity['id'], live_email,
                                                        int(time.time()), health_identity=identity)
        assert active[target] == [paired['device_id']] and unlinked == []
        funcs = {f['kind']: f for f in rows[target]['functions']}
        assert funcs['health']['can_change']
        assert not funcs['schedule']['can_change']  # General permissions retain their existing rule.
        result = device_catalog.change(conn, local, identity['id'], live_email, target, 'health', False,
                                       int(time.time()), health_identity=identity)
        assert result['function']['on'] is False
    assert store.active_device() is None


def test_deferred_signout_retries_subject_absence_until_live_proof_returns(store, mobile_http, monkeypatch):
    import time
    from sqlalchemy import select
    from storage import device_health_owner_checks as worker
    from storage.models import device_health_owner_checks as checks
    from vibe.account_session_revoke import pairing_epoch
    _, projection = mobile_http
    paired, _ = new_pair(store, subject_identity())
    link_device(paired['device_id'])
    config = V2Config.load()
    now = int(time.time())
    with get_cached_sqlite_engine().begin() as conn:
        worker.queue(conn, binding=device_binding(config), epoch=pairing_epoch(config), subject=SUBJECT,
                     device='synthetic-phone', email=IDENTITY['owner'], now=now)
    assert worker.run_once(lambda: config) is False
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(checks.c.mobile_id)) == paired['device_id']
    projection['access']['owner'] = {'user_id': SUBJECT, 'email': None}
    monkeypatch.setattr(worker.time, 'time', lambda: now + 31)
    assert worker.run_once(lambda: config)
    assert store.active_device() is None


def test_unswept_expired_legacy_request_does_not_block_subject_status(store, mobile_http, clock):
    from datetime import timedelta
    _, projection = mobile_http
    req, _ = store.create_request('health.connect', IDENTITY)
    clock[0] += timedelta(days=2)
    projection['access']['owner']['user_id'] = SUBJECT
    result = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'status']))
    assert result['read_allowed'] and not result['connected']
    assert store.show_request(req['id'])['state'] == 'expired'


def test_unproven_legacy_phone_disconnect_is_blocked_but_cli_revoke_recovers(store, mobile_http):
    from datetime import date, timedelta
    client, projection = mobile_http
    paired, _ = pair_device(store)
    store.upload(device_for(store, paired), upload_payload())
    projection['access']['owner']['user_id'] = SUBJECT
    bearer = {'Authorization': 'Bearer ' + paired['credential']}
    response = client.delete('/api/mobile/device/v1/', headers=bearer)
    assert response.status_code == 503
    assert response.get_json()['details']['reason'] == 'legacy_owner_unproven'
    assert store.active_device() and counts(store)[:2] == (2, 2)
    with pytest.raises(MobileError) as error:
        cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'connect', '--no-callback']))
    assert error.value.details['reason'] == 'legacy_owner_unproven'
    result = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'revoke']))
    assert result['purged']
    connect = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'connect', '--no-callback']))
    identity = mobile_health.current_identity(store)
    code = store.issue_pairing(connect['request']['id'], identity)
    response = client.post('/api/mobile/device/v1/pair', json=dict(
        pairing_code=code['pairing_code'], device_name='Recovered synthetic phone',
        platform='ios', app_version='1', metrics=['steps']))
    assert response.status_code == 200
    bearer = {'Authorization': 'Bearer ' + response.get_json()['credential']}
    backfill = upload_payload(names=['steps'])
    start, end = date(2026, 10, 20), date(2026, 11, 18)
    backfill['window']['from'] = start.isoformat()
    backfill['items'] = []
    for offset in range(30):
        day = (start + timedelta(days=offset)).isoformat()
        item = upload_payload(names=['steps'], day=day)['items'][0]
        item['latest_sample_end_at'] = day + 'T07:00:00+08:00'
        item['closed'] = day != end.isoformat()
        backfill['items'].append(item)
    assert client.put('/api/mobile/device/v1/health/days', json=backfill, headers=bearer).status_code == 200
    status = cli_mobile.execute(cli.build_parser().parse_args(['mobile', 'health', 'status']))
    assert status['read_allowed'] and status['connected']
    read = cli_mobile.execute(cli.build_parser().parse_args([
        'mobile', 'health', 'read', '--metric', 'steps', '--from', start.isoformat(), '--to', end.isoformat()]))
    items = read['metrics']['steps']['items']
    assert len(items) == 30 and all(item['availability'] == 'ok' for item in items)


@pytest.mark.parametrize('command', ['status', 'read'])
def test_legacy_owner_cli_has_explicit_recovery_action(store, mobile_http, capsys, command):
    import json
    from vibe.i18n import t
    _, projection = mobile_http
    pair_device(store)
    projection['access']['owner']['user_id'] = SUBJECT
    config = V2Config.load()
    config.language = 'zh'
    config.save()
    args = ['mobile', 'health', command] + (['--metric', 'steps'] if command == 'read' else [])
    assert cli_mobile.cmd_mobile(cli.build_parser().parse_args(args)) == (1 if command == 'read' else 0)
    result = json.loads(capsys.readouterr().out)
    diagnostic = result['diagnostic'] if command == 'status' else result
    assert diagnostic['next_action'] == 'revoke_then_connect'
    assert diagnostic['error'] == t('mobile.health.owner.legacy_owner_unproven', 'en')


@pytest.mark.parametrize('history', [0, 3000])
def test_owner_migration_work_is_bounded_by_live_records(store, mobile_http, monkeypatch, history, record_property):
    """Count SQLite VM work, not machine-dependent timing, while the registry lock is held."""
    from contextlib import contextmanager
    from sqlalchemy import event
    _, projection = mobile_http
    paired, _ = pair_device(store)
    link_device(paired['device_id'])
    projection['access']['owner']['user_id'] = SUBJECT
    with store.transaction() as conn:
        # Both unproven legacy history and already-migrated history must be irrelevant.
        conn.executemany(
            "INSERT INTO requests(id,operation,instance_id,owner,owner_subject,metrics,state,created_at,"
            "expires_at,ended_at,caller) VALUES(?,'health.connect',?,?,?,?, 'canceled',?,?,?,'fixture')",
            [(f'history-{i}', IDENTITY['id'], IDENTITY['owner'], SUBJECT if i % 2 else None,
              '[]', '2026-11-01', '2026-11-02', '2026-11-02') for i in range(history)])
        conn.executemany("INSERT INTO pairings(code_hash,request_id,instance_id,owner,expires_at) VALUES(?,?,?,?,?)",
                         [(f'old-code-{i}', f'history-{i}', IDENTITY['id'], IDENTITY['owner'], '2026-11-02')
                          for i in range(history)])
    binding = device_binding(V2Config.load())
    if history:
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(insert(device_health_links), [
                dict(mobile_id=f'old-registry-{i}', binding=binding,
                     subject=SUBJECT, device_id='retired-phone', linked_at='2026-11-01')
                for i in range(history)])
    original_transaction = store.transaction
    vm_steps = [0]
    statements = []
    registry_statements = []

    def progress():
        vm_steps[0] += 100
        # Stop pathological old code promptly; a per-call budget does not grow with history.
        return int(vm_steps[0] > 12000)

    @contextmanager
    def measured():
        with original_transaction() as conn:
            conn.set_progress_handler(progress, 100)
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
                conn.set_trace_callback(None)

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if 'device_health_links' in statement and statement.lstrip().upper().startswith('SELECT'):
            registry_statements.append((statement, parameters))

    monkeypatch.setattr(store, 'transaction', measured)
    engine = get_cached_sqlite_engine()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert mobile_health.current_identity(store)['owner_subject'] == SUBJECT
        first_steps = vm_steps[0]
        with engine.connect() as conn:
            for sql, parameters in registry_statements:
                plan = [r[3] for r in conn.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql, parameters)]
                assert any('SEARCH device_health_links' in detail and 'mobile_id=?' in detail
                           for detail in plan), plan
        vm_steps[0] = 0
        statements.clear()
        registry_statements.clear()
        assert mobile_health.current_identity(store)['owner_subject'] == SUBJECT
        with original_transaction() as conn:
            plans = [(sql, [r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + sql)])
                     for sql in statements if sql.lstrip().upper().startswith(('SELECT', 'UPDATE', 'DELETE'))]
        if vm_steps[0] >= 2500:
            pytest.fail(f"{vm_steps[0]} VM steps:\n" + "\n".join(f"{sql}: {plan}" for sql, plan in plans))
        assert first_steps < 12000
        assert registry_statements == []  # No migration means no registry history lookup.
        for sql, plan in plans:
            assert any('SEARCH' in detail for detail in plan), (sql, plan)
            assert not any('SCAN ' in detail or 'TEMP B-TREE' in detail for detail in plan), (sql, plan)
        record_property('migration_vm_steps', first_steps)
        record_property('steady_vm_steps', vm_steps[0])
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
