"""Publish only the current master snapshot. Qualification belongs before merge."""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_qualification as qualification  # noqa: E402
from scripts import model_hub_engine_snapshot as snapshot  # noqa: E402

hosted = snapshot.hosted
producer = hosted.producer
INDEX_WORKFLOW = '.github/workflows/model-hub-engine-index.yml'


def sign(payload, roots, verifier, avault):
    credential = json.loads(os.environ.pop("HOSTED_SIGNING_BUNDLE"))
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        binary = Path(avault).resolve(strict=True)
        hosted.avault_run(binary, ["key", "import"], home, data=json.dumps(credential["import"]).encode())
        envelope = producer.sign_payload(payload.raw, key_id=roots.roots[0].key_id, key_name=hosted.KEY_NAME,
            key_envelope=credential["envelope"], roots=roots, verifier=verifier,
            sign=lambda request: json.loads(hosted.avault_run(binary, ["sign"], home, data=request))["signature"])
    return envelope


def verify_installable(desired):
    for release in desired.releases:
        qualification.require_listed_assets(release)


def writer(desired, *, bucket, roots, verifier, envelope, current_head, fetch_public=producer.fetch_public_index):
    if producer.authenticate(envelope, roots=roots, verifier=verifier).raw != desired.raw:
        raise hosted.Error('artifact does not sign this snapshot')
    stored = bucket.read()
    raw = stored[0] if stored else None
    current = producer.authenticate(raw, roots=roots, verifier=verifier) if raw is not None else None
    if snapshot.transition(current, desired) != 'readback':
        verify_installable(desired)
        current_head()
        bucket.replace(envelope, etag=stored[1] if stored else None)
    else:
        # A valid origin already has the desired payload. Never repair a
        # signature or cache mismatch by writing the same sequence again.
        envelope = raw
    if fetch_public() != envelope or (bucket.read() or (None,))[0] != envelope:
        raise hosted.Error('origin/public readback pending; retry current master')
    return 'readback'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('prepare', 'sign', 'publish'))
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--avault', required=True)
    args = parser.parse_args(argv)
    if os.environ.get('HOSTED_COMMISSIONED') != 'true' or producer.run_context_problems(
            os.environ, events={'push', 'workflow_dispatch'}, workflow_path=INDEX_WORKFLOW):
        raise hosted.Error('not the commissioned master workflow')
    head = hosted.git(args.data, 'rev-parse', 'HEAD').decode().strip()
    def current_head():
        ref = producer.github_api(f'/repos/{producer.REPOSITORY}/git/ref/heads/master', token=os.environ['GITHUB_TOKEN'])
        if ref['object']['sha'] != head:
            raise hosted.Error('master advanced; retry current master')
    current_head()
    desired = snapshot.master_snapshot(args.data, head)
    if desired is None:
        if args.role == 'prepare':
            return
        raise hosted.Error('no snapshot')
    roots = hosted.channel.parse_trust_roots(hosted.file_at(args.data, head, hosted.ROOT))
    if len(roots.roots) != 1 or desired.sequence < roots.sequence_floor:
        raise hosted.Error('no matching commissioned root')
    if args.role == 'prepare':
        verify_installable(desired)
        current_head()
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            print(f'revision={head}', file=output)
        return
    verifier = producer.avault_verifier(args.avault)
    artifact = Path(os.environ['RUNNER_TEMP']) / 'model-hub-engine.json'
    if args.role == 'sign':
        current_head()
        artifact.write_bytes(sign(desired, roots, verifier, args.avault))
        return
    account = os.environ['R2_ACCOUNT_ID']
    if not re.fullmatch('[0-9a-f]{32}', account):
        raise hosted.Error('unexpected writer endpoint')
    bucket = producer.IndexBucket('avibe', f'https://{account}.r2.cloudflarestorage.com', producer._run_command)
    writer(desired, bucket=bucket, roots=roots, verifier=verifier,
           envelope=artifact.read_bytes(), current_head=current_head)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if sys.argv[1:2] == ['publish'] and isinstance(exc, (hosted.Error, hosted.channel.ChannelError, qualification.QualificationError)):
            sys.exit(f'index publication refused: {exc}')
        sys.exit(f'index workflow refused ({type(exc).__name__}); verify origin/public readback before reporting published')
