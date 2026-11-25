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
        return index.candidates(repo, before, last)
    accepted, rejected = index.delivery_plan(rows(), prove)
    assert rejected and [r[5] for r in accepted] == [revoke]
    assert index.selected_records(rows(), [3]) == accepted
    with pytest.raises(index.hosted.Error):
        index.selected_records(rows(), [5])
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


@pytest.mark.parametrize('mutation', ['none', 'payload', 'parent', 'sequence', 'blob'])
def test_exceptional_binding_reopens_only_the_original_bytes(tmp_path, mutation):
    repo, before, landed, _, _, _ = fixture(tmp_path)
    row, = index.iter_records(repo, before, landed)
    doc = index.recovery.binding(row)
    if mutation != 'none':
        key = {'payload': 'payload_sha256'}.get(mutation, mutation)
        doc[key] = {'payload': '0' * 64, 'parent': {}, 'sequence': 2, 'blob': '0' * 40}[mutation]
    path = f'{index.recovery.DIRECTORY}/0000000000000001.json'
    request = repo / path
    request.parent.mkdir()
    request.write_text(json.dumps(doc))
    end = commit(repo)
    if mutation == 'none':
        original, name, oid = index.recovery.read_request(repo, landed, end, index.iter_records)
        assert original == row and name == path and oid == index.hosted.git(repo, 'rev-parse', end + ':' + path).decode().strip()
    else:
        with pytest.raises(index.hosted.Error):
            index.recovery.read_request(repo, landed, end, index.iter_records)


@pytest.mark.parametrize('outcome', ['missing', 'timeout', 'failed', 'green'])
def test_only_missing_history_can_use_exceptional_path(monkeypatch, outcome):
    gate = index.gate
    def proof(*args, **kwargs):
        if outcome == 'missing': raise gate.MissingHistoricalEvidence('absent')
        if outcome == 'timeout': raise TimeoutError('temporarily unavailable')
        if outcome == 'failed': raise index.hosted.Error('failed')
        return 1, 'a' * 40
    monkeypatch.setattr(gate, 'prove_landing', proof)
    if outcome == 'missing':
        index.recovery.require_missing(None, ('commit', 'path', 'blob'))
    else:
        with pytest.raises(TimeoutError if outcome == 'timeout' else index.hosted.Error):
            index.recovery.require_missing(None, ('commit', 'path', 'blob'))
