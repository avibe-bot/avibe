"""The exact bytes of the current master ledger are the only signing input."""
import json
import re

from scripts import model_hub_engine_hosted as hosted

PATH = hosted.LEDGER + '/current.payload.json'


def snapshot(repo, revision):
    entry = hosted.git(repo, 'ls-tree', revision, '--', PATH).split()
    if not entry:
        return None
    if len(entry) != 4 or entry[:2] != [b'100644', b'blob']:
        raise hosted.Error('snapshot must be a regular file')
    raw = hosted.file_at(repo, revision, PATH)
    payload = hosted.channel.parse_payload(raw)
    if raw != json.dumps(hosted.channel.strict_json(raw), sort_keys=True, separators=(',', ':')).encode('ascii'):
        raise hosted.Error('snapshot bytes must use canonical JSON')
    for withdrawal in payload.revoked:
        if payload.release(withdrawal.asset_release_tag) or payload.tombstone(withdrawal.rollback_to):
            raise hosted.Error('withdrawn releases cannot be offered or used as fallbacks')
    return payload


def revision_step(repo, base, head):
    old, new = snapshot(repo, base), snapshot(repo, head)
    if new is None:
        if old is not None:
            raise hosted.Error('cannot delete the signing snapshot')
    elif old and old.raw != new.raw and old.sequence >= new.sequence:
        raise hosted.Error('changing any snapshot byte requires a higher sequence')
    return old, new


def master_snapshot(repo, head):
    # A stale PR check must never let 3/Q be signed after 3/P, even if P's
    # upload failed. Check Git revisions, not publications or workflow runs.
    if not re.fullmatch('[0-9a-f]{40}', head) or hosted.git(repo, 'rev-parse', '--is-shallow-repository').strip() != b'false':
        raise hosted.Error('complete immutable master revision required')
    revisions = hosted.git(repo, 'rev-list', '--first-parent', '--reverse', head, '--', PATH).decode().splitlines()
    current = snapshot(repo, head)
    if current is None:
        if revisions:
            raise hosted.Error('cannot delete the signing snapshot')
        return None
    for rev in reversed(revisions):
        previous = snapshot(repo, rev)
        if previous and previous.raw != current.raw and previous.sequence >= current.sequence:
            raise hosted.Error('snapshot history reused a sequence')
    return current


def transition(current, desired):
    """Return sign or readback. Only authenticated published facts are permanent."""
    if current is None:
        return 'sign'
    if current.raw == desired.raw:
        return 'readback'
    if current.sequence >= desired.sequence:
        raise hosted.Error('stale or conflicting snapshot')
    hosted.channel.check_successor(hosted.producer._identities(current), current.revoked, desired)
    for release in current.releases:
        if desired.release(release.asset_release_tag) is None:
            withdrawal = desired.tombstone(release.asset_release_tag)
            if not withdrawal:
                raise hosted.Error('removing a published release requires an explicit withdrawal')
            if desired.release(withdrawal.rollback_to).minimum > release.minimum:
                raise hosted.Error('rollback target needs a newer Avibe')
    return 'sign'
