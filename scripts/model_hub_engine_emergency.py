"""A reviewed emergency withdrawal chooses one effective parent, never two envelopes.

The public wire protocol has no branch selection. The allocation PR names the
exact records it retires. Retirement takes effect only at the conditional PUT
of the higher signed withdrawal; creating this document makes no state change.
"""
import hashlib
import json

from scripts import model_hub_engine_hosted as hosted


def path(sequence):
    return f"patches/model-hub-engine-emergency/{sequence:016}.json"


def ledger_path(sequence):
    return f"{hosted.LEDGER}/{sequence:016}.payload.json"


def declaration(repo, commit, payload):
    record = hosted.git(repo, 'ls-tree', commit, '--', path(payload.sequence)).split()
    if not record:
        return None
    if len(record) != 4 or record[:2] != [b'100644', b'blob']:
        raise hosted.Error('emergency declaration must be a regular Git blob')
    document = json.loads(hosted.file_at(repo, commit, path(payload.sequence)))
    if not isinstance(document, dict) or set(document) != {'effective_parent', 'retire', 'payload_sha256'}:
        raise hosted.Error('invalid emergency declaration')
    if document['payload_sha256'] != payload.sha256:
        raise hosted.Error('emergency declaration differs from reviewed withdrawal bytes')
    first = document['effective_parent']
    if not isinstance(first, dict) or type(first.get('sequence')) is not int or not 1 <= first['sequence'] < payload.sequence - 1:
        raise hosted.Error('emergency must name a prior effective parent and at least one intervening allocation')
    start = first['sequence']
    retired = document['retire']
    if not isinstance(retired, list) or len(retired) != payload.sequence - start - 1:
        raise hosted.Error('emergency must name every intervening allocation')
    parent = None
    for n, ref in enumerate([first, *retired], start):
        raw = hosted.file_at(repo, commit, ledger_path(n))
        if (not isinstance(ref, dict) or type(ref.get('sequence')) is not int
                or ref != {'sequence': n, 'sha256': hashlib.sha256(raw).hexdigest()}):
            raise hosted.Error('effective parent or retired blob mismatch')
        if n == start:
            parent = hosted.channel.parse_payload(raw)
            if parent.sequence != n:
                raise hosted.Error('declared effective parent sequence mismatch')
    return parent, record[2].decode()
