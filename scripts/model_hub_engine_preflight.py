"""Pre-merge eligibility only. Read the proposed merge tree as data."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_snapshot as snapshot, model_hub_engine_qualification as qualification  # noqa: E402

hosted = snapshot.hosted
producer = hosted.producer


def evidence_path(sequence):
    return f"patches/model-hub-engine-allocation-evidence/{sequence:016}.json"


def load_receipt(repo, commit, payload, parent):
    """Bind the designated rehearsal to the proposed bytes and merge base."""
    path = evidence_path(payload.sequence)
    record = hosted.git(repo, "ls-tree", commit, "--", path).split()
    if len(record) != 4 or record[:2] != [b"100644", b"blob"]:
        raise hosted.Error("missing regular allocation evidence")
    raw = hosted.file_at(repo, commit, path)
    if len(raw) > 2_000_000:
        raise hosted.Error("oversized allocation evidence")
    receipt = json.loads(raw)
    if (receipt.get("payload_sha256") != hashlib.sha256(payload.raw).hexdigest()
            or receipt.get("parent_sha256") != (hashlib.sha256(parent.raw).hexdigest() if parent else None)):
        raise hosted.Error("qualification belongs to another allocation or predecessor")
    result = receipt["rehearsal"]
    if any(type(result.get(k)) is not int or not 0 < result[k] < 10**20 for k in ("run_id", "run_attempt")):
        raise hosted.Error("invalid rehearsal attempt")
    return result


def qualify_promotion(repo, commit, release, parent, payload, *, published, api, token):
    """Pre-merge only: independently check the committed evidence while artifacts exist."""
    frozen = load_receipt(repo, commit, payload, parent)
    run, attempt = str(frozen["run_id"]), frozen["run_attempt"]
    source = api(f"/repos/{producer.REPOSITORY}/actions/runs/{run}/attempts/{attempt}")
    if (source.get("run_attempt") != attempt or str(source.get("id")) != run
            or source.get("status") != "completed" or source.get("conclusion") != "success"):
        raise hosted.Error("designated rehearsal attempt did not succeed")
    tag = release.asset_release_tag
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        candidate = tmp / tag
        candidate.mkdir()
        for name in (qualification.CANDIDATE_FILE, qualification.MANIFEST_FILE):
            (candidate / name).write_bytes(hosted.file_at(repo, commit, f"patches/model-hub-engine-candidates/{tag}/{name}"))
        reviewed = qualification.load_candidate(candidate)
        if (reviewed.manifest_bytes, reviewed.min_avibe_version) != (release.manifest_bytes, release.min_avibe_version):
            raise hosted.Error("candidate differs from allocation")
        for name in (qualification.UNTRUSTED_REHEARSAL_ARTIFACT, qualification.UNTRUSTED_ASSETS_ARTIFACT):
            # The first attempt keeps its historical name. Later full attempts
            # must not shadow it when preflight is verifying an older receipt.
            artifact = name if attempt == 1 else f'{name}-attempt-{attempt}'
            subprocess.run(["gh", "run", "download", run, "--repo", producer.REPOSITORY, "--name", artifact, "--dir", str(tmp / name)],
                           check=True, env={"GH_TOKEN": token, "PATH": os.environ["PATH"], "HOME": directory})
        result = json.loads((tmp / qualification.UNTRUSTED_REHEARSAL_ARTIFACT / "untrusted_rehearsal.json").read_bytes())
        if json.dumps(result, sort_keys=True) != json.dumps(frozen, sort_keys=True):
            raise hosted.Error("downloaded rehearsal differs from immutable allocation evidence")
        roles = qualification.trusted_released_context(reviewed, api=api, output_dir=tmp / "released")
        bundled = [p.read_bytes() for p in (tmp / "released").glob("bundled-manifest-*.json")]
        manifests = producer.baseline_manifests(published, bundled)
        qualification.verify_untrusted_rehearsal(result, candidate=reviewed, api=api, harness_sha256=qualification.harness_digest(),
                                                 baselines={k: hashlib.sha256(v).hexdigest() for k, v in manifests.items()}, avibe=roles)
        qualification.build_evidence(candidate, tmp / qualification.UNTRUSTED_ASSETS_ARTIFACT)

def preflight(repo, base, head, *, published, api, token):
    old, desired = snapshot.revision_step(repo, base, head)
    if desired is None or (old and desired.raw == old.raw):
        return
    withdrawal_only = (old and any(not old.tombstone(w.asset_release_tag) for w in desired.revoked)
                       and all(old.release(r.asset_release_tag) == r for r in desired.releases))
    # An emergency withdrawal needs no rehearsal baseline. Do not depend on
    # a possibly unavailable CDN index; the writer enforces published facts
    # against authenticated origin, before its conditional PUT.
    current = None if withdrawal_only else published()
    if not withdrawal_only:
        snapshot.transition(current, desired)
    snapshot.compatible_withdrawals(old, desired)
    for release in desired.releases:
        # Either the base tree qualified these exact bytes or the authenticated
        # index already offers them. Otherwise qualification is mandatory.
        if not any(p and p.release(release.asset_release_tag) == release for p in (old, current)):
            qualify_promotion(repo, head, release, old, desired, published=current, api=api, token=token)
        qualification.require_listed_assets(release)


def merge_base(repo, kind, event, head):
    """Bind the checked-out merge to its event head and actual base tree."""
    def sha(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
            raise hosted.Error('missing merge identities')
        return value

    if hosted.git(repo, 'rev-parse', 'HEAD').decode().strip() != sha(head):
        raise hosted.Error('checkout differs from the event merge')
    if kind == 'merge_group':
        group = event['merge_group']
        base = sha(group['base_sha'])
        if sha(group['head_sha']) != head or base.encode() not in hosted.git(repo, 'rev-list', '--first-parent', head).splitlines()[1:]:
            raise hosted.Error('unrelated merge group base or head')
        return base
    if kind != 'pull_request':
        raise hosted.Error('unsupported preflight event')
    pr = event['pull_request']
    parents = hosted.git(repo, 'show', '-s', '--format=%P', head).decode().split()
    if len(parents) != 2 or parents[1] != sha(pr['head']['sha']):
        raise hosted.Error('merge tree does not contain this PR head as its second parent')
    actual, _ = parents
    # GitHub can construct the synthetic merge after capturing the PR event.
    # Only accept forward progress on the target branch, not an unrelated tree.
    recorded = sha(pr['base']['sha'])
    target = 'refs/remotes/origin/' + pr['base']['ref']
    if (recorded.encode() not in hosted.git(repo, 'rev-list', '--first-parent', actual).splitlines() or
            actual.encode() not in hosted.git(repo, 'rev-list', '--first-parent', target).splitlines()):
        raise hosted.Error('merge parent is not forward progress on the event base branch')
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    args = parser.parse_args()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
    kind, head = os.environ['GITHUB_EVENT_NAME'], os.environ['GITHUB_SHA']
    if kind in ('push', 'workflow_dispatch'):
        return
    base = merge_base(args.data, kind, event, head)
    token = os.environ['GITHUB_TOKEN']

    def published():
        roots = hosted.channel.parse_trust_roots(hosted.file_at(args.data, base, hosted.ROOT))
        if not roots.commissioned:
            raise hosted.Error('snapshot requires a commissioned master root')
        raw = producer.fetch_public_index()
        if raw is None:
            return None
        with tempfile.TemporaryDirectory() as directory:
            binary = subprocess.check_output([sys.executable, '-I', str(Path(__file__).with_name('ci_avault_verifier.py')),
                                              '--output-dir', directory], text=True).strip()
            return producer.authenticate(raw, roots=roots, verifier=producer.avault_verifier(binary))
    preflight(args.data, base, head, published=published, api=qualification._api(token), token=token)


if __name__ == '__main__':
    main()
