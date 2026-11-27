"""Signed fixture origin, independently of the hosted qualification evidence."""
import json

import pytest

from scripts import model_hub_engine_manifest_publish as publish
from tests.engine_channel_support import LocalVerifier, Signer, envelope_bytes, manifest_bytes, payload_document, release_entry, tombstone

channel = publish.channel
A, B = ('model-hub-engine-v8.0.63-1', 'model-hub-engine-v8.0.63-2')


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
