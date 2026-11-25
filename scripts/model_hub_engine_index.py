"""Publish only the current master snapshot. Qualification belongs before merge."""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


def writer(role, desired, *, bucket, roots, verifier, artifact, current_head, fetch_public=producer.fetch_public_index):
    stored = bucket.read()
    raw = stored[0] if stored else None
    def authenticate(value):
        return producer.authenticate(value, roots=roots, verifier=verifier) if value is not None else None
    action = snapshot.transition(authenticate(raw), desired)
    if action == 'sign':
        # Do not silently bootstrap from a missing bucket object over a signed
        # public state, or advance during an unresolved readback.
        if fetch_public() != raw:
            raise hosted.Error('origin/public disagreement; publication pending')
        if role == 'inspect':
            return action
        envelope = artifact()
        if authenticate(envelope).raw != desired.raw:
            raise hosted.Error('artifact does not sign this snapshot')
        current_head()
        bucket.replace(envelope, etag=stored[1] if stored else None)
    else:
        envelope = raw
    if fetch_public() != envelope or (bucket.read() or (None,))[0] != envelope:
        raise hosted.Error('origin/public readback pending; retry current master')
    return 'readback'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('inspect', 'sign', 'publish'))
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
        if args.role == 'inspect':
            return
        raise hosted.Error('no snapshot')
    roots = hosted.channel.parse_trust_roots(hosted.file_at(args.data, head, hosted.ROOT))
    if len(roots.roots) != 1 or desired.sequence < roots.sequence_floor:
        raise hosted.Error('no matching commissioned root')
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
    action = writer(args.role, desired, bucket=bucket, roots=roots, verifier=verifier,
                    artifact=artifact.read_bytes, current_head=current_head)
    if args.role == 'inspect':
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            print(f'action={action}\nrevision={head}', file=output)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        sys.exit(f'index workflow refused ({type(exc).__name__}); verify origin/public readback before reporting published')
