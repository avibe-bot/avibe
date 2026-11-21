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


def fake_issuer(monkeypatch, code):
    calls = []
    class Connection:
        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))
        def getresponse(self):
            if code is None:
                raise TimeoutError()
            return SimpleNamespace(status=code)
        def close(self):
            pass
    monkeypatch.setattr(remote_access, '_resolve_pairing_backend_addresses', lambda *_: (__import__('ipaddress').ip_address('8.8.8.8'),))
    monkeypatch.setattr(remote_access, '_validated_backend_proxy_url', lambda *_: None)
    monkeypatch.setattr(remote_access, '_validated_backend_connection', lambda *_: Connection())
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
