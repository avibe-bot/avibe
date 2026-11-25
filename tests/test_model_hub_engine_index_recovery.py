"""Temporary Git histories only; no hosted identities, signing keys or network."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts import model_hub_engine_index as index
from tests.engine_channel_support import manifest_bytes
from tests.test_model_hub_engine_hosted import commit, fixture


def append(repo, number, op, parent, *, corrupt=False):
    raw = index.producer.plan_payload(parent, op, sequence=number, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    path = repo / index.hosted.LEDGER / f"{number:016}.payload.json"
    path.write_bytes(raw + (b"\n" if corrupt else b""))
    return commit(repo), index.channel.parse_payload(raw)


@pytest.mark.parametrize('failure', ['canonical', 'proof'])
def test_recovery_range_and_each_role_use_same_proven_withdrawal_prefix(tmp_path, failure):
    repo, _, _, _, _, first = fixture(tmp_path)
    before, second = append(repo, 2, index.producer.Promote(manifest_bytes(build=3), '3.2.6'), index.channel.parse_payload(first))
    revoke = index.producer.Revoke('model-hub-engine-v8.0.63-3', 'model-hub-engine-v8.0.63-2', 'rollback')
    landing, third = append(repo, 3, revoke, second)
    last, _ = append(repo, 4, index.producer.Promote(manifest_bytes(build=4), '3.2.6'), third, corrupt=failure == 'canonical')
    def prove(row):
        if row[4].sequence != 3:
            raise index.hosted.Error('qualification mismatch')
    def rows():
        return index.iter_records(repo, before, last)
    accepted, rejected = index.qualified_prefix(rows(), prove)
    assert rejected and [r[5] for r in accepted] == [revoke]
    assert index.authorized_rows(rows(), [3]) == accepted
    with pytest.raises(index.hosted.Error):
        index.authorized_rows(rows(), [4])
    replay = index.landing_range(repo, 'workflow_dispatch', {'inputs': {'landing': landing}}, last)
    assert index.records(repo, *replay) == accepted
    index.preflight(repo, before, landing, api=None, token=None, single=True)
    with pytest.raises(index.hosted.Error):
        index.preflight(repo, before, last, api=None, token=None, single=True)


def test_rehearsal_attempt_is_checked_before_artifact_or_release_io(tmp_path):
    repo, _, _, _, op, raw = fixture(tmp_path)
    payload = index.channel.parse_payload(raw)
    evidence = repo / index.evidence_path(1)
    evidence.parent.mkdir()
    evidence.write_text(json.dumps({'payload_sha256': hashlib.sha256(raw).hexdigest(), 'parent_sha256': None,
                                    'rehearsal': {'run_id': 11, 'run_attempt': 2}}))
    sha = commit(repo)
    class WrongAttempt:
        def get(self, path):
            assert path == 'actions/runs/11/attempts/2'
            return {'id': 11, 'run_attempt': 3, 'status': 'completed', 'conclusion': 'success'}
    with pytest.raises(index.hosted.Error, match='attempt'):
        index.qualify_promotion(repo, sha, op, None, payload, api=WrongAttempt(), token=None)
