"""Exercise both origin winners using reviewed Git bytes and a fresh client state file."""
import json
import os
from datetime import datetime, timezone

import pytest

from scripts import model_hub_engine_index as index
from tests.engine_channel_support import LocalVerifier, Signer, envelope_bytes, manifest_bytes
from tests.test_model_hub_engine_hosted import commit, fixture
from tests.test_model_hub_engine_index_recovery import append


@pytest.mark.parametrize('promotion_wins', [False, True], ids=['withdrawal-wins', 'promotion-wins'])
def test_origin_race_reads_back_client_and_next_allocation(tmp_path, promotion_wins):
    if os.environ.get('AVIBE_TEST_ISOLATED_ENGINE_RACE') != '1':
        pytest.skip('throwaway signatures only in isolated runner')
    repo, _, _, _, _, raw = fixture(tmp_path)
    A, B, C = ['model-hub-engine-v8.0.63-' + str(i) for i in (2, 3, 4)]
    promote = lambda n: index.producer.Promote(manifest_bytes(build=n), '3.2.6')
    before, parent = append(repo, 2, promote(3), index.channel.parse_payload(raw))
    third, pending = append(repo, 3, promote(4), parent)
    op = index.producer.Revoke(B, A, 'emergency withdrawal')
    def ref(p): return {'sequence': p.sequence, 'sha256': p.sha256}
    def emergency(n, base, retire):
        body = index.producer.plan_payload(base, op, sequence=n, now=datetime(2026, 1, 4, tzinfo=timezone.utc))
        p = index.channel.parse_payload(body)
        (repo / index.emergency.ledger_path(n)).write_bytes(body)
        path = repo / index.emergency.path(n)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'effective_parent': ref(base), 'retire': [ref(x) for x in retire], 'payload_sha256': p.sha256}))
        return commit(repo), p
    fourth, withdrawal = emergency(4, parent, [pending])
    def plan(b, e): return index.delivery_plan(index.candidates(repo, b, e), lambda row: None)[0]
    original = plan(before, third)
    rows = plan(before, fourth)
    assert [r[4].sequence for r in rows] == [4]
    signer, verifier = Signer(), LocalVerifier()
    roots = signer.roots()
    signed = {p.sequence: envelope_bytes(signer, p.raw) for p in (parent, pending, withdrawal)}
    class Bucket:
        stored = signed[2]
        version = 0
        def read(self): return self.stored, str(self.version)
        def replace(self, body, *, etag):
            assert etag == str(self.version)
            self.stored = body
            self.version += 1
    bucket = Bucket()
    store = index.channel.ChannelStateStore(tmp_path / 'client')
    def client(body):
        env, p = index.channel.authenticate(body, roots=roots, verifier=verifier)
        store.accept(env, p, floor=1)
        return store.cached(roots=roots, verifier=verifier).payload
    client(bucket.stored)
    def publish(items, fetch=None):
        index.publish_batch(items, bucket=bucket, roots=roots, verifier=verifier,
                            envelope_for=lambda p: signed[p.sequence], fetch_public=fetch or (lambda: bucket.stored))
    if promotion_wins:
        # A successful conditional origin write can precede any public readback.
        def interrupted(): raise TimeoutError('not public yet')
        with pytest.raises(TimeoutError): publish(original, interrupted)
        with pytest.raises(index.hosted.Error): publish(rows, lambda: signed[2])
        assert bucket.stored == signed[3] and bucket.version == 1
        publish(original)
        assert client(bucket.stored).release(C) is not None
        fifth, actual = emergency(5, pending, [withdrawal])
        index.preflight(repo, fourth, fifth, api=None, token=None, single=True)
        signed[5] = envelope_bytes(signer, actual.raw)
        publish(plan(fourth, fifth))
        with pytest.raises(index.hosted.Error): publish(rows)
        final = client(bucket.stored)
        assert final.sequence == 5 and final.release(C) and bucket.version == 2
    else:
        index.preflight(repo, third, fourth, api=None, token=None, single=True)
        publish(rows)
        final = client(bucket.stored)
        assert final.sequence == 4 and final.release(C) is None
        with pytest.raises(index.hosted.Error): publish(original)
        with pytest.raises(index.channel.ChannelError, match='older'): client(signed[3])
        publish(rows)
        assert bucket.version == 1
        follow, successor = append(repo, 5, promote(5), withdrawal)
        signed[5] = envelope_bytes(signer, successor.raw)
        publish(plan(fourth, follow))
        assert client(bucket.stored).tombstone(B) and bucket.version == 2
    resolved = index.channel.resolve(final, hold=None, avibe_version='3.2.7', host_platform='linux-arm64', bundled=A, current=B)
    assert resolved.action == index.channel.ResolutionAction.WITHDRAW and resolved.target.asset_release_tag == A
