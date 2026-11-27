"""One reviewed master manifest authorizes one exact engine index payload."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_rehearsal as rehearsal  # noqa: E402

producer, q = rehearsal.producer, rehearsal.q
channel = q.channel
MANIFEST = 'patches/model-hub-engine-channel.json'
Error = producer.ProducerError


def git(repo, *args):
    return subprocess.check_output(['git', '-C', repo, *args])


def manifest(repo, rev):
    entry = git(repo, 'ls-tree', rev, '--', MANIFEST).split()
    if not entry:
        return None, {}
    if entry[:2] != [b'100644', b'blob']:
        raise Error('channel manifest must be a regular file')
    doc = channel.strict_json(git(repo, 'show', f'{rev}:{MANIFEST}'))
    if set(doc) != {'payload', 'rehearsals'} or not isinstance(doc['rehearsals'], dict):
        raise Error('expected payload and rehearsal selectors')
    # This serialization is the signing contract. Never read a clock in the signer.
    payload = channel.parse_payload(json.dumps(doc['payload'], sort_keys=True, separators=(',', ':')).encode('ascii'))
    for w in payload.revoked:
        if payload.release(w.asset_release_tag) or payload.tombstone(w.rollback_to):
            raise Error('withdrawn release cannot be offered or used for rollback')
    return payload, doc['rehearsals']


def successor(previous, desired, *, origin=False):
    if previous and previous.raw == desired.raw:
        return
    if previous and desired.sequence <= previous.sequence:
        raise Error('changed bytes require a higher sequence')
    if previous:
        channel.check_successor(producer._identities(previous), previous.revoked, desired)
        for release in previous.releases:
            if not desired.release(release.asset_release_tag) and not desired.tombstone(release.asset_release_tag):
                raise Error('removing a release requires an explicit withdrawal')
    for w in desired.revoked:
        if previous and previous.tombstone(w.asset_release_tag) == w:
            continue
        target = previous and previous.release(w.asset_release_tag)
        # A lagging origin may never have seen the retired master entry. The
        # merge preflight already checked that withdrawal against the actual
        # base manifest (or the floor for an unlisted packaged engine).
        if origin and target is None:
            continue
        minimum = target.minimum if target else channel.FIRST_CHANNEL_AVIBE_VERSION
        if desired.release(w.rollback_to).minimum > minimum:
            raise Error('rollback target needs a newer Avibe than an affected client')


def verify_assets(payload):
    for release in payload.releases:
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work) / release.asset_release_tag
            directory.mkdir()
            (directory / q.MANIFEST_FILE).write_bytes(release.manifest_bytes)
            (directory / q.CANDIDATE_FILE).write_text(json.dumps({'schema_version': 1, 'asset_release_tag': release.asset_release_tag,
                'manifest_sha256': release.manifest_sha256, 'min_avibe_version': release.min_avibe_version}))
            q.require_published(directory, Path(work) / 'available')


def preflight(repo, base, head, *, harness):
    previous, _ = manifest(repo, base)
    desired, receipts = manifest(repo, head)
    if desired is None:
        if previous:
            raise Error('withdraw explicitly; the channel manifest cannot be deleted')
        return
    if previous and previous.raw == desired.raw:
        return
    successor(previous, desired)
    for release in desired.releases:
        tag = release.asset_release_tag
        if previous and previous.release(tag) == release:
            continue
        if tag not in receipts or set(receipts[tag]) != {'run', 'attempt'}:
            raise Error('promotion needs a designated rehearsal run and attempt')
        candidate = rehearsal.verify(harness, tag=tag, previous=previous, **receipts[tag])
        if (candidate.manifest_bytes, candidate.min_avibe_version) != (release.manifest_bytes, release.min_avibe_version):
            raise Error('rehearsal does not qualify these bytes and minimum')
    verify_assets(desired)


def write_index(desired, envelope, *, bucket, roots, verifier, public, assert_current):
    if producer.authenticate(envelope, roots=roots, verifier=verifier).raw != desired.raw:
        raise Error('signed artifact does not match the master manifest')
    stored = bucket.read()
    previous = producer.authenticate(stored[0], roots=roots, verifier=verifier) if stored else None
    successor(previous, desired, origin=True)
    if previous is None or previous.raw != desired.raw:
        verify_assets(desired)
        assert_current()
        bucket.replace(envelope, etag=stored[1] if stored else None)
    else:
        envelope = stored[0]
    if (bucket.read() or (None,))[0] != envelope or public() != envelope:
        raise Error('origin/public readback pending; retry the current manifest')


def sign_index(desired, roots, avault, bundle):
    """Import the channel's wrapped key into a disposable, signer-only avault home."""
    with tempfile.TemporaryDirectory() as home:
        env = {'HOME': home, 'AVAULT_HOME': home, 'AVAULT_STORE': 'file', 'PATH': os.defpath}
        def call(args, data):
            process = subprocess.run([avault, *args], input=data, capture_output=True, env=env, timeout=30)
            if process.returncode:
                raise Error('channel avault operation failed')
            return process.stdout
        call(['key', 'import'], json.dumps(bundle['import']).encode())
        return producer.sign_payload(desired.raw, key_id=roots.roots[0].key_id, key_name='MODEL_HUB_ENGINE_CHANNEL',
            key_envelope=bundle['envelope'], roots=roots, verifier=producer.avault_verifier(avault),
            sign=lambda data: json.loads(call(['sign'], data))['signature'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('role', choices=['preflight', 'prepare', 'sign', 'upload'])
    p.add_argument('--repo', type=Path, default=Path.cwd())
    p.add_argument('--harness', type=Path, help='repository containing the reviewed producer revision')
    p.add_argument('--base')
    p.add_argument('--avault')
    p.add_argument('--envelope', type=Path)
    args = p.parse_args()
    head = git(args.repo, 'rev-parse', 'HEAD').decode().strip()
    if args.role == 'preflight':
        preflight(args.repo, args.base or head + '^1', head, harness=args.harness or args.repo)
        return
    if os.environ.get('GITHUB_REF') != 'refs/heads/master' or os.environ.get('GITHUB_REPOSITORY') != producer.REPOSITORY:
        raise Error('publication requires the master workflow')
    def assert_current():
        if rehearsal.api(f'/repos/{producer.REPOSITORY}/git/ref/heads/master')['object']['sha'] != head:
            raise Error('master advanced; retry current master')
    roots = channel.parse_trust_roots((args.repo / 'vibe/model_hub_runtime/engine_channel_roots.json').read_bytes())
    desired, _ = manifest(args.repo, head)
    if not roots.commissioned or not desired:
        if args.role == 'prepare':
            return
        raise Error('no commissioned manifest')
    assert_current()
    if len(roots.roots) != 1 or desired.sequence < roots.sequence_floor:
        raise Error('manifest outside commissioned root policy')
    if args.role == 'prepare':
        verify_assets(desired)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            print(f'revision={head}', file=output)
        return
    verifier = producer.avault_verifier(args.avault)
    if args.role == 'sign':
        args.envelope.write_bytes(sign_index(desired, roots, args.avault, json.loads(os.environ.pop('CHANNEL_SIGNER_BUNDLE'))))
        return
    account = os.environ['R2_ACCOUNT_ID']
    if not re.fullmatch('[0-9a-f]{32}', account):
        raise Error('invalid mirror endpoint')
    bucket = producer.IndexBucket('avibe', f'https://{account}.r2.cloudflarestorage.com', producer._run_command)
    write_index(desired, args.envelope.read_bytes(), bucket=bucket, roots=roots, verifier=verifier,
                public=producer.fetch_public_index, assert_current=assert_current)


if __name__ == '__main__':
    try:
        main()
    except (Error, q.QualificationError, channel.ChannelError) as exc:
        sys.exit(str(exc))
