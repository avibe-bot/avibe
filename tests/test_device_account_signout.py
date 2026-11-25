"""Exercise the real local commit and worker against a fake issuer transport."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from storage import device_registry
from storage.db import get_cached_sqlite_engine
from storage.device_account_signout import recent
from storage.importer import ensure_sqlite_state
from storage.models import remote_account_revocations as jobs, remote_account_refs as refs, remote_device_sessions as sessions
from tests.test_remote_access_vibe_cloud import _config
from vibe import account_session_revoke as cloud, account_signout_worker as worker, remote_access

A = 'asr1_' + 'a' * 43
B = 'asr1_' + 'B' * 43


@pytest.fixture
def paired(monkeypatch):
    ensure_sqlite_state()
    cfg = _config()
    cfg.remote_access.vibe_cloud.backend_url = 'https://issuer.invalid'
    cfg.remote_access.vibe_cloud.instance_secret = 'test-device-secret'
    clock = [1000000]
    monkeypatch.setattr(worker, 'time', SimpleNamespace(time=lambda: clock[0]))
    binding = remote_access.device_binding(cfg)
    def enroll(sid, ref, *, subject='owner'):
        device_registry.record_session(binding, {'browser_session_id': sid, 'auth_time': clock[0], 'sub': subject,
            'email': 'u@example.invalid', 'exp': clock[0]+600}, 'owner', {'kind': 'browser'},
            expires_at=clock[0]+86400, now=clock[0], fresh=True, epoch=cloud.pairing_epoch(cfg), reference=ref)
        with get_cached_sqlite_engine().connect() as conn:
            return conn.scalar(select(sessions.c.device_id).where(sessions.c.session_id == sid))
    def stop(target, **kw):
        return device_registry.sign_out(binding, 'owner', owner=True, caller='B', device_id=target, lifetime=86400,
                                        now=clock[0], delegate=kw.get('delegate', True))
    def read():
        with get_cached_sqlite_engine().connect() as conn:
            return recent(conn, binding, 'owner', True, clock[0])
    return cfg, clock, enroll, stop, read


def fake_issuer(monkeypatch, code, *, addresses=('8.8.8.8',), offline=()):
    import ipaddress
    calls = []
    class Connection:
        def __init__(self, address):
            self.address = address
        def connect(self):
            if self.address in offline:
                raise OSError('fixture_unreachable')
        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))
        def getresponse(self):
            if code is None:
                raise TimeoutError()
            return SimpleNamespace(status=code)
        def close(self):
            pass
    monkeypatch.setattr(remote_access, '_resolve_pairing_backend_addresses', lambda *_: tuple(ipaddress.ip_address(a) for a in addresses))
    monkeypatch.setattr(remote_access, '_validated_backend_proxy_url', lambda *_: None)
    monkeypatch.setattr(remote_access, '_validated_backend_connection', lambda host, *_: Connection(host))
    return calls


@pytest.mark.parametrize(('code', 'status', 'reason'), [
    (204, 'accepted', None), (400, 'refused', 'protocol'), (401, 'refused', 'credential'),
    (403, 'not_applicable', 'not_eligible'), (415, 'refused', 'protocol'),
    (429, 'pending', 'retrying'), (503, 'pending', 'retrying'), (None, 'pending', 'retrying'),
    (302, 'refused', 'protocol'), (404, 'refused', 'protocol'), (409, 'refused', 'protocol'),
    (422, 'refused', 'protocol'), (200, 'pending', 'retrying'),
])
def test_local_commit_then_exact_request_and_durable_result(paired, monkeypatch, code, status, reason):
    cfg, clock, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    calls = fake_issuer(monkeypatch, code)
    ids, count, [result], _ = stop(None)
    assert ids == ['A'] and count == 1 and result['device_id'] == target and calls == []
    assert result['account_results'] == [{'status': 'pending', 'reason': 'queued', 'count': 1}]
    assert read() == [result]
    # When this Avibe ended it, read back unchanged, so a page can tell a long wait from a new one.
    assert result['signed_out_at'] == clock[0]
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(sessions.c.session_id)).scalars().all() == ['B']
        assert conn.scalar(select(refs.c.reference)) == B
    assert worker.run_once(lambda: cfg)
    assert calls == [('POST', '/api/v1/instances/inst_123/account-sessions/revoke', {'account_session_ref': A},
                      {'Host': 'issuer.invalid', 'Content-Type': 'application/json', 'X-Vibe-Device-Secret': 'test-device-secret'})]
    public = read()
    assert public[0]['account_results'] == [{'status': status, 'reason': reason, 'count': 1}]
    assert A not in json.dumps(public) and B not in json.dumps(public)
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(jobs.c.reference)) == (A if status == 'pending' else None)
    with pytest.raises(PermissionError, match='account_session_signed_out'):
        enroll('post-commit', A)
    assert not worker.run_once(lambda: cfg)
    if code == 429:
        clock[0] += 599
        assert not worker.run_once(lambda: cfg)
        clock[0] += 1
        assert worker.run_once(lambda: cfg)


@pytest.mark.parametrize(('keeper', 'target', 'reason'), [(None, A, 'keeper_unknown'), (A, A, 'shared_sign_in'),
                                                           (B, None, 'no_reference')])
def test_bulk_never_submits_a_possible_keeper(paired, monkeypatch, keeper, target, reason):
    cfg, _, enroll, stop, _ = paired
    enroll('B', keeper)
    enroll('A', target)
    _, count, [result], _ = stop(None)
    assert count == 1 and result['account_results'] == [{'status': 'not_applicable', 'reason': reason, 'count': 1}]
    calls = fake_issuer(monkeypatch, 204)
    assert not worker.run_once(lambda: cfg) and not calls


@pytest.mark.parametrize('second', ['pending', 'accepted', 'unconfirmed'])
def test_explicit_lost_device_signout_handles_shared_noncaller_and_later_sibling(paired, monkeypatch, second):
    cfg, clock, enroll, stop, read = paired
    enroll('B', B)
    target = enroll('lost', A)
    sibling = enroll('sibling', A)
    stop(target)
    calls = fake_issuer(monkeypatch, 503 if second == 'unconfirmed' else 204)
    if second == 'unconfirmed':
        clock[0] += 7 * 86400
        # Keep the sibling's independent local enrollment live through the exhaustion test.
        with get_cached_sqlite_engine().begin() as conn:
            conn.execute(sessions.update().where(sessions.c.session_id.in_(['B', 'sibling'])).values(expires_at=clock[0]+600, cookie_exp=clock[0]+600))
            conn.execute(refs.update().where(refs.c.sid.in_(['B', 'sibling'])).values(expires_at=clock[0]+600))
    if second != 'pending':
        assert worker.run_once(lambda: cfg)
    _, _, [result], _ = stop(sibling)
    assert result['account_results'][0]['status'] == ('accepted' if second == 'accepted' else 'pending')
    if second == 'unconfirmed':
        assert any(r['account_results'][0]['status'] == 'unconfirmed' for r in read())
    worker.run_once(lambda: cfg)
    assert len(calls) == (2 if second == 'unconfirmed' else 1)


def test_repair_drops_old_authority_and_never_dispatches_to_replacement(paired, monkeypatch):
    cfg, _, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    from vibe.binding_admission import reset_intent_path
    marker = reset_intent_path()
    marker.write_text('{')
    calls = fake_issuer(monkeypatch, 204)
    assert not worker.run_once(lambda: cfg) and not calls
    assert read()[0]['account_results'][0]['status'] == 'pending'
    cfg.remote_access.vibe_cloud.instance_secret = 'replacement-pairing'
    marker.unlink()
    assert not worker.run_once(lambda: cfg) and not calls
    assert read()[0]['account_results'][0]['reason'] == 'binding_changed'
    with get_cached_sqlite_engine().connect() as conn:
        assert not conn.scalar(select(refs.c.reference)) and not conn.scalar(select(jobs.c.reference))


def test_generic_query_cannot_read_any_account_reference_bearing_table(paired):
    from storage.models import metadata
    from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
    _, _, enroll, stop, _ = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    names = [t.name for t in metadata.tables.values() if t.name.startswith('remote_account_') and 'reference' in t.c]
    assert set(names) >= {'remote_account_refs', 'remote_account_revocations'}
    for name in names:
        with pytest.raises(ReadOnlyQueryError):
            run_read_only_query(f'SELECT reference FROM {name}', page_request=None)


def test_expired_lease_replays_without_budget_reset_or_late_overwrite(paired, monkeypatch):
    cfg, clock, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    digest, _, _, first = worker._claim(cfg, clock[0])
    assert worker._claim(cfg, clock[0]) is None
    clock[0] += worker.LEASE
    calls = fake_issuer(monkeypatch, 204)
    assert worker.run_once(lambda: cfg) and len(calls) == 1
    worker._settle(digest, first, 401, clock[0])
    assert read()[0]['account_results'][0]['status'] == 'accepted'


def test_unusable_dns_preserves_work_and_retries_all_validated_addresses(paired, monkeypatch):
    cfg, clock, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    blocked = fake_issuer(monkeypatch, 204, addresses=('10.0.0.1',))
    for _ in range(33):
        assert worker.run_once(lambda: cfg)
        clock[0] += 3600
    assert not blocked and read()[0]['account_results'][0]['status'] == 'pending'
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.execute(select(jobs.c.answers, jobs.c.reference)).one() == (0, A)
    calls = fake_issuer(monkeypatch, 204, addresses=('8.8.8.8', '1.1.1.1'), offline=('8.8.8.8',))
    assert worker.run_once(lambda: cfg) and len(calls) == 1
    assert read()[0]['account_results'][0]['status'] == 'accepted'


def test_explicit_answer_budget_only_counts_replies(paired, monkeypatch):
    cfg, clock, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    no_reply = fake_issuer(monkeypatch, None, addresses=('8.8.8.8', '1.1.1.1'))
    worker.run_once(lambda: cfg)
    assert len(no_reply) == 1  # Bytes may have reached the issuer. No immediate replay on the next address.
    calls = fake_issuer(monkeypatch, 503)
    for _ in range(31):
        clock[0] += 3600
        worker.run_once(lambda: cfg)
    assert read()[0]['account_results'][0]['status'] == 'pending'
    clock[0] += 3600
    worker.run_once(lambda: cfg)
    assert len(calls) == 32 and read()[0]['account_results'][0]['status'] == 'unconfirmed'


def test_age_limit_is_unconfirmed_and_erases_ref(paired, monkeypatch):
    cfg, clock, enroll, stop, read = paired
    target = enroll('A', A)
    enroll('B', B)
    stop(target)
    calls = fake_issuer(monkeypatch, None)
    assert worker.run_once(lambda: cfg)
    clock[0] += 9 * 86400
    assert read()[0]['account_results'][0]['status'] == 'pending'
    worker.run_once(lambda: cfg)
    assert len(calls) == 2
    assert read()[0]['account_results'] == [{'status': 'unconfirmed', 'reason': 'retry_exhausted', 'count': 1}]
    with get_cached_sqlite_engine().connect() as conn:
        assert conn.scalar(select(jobs.c.reference)) is None


def test_a_recorded_role_leaves_with_its_receipt(paired, monkeypatch):
    from sqlalchemy import event

    from storage.models import remote_signout_receipt_roles as roles, remote_signout_receipts as receipts

    cfg, clock, enroll, stop, read = paired
    target = enroll('A', None)
    enroll('B', B)
    _, _, [result], _ = stop(target, delegate=False)
    assert result['group'] == 'mine' and read() == [result]
    monkeypatch.setattr(worker, '_ROLE_PAGE', 2)
    monkeypatch.setattr(worker, '_roles_checked_to', '')  # as after a start
    with get_cached_sqlite_engine().begin() as conn:
        assert conn.execute(select(roles.c.receipt, roles.c.login_role)).all() == [(result['operation_id'], 'owner')]
        # Roles whose receipts an earlier version already removed.
        conn.execute(roles.insert(), [dict(receipt=f'left-{n}', login_role='member') for n in range(5)])
    # Even orphan-only maintenance is deferred while reset owns the store.
    from vibe.binding_admission import reset_intent_path
    marker = reset_intent_path()
    marker.write_text('{}')
    try:
        assert worker.run_once(lambda: pytest.fail('worker passed the reset fence')) is False
        assert worker._roles_checked_to == ''
        with get_cached_sqlite_engine().connect() as conn:
            assert len(conn.execute(select(roles.c.receipt)).all()) == 6
    finally:
        marker.unlink()
    fake_issuer(monkeypatch, 204)
    engine, statements = get_cached_sqlite_engine(), []

    def record(_conn, _cursor, statement, parameters, *_):
        if 'remote_signout_receipt_roles' in statement:
            statements.append((statement, parameters))

    event.listen(engine, 'before_cursor_execute', record)
    try:
        passes = 0
        while worker._roles_checked_to is not None:
            worker.run_once(lambda: cfg)
            passes += 1
        ran = len(statements)
        with engine.connect() as conn:
            assert conn.execute(select(roles.c.receipt)).scalars().all() == [result['operation_id']]
        del statements[ran:]  # the check above is the test's own
        # Six roles, two keys at a step, and a last step that finds the end; then the table is not read for this again.
        assert passes == 4
        clock[0] += 7 * 86400 + 1
        worker.run_once(lambda: cfg)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    with engine.connect() as conn:
        assert conn.execute(select(receipts.c.id)).all() == [] and conn.execute(select(roles.c.receipt)).all() == []
        # Every statement a tick runs on the table reaches it by key.
        kinds = {statement.split()[0].upper() for statement, _ in statements}
        assert kinds == {'SELECT', 'DELETE'}
        for statement, parameters in statements:
            plan = [row[-1] for row in conn.exec_driver_sql('EXPLAIN QUERY PLAN ' + statement, parameters)]
            assert plan and not any(step.startswith('SCAN remote_signout_receipt') for step in plan), (statement, plan)
            assert not any('TEMP B-TREE' in step for step in plan), (statement, plan)
    assert read() == []


@pytest.mark.no_sqlite_template
def test_signed_out_at_is_the_local_sign_out_time_and_survives_later_reads(paired, monkeypatch):
    cfg, clock, enroll, stop, read = paired
    enroll('A', A)
    enroll('B', B)
    fake_issuer(monkeypatch, 503)
    signed_out = clock[0]
    _, _, [result], _ = stop(None)
    assert result['signed_out_at'] == signed_out
    clock[0] += 86400 + 1
    assert worker.run_once(lambda: cfg)
    [later] = read()
    assert later['signed_out_at'] == signed_out
    assert later['account_results'] == [{'status': 'pending', 'reason': 'retrying', 'count': 1}]


def test_receipt_role_migration_round_trip_keeps_existing_receipts(tmp_path):
    import sqlite3

    from alembic import command

    from storage import migrations

    path = tmp_path / "registry.sqlite"
    previous, head = "20261121_0097", "20261122_0098"
    migrations.run_migrations(path, revision=previous)
    row = ("receipt-1", "binding", "person", "device", "browser", "Chrome", 5, 50)

    def kept(db):
        return (db.execute("SELECT * FROM remote_signout_receipts").fetchall(),
                db.execute("SELECT receipt,status,count FROM remote_signout_items").fetchall(),
                db.execute("SELECT sql FROM sqlite_master WHERE name LIKE '%signout_receipts' OR tbl_name = 'remote_signout_receipts' "
                           "ORDER BY name").fetchall())

    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO remote_signout_receipts(id,binding,subject,device_id,kind,display_name,created_at,expires_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", row)
        db.execute("INSERT INTO remote_signout_items(receipt,status,reason,count) VALUES ('receipt-1','accepted',NULL,1)")
        before = kept(db)
    assert before[0] == [row]
    for _ in range(2):  # and again after a downgrade
        migrations.run_migrations(path, revision=head)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
            # The receipts table is untouched; a receipt written before says nothing about the role.
            assert kept(db) == before
            assert db.execute("SELECT receipt, login_role FROM remote_signout_receipt_roles").fetchall() == []
            db.execute("INSERT INTO remote_signout_receipt_roles(receipt, login_role) VALUES ('receipt-1', 'owner')")
        command.downgrade(migrations.alembic_config(path), previous)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (previous,)
            assert kept(db) == before
            assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%receipt_roles%'").fetchall() == []


@pytest.mark.no_sqlite_template
def test_receipt_role_migration_replays_on_a_database_that_already_has_the_table(tmp_path):
    from storage import migrations
    from storage.db import create_sqlite_engine
    from storage.models import metadata

    path = tmp_path / "head-shape.sqlite"
    engine = create_sqlite_engine(path)
    metadata.create_all(engine)
    engine.dispose()
    migrations.run_migrations(path)
    migrations.run_migrations(path)
