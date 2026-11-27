"""Signed fixture origin, independently of the hosted qualification evidence."""
import json

import pytest

from scripts import model_hub_engine_manifest_publish as publish
from tests.engine_channel_support import LocalVerifier, Signer, envelope_bytes, manifest_bytes, payload_document, release_entry, tombstone

channel = publish.channel
A, B = ('model-hub-engine-v8.0.63-1', 'model-hub-engine-v8.0.63-2')


@pytest.fixture
def revision(tmp_path):
    path = tmp_path / publish.MANIFEST
    path.parent.mkdir()
    publish.git(tmp_path, 'init', '-q')
    def commit(sequence, entries, revoked):
        payload = payload_document(sequence, releases=entries, revoked=revoked)
        path.write_text(json.dumps({'payload': payload, 'rehearsals': {}}))
        publish.git(tmp_path, 'add', '.')
        publish.git(tmp_path, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        return publish.git(tmp_path, 'rev-parse', 'HEAD').decode().strip()
    return commit


@pytest.mark.parametrize('change', ['omit', 'withdraw', 'drop tombstone'])
def test_preflight_requires_explicit_permanent_retirement(tmp_path, monkeypatch, revision, change):
    a, b = [release_entry(manifest_bytes(build=i)) for i in (1, 2)]
    withdrawal = [tombstone(B, A)]
    retired = change == 'drop tombstone'
    base = revision(1, [a] if retired else [a, b], withdrawal if retired else [])
    head = revision(2, [a], withdrawal if change == 'withdraw' else [])
    checked = []
    monkeypatch.setattr(publish.q, 'require_published', lambda record, _: checked.append(record.name))
    if change == 'withdraw':
        publish.preflight(tmp_path, base, head, harness=tmp_path)
        assert checked == [A]
    else:
        with pytest.raises((publish.Error, channel.ChannelError), match='withdrawal'):
            publish.preflight(tmp_path, base, head, harness=tmp_path)
        assert not checked


class Origin:
    def __init__(self):
        self.stored = None
        self.puts = 0

    def read(self):
        return (self.stored, str(self.puts)) if self.stored is not None else None

    def replace(self, body, *, etag):
        if etag != (str(self.puts) if self.stored is not None else None):
            raise publish.Error('conditional write lost')
        self.stored = body
        self.puts += 1


def test_withdraw_historical_fallback_without_offering_it(tmp_path, monkeypatch, revision):
    a, b, c = [release_entry(manifest_bytes(build=i)) for i in (1, 2, 3)]
    C = 'model-hub-engine-v8.0.63-3'
    first = revision(1, [a, b, c], [])
    retired_b = revision(2, [a, c], [tombstone(B, A)])
    retired_a = revision(3, [a, c], [tombstone(B, A), tombstone(A, C)])
    checked = []
    monkeypatch.setattr(publish.q, 'require_published', lambda record, _: checked.append(record.name))
    publish.preflight(tmp_path, first, retired_b, harness=tmp_path)
    assert checked == [A, C]
    checked.clear()
    publish.preflight(tmp_path, retired_b, retired_a, harness=tmp_path)
    assert checked == [C]

    signer, verifier, bucket = Signer(), LocalVerifier(), Origin()
    # Origin missed B's withdrawal; do not mistake its historical record for a new decision.
    bucket.stored = envelope_bytes(signer, publish.manifest(tmp_path, first)[0].raw)
    desired, _ = publish.manifest(tmp_path, retired_a)
    checked.clear()
    publish.write_index(desired, envelope_bytes(signer, desired.raw), bucket=bucket, roots=signer.roots(),
                        verifier=verifier, public=lambda: bucket.stored, assert_current=lambda: None)
    assert checked == [C]
    _, received = channel.authenticate(bucket.stored, roots=signer.roots(), verifier=verifier)
    def resolve(installed):
        return channel.resolve(received, bundled=A, current=installed, hold=None, avibe_version='3.2.8', host_platform='linux-amd64')
    assert (resolve(A).action.value, resolve(A).target.asset_release_tag) == ('withdraw', C)
    blocked = resolve(B)
    assert blocked.action.value == 'blocked' and blocked.target is None and not blocked.download_authorized

    bad = revision(4, [a, c], [tombstone(B, A), tombstone(A, C), tombstone('model-hub-engine-v8.0.63-4', A)])
    checked.clear()
    with pytest.raises(publish.Error, match='withdrawn fallback'):
        publish.preflight(tmp_path, retired_a, bad, harness=tmp_path)
    assert not checked


def test_withdrawal_survives_a_missed_promotion_upload(monkeypatch):
    signer, bucket = Signer(), Origin()
    a, b = [release_entry(manifest_bytes(build=i), minimum='3.2.8') for i in (1, 2)]
    def make(seq, entries, revoked=()):
        return channel.parse_payload(json.dumps(payload_document(seq, releases=entries, revoked=list(revoked))).encode())
    origin, merged = make(1, [a]), make(2, [a, b])
    desired = make(3, [a], [tombstone(B, A)])
    publish.successor(merged, desired)
    with pytest.raises(publish.Error, match='newer Avibe'):
        publish.successor(origin, desired)  # packaged-only B still needs a 3.2.6 fallback at merge
    bucket.stored = envelope_bytes(signer, origin.raw)
    monkeypatch.setattr(publish.q, 'require_published', lambda *_: None)
    publish.write_index(desired, envelope_bytes(signer, desired.raw), bucket=bucket, roots=signer.roots(),
                        verifier=LocalVerifier(), public=lambda: bucket.stored, assert_current=lambda: None)
    assert bucket.puts == 1 and channel.authenticate(bucket.stored, roots=signer.roots(), verifier=LocalVerifier())[1].raw == desired.raw


def test_promotion_then_withdrawal_reaches_client_via_authenticated_origin(tmp_path, monkeypatch):
    signer, verifier, bucket = Signer(), LocalVerifier(), Origin()
    roots = signer.roots()
    state = channel.ChannelStateStore(tmp_path / 'client')
    a, b = [release_entry(manifest_bytes(build=i)) for i in (1, 2)]
    def make(n, entries, revoked=()):
        return channel.parse_payload(json.dumps(payload_document(n, releases=entries, revoked=list(revoked)),
                                                sort_keys=True, separators=(',', ':')).encode())
    live = make(2, [a, b]); withdrawal = make(3, [a], [tombstone(B, A)])
    checked = []
    def available(path, _):
        checked.append(publish.q.load_candidate(path).asset_release_tag)
    monkeypatch.setattr(publish.q, 'require_published', available)
    def upload(payload, public=lambda: bucket.stored, race=lambda: None):
        publish.write_index(payload, envelope_bytes(signer, payload.raw), bucket=bucket, roots=roots,
                            verifier=verifier, public=public, assert_current=race)
    def client(installed):
        env, payload = channel.authenticate(bucket.stored, roots=roots, verifier=verifier)
        state.accept(env, payload, floor=0)
        resolved = channel.resolve(payload, bundled=A, current=installed, hold=None, avibe_version='3.2.6', host_platform='linux-amd64')
        return resolved.action.value, resolved.target.asset_release_tag
    upload(live)
    assert client(A) == ('adopt', B) and checked == [A, B]
    with pytest.raises(publish.Error, match='explicit withdrawal'):
        upload(make(3, [a]))
    with pytest.raises(publish.Error, match='higher sequence'):
        upload(make(2, [a], [tombstone(B, A)]))
    checked.clear(); stale = bucket.stored
    with pytest.raises(publish.Error, match='pending'):
        upload(withdrawal, public=lambda: stale)
    assert checked == [A] and bucket.puts == 2  # withdrawn B was never fetched
    upload(withdrawal)
    assert client(B) == ('withdraw', A) and bucket.puts == 2
    with pytest.raises(channel.ChannelError, match='withdrawal'):
        upload(make(4, [a, b]))
    with pytest.raises(publish.Error, match='conditional'):
        upload(make(4, [a], [tombstone(B, A)]), race=lambda: setattr(bucket, 'puts', 5))
    assert channel.authenticate(bucket.stored, roots=roots, verifier=verifier)[1].raw == withdrawal.raw
