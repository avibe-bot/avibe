"""Validate one designated hosted rehearsal, using the reviewed producer tree as data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_channel as producer  # noqa: E402
from scripts import model_hub_engine_qualification as q  # noqa: E402

# Updated by the trusted caller *after* freezing the complete producer revision.
H = '275e150316765a3b5f1e467b249ccdca08327832'
LIMIT = 2_000_000
Error = q.QualificationError


def gh(path):
    return subprocess.check_output(['gh', 'api', path], timeout=90)


def api(path):
    return json.loads(gh(path))


def blob(repo, path):
    return subprocess.check_output(['git', '-C', repo, 'show', f'{H}:{path}'])


def hosted_report(run, attempt):
    prefix = f'/repos/{q.REPOSITORY}/actions'
    record = api(f'{prefix}/runs/{run}/attempts/{attempt}')
    if record.get('id') != run or record.get('run_attempt') != attempt:
        raise Error('wrong rehearsal attempt')
    if record.get('head_sha') != H:
        raise Error('hosted attempt executed a different rehearsal revision')
    if (record.get('path'), record.get('event'), record.get('conclusion')) != (q.QUALIFICATION_WORKFLOW_PATH, 'workflow_dispatch', 'success'):
        raise Error('designated attempt is not a successful qualification dispatch')
    name = q.UNTRUSTED_REHEARSAL_ARTIFACT + ('' if attempt == 1 else f'-attempt-{attempt}')
    listing = api(f'{prefix}/runs/{run}/artifacts?name={name}')
    if listing.get('total_count') != 1 or len(listing['artifacts']) != 1:
        raise Error('designated rehearsal artifact missing or ambiguous')
    item, = listing['artifacts']
    if item['name'] != name or item['expired'] or not 0 < item['size_in_bytes'] <= LIMIT:
        raise Error('invalid or expired rehearsal artifact')
    raw = gh(f"{prefix}/artifacts/{int(item['id'])}/zip")
    if item['digest'] != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise Error('hosted artifact archive digest mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist() != ['untrusted_rehearsal.json'] or archive.infolist()[0].file_size > LIMIT:
            raise Error('unexpected rehearsal artifact contents')
        return archive.read('untrusted_rehearsal.json')


def verify(repo, run, attempt, tag, *, supplied=None, previous=None):
    if type(run) is not int or type(attempt) is not int or min(run, attempt) < 1 or not producer.ASSET_TAG_RE.fullmatch(tag):
        raise Error('invalid rehearsal selector')
    original = hosted_report(run, attempt)
    if supplied is not None and supplied != original:
        raise Error('supplied artifact differs from the original hosted bytes')
    report = q.channel.strict_json(original)
    if (report.get('run_id'), report.get('run_attempt')) != (run, attempt):
        raise Error('artifact records a different run or attempt')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path in q.HARNESS_FILES:
            dest = root / path
            dest.parent.mkdir(exist_ok=True, parents=True)
            dest.write_bytes(blob(repo, path))
        expected = q.harness_digest(root)
        record = root / tag
        record.mkdir()
        for file in (q.CANDIDATE_FILE, q.MANIFEST_FILE):
            (record / file).write_bytes(blob(repo, f'patches/model-hub-engine-candidates/{tag}/{file}'))
        candidate = q.load_candidate(record)
        roles = q.trusted_released_context(candidate, api=api, output_dir=root / 'released')
        bundled = [p.read_bytes() for p in (root / 'released').glob('bundled-manifest-*.json')]
        baselines = producer.baseline_manifests(previous, bundled)
        q.verify_untrusted_rehearsal(report, candidate=candidate, api=api, harness_sha256=expected, reviewed_revision=H,
                                     baselines={k: hashlib.sha256(v).hexdigest() for k, v in baselines.items()}, avibe=roles)
        return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--run', type=int, required=True)
    parser.add_argument('--attempt', type=int, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--artifact', type=Path, required=True, help='original untrusted_rehearsal.json')
    args = parser.parse_args()
    candidate = verify(args.repo, args.run, args.attempt, args.tag, supplied=args.artifact.read_bytes())
    print(f'accepted revision {H}, attempt {args.run}/{args.attempt}, manifest {candidate.manifest_sha256}')


if __name__ == '__main__':
    try:
        main()
    except (Error, ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
