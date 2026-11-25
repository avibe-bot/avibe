"""An exceptional recovery PR binds existing immutable allocation bytes; it cannot allocate."""
import json
import re

from scripts import model_hub_engine_hosted as hosted
from scripts import model_hub_engine_merge as gate

DIRECTORY = 'patches/model-hub-engine-recoveries'


def binding(row):
    commit, _, blob, parent, payload, _ = row
    return {'landing': commit, 'sequence': payload.sequence, 'blob': blob, 'payload_sha256': payload.sha256,
            'parent': None if parent is None else {'sequence': parent.sequence, 'payload_sha256': parent.sha256}}


def read_request(repo, before, after, allocation_at):
    changes = hosted.git(repo, 'diff', '--name-status', '--no-renames', before, after, '--', DIRECTORY).decode().splitlines()
    if not changes:
        return None
    if len(changes) != 1 or not (m := re.fullmatch(r'A\t(' + DIRECTORY + r'/([0-9]{16})\.json)', changes[0])):
        raise hosted.Error('recovery must add exactly one new immutable request')
    path, seq = m[1], int(m[2])
    mode, kind, request_blob, _ = hosted.git(repo, 'ls-tree', after, '--', path).decode().split()
    if (mode, kind) != ('100644', 'blob'):
        raise hosted.Error('recovery request must be a regular blob')
    doc = json.loads(hosted.file_at(repo, after, path))
    landing = doc.get('landing')
    if not isinstance(landing, str) or not re.fullmatch('[0-9a-f]{40}', landing) or landing.encode() not in hosted.git(repo, 'rev-list', '--first-parent', before).splitlines():
        raise hosted.Error('original allocation must already be on master')
    prev = hosted.git(repo, 'rev-parse', landing + '^1').decode().strip()
    matches = [row for row in allocation_at(repo, prev, landing) if row[4].sequence == seq]
    if len(matches) != 1 or json.dumps(binding(matches[0]), sort_keys=True) != json.dumps(doc, sort_keys=True):
        raise hosted.Error('recovery differs from the original sequence, parent or bytes')
    return matches[0], path, request_blob


def require_missing(api, row):
    commit, path, blob = row[:3]
    try:
        gate.prove_landing(api, commit=commit, path=path, blob=blob)
    except gate.MissingHistoricalEvidence:
        return
    raise hosted.Error('original authorization is readable: use ordinary recovery')
