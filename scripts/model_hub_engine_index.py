"""Read-only prep and isolated role entry points for the merged allocation workflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_artifact as artifacts  # noqa: E402
from scripts import model_hub_engine_merge as gate  # noqa: E402
from scripts import model_hub_engine_qualification as qualification  # noqa: E402
from scripts import model_hub_engine_hosted as hosted  # noqa: E402

producer, channel = hosted.producer, hosted.channel
INDEX_WORKFLOW = ".github/workflows/model-hub-engine-index.yml"


def records(repo, before, after):
    """Only contiguous, append-only, canonical one-operation payloads are eligible."""
    changes = gate.push_additions(repo, before=before, after=after)
    # Examine the parent tree, not just the last record found in a path-limited log.
    names = hosted.git(repo, "ls-tree", "-r", "--name-only", before, "--", hosted.LEDGER).decode().splitlines()
    if names != [f"{hosted.LEDGER}/{n:016}.payload.json" for n in range(1, len(names) + 1)]:
        raise hosted.Error("noncontiguous predecessor ledger")
    if hosted.git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise hosted.Error("complete master history required")
    history = hosted.git(repo, "log", "--first-parent", "--reverse", "--root", "--diff-merges=first-parent", "--no-renames",
                         "--format=", "--name-status", before, "--", hosted.LEDGER).decode().splitlines()
    if [line for line in history if line] != ["A\t" + path for path in names]:
        raise hosted.Error("predecessor ledger has a rewrite, deletion or out-of-order allocation")
    parent = None if not names else channel.parse_payload(hosted.file_at(repo, before, names[-1]))
    if parent and parent.sequence != len(names):
        raise hosted.Error("predecessor sequence disagrees with allocation")
    result = []
    for seq, (commit, path, blob) in enumerate(changes, start=len(names) + 1):
        raw = hosted.file_at(repo, commit, path)
        payload = channel.parse_payload(raw)
        if path != f"{hosted.LEDGER}/{seq:016}.payload.json" or payload.sequence != seq:
            raise hosted.Error("allocation sequence disagrees with push")
        ops = [producer.Promote(r.manifest_bytes, r.min_avibe_version) for r in payload.releases
               if not parent or not parent.release(r.asset_release_tag)]
        ops += [producer.Revoke(r.asset_release_tag, r.rollback_to, r.reason) for r in payload.revoked
                if not parent or not parent.tombstone(r.asset_release_tag)]
        if len(ops) != 1 or producer.plan_payload(parent, ops[0], sequence=seq, now=payload.issued_at) != raw:
            raise hosted.Error("allocation must be exactly one canonical promotion or withdrawal")
        result.append((commit, path, blob, parent, payload, ops[0]))
        parent = payload
    return result


def qualify_promotion(repo, commit, op, parent, *, head, api, token):
    # A PR's description is mutable after review. Read the designation from the
    # reviewed commit message, not from the description or a workflow input.
    from core.model_hub_engine_manifest import parse_release_manifest

    message = api.get(f"git/commits/{head}")
    ids = re.findall(r"^Engine-Rehearsal: ([1-9][0-9]{0,19})$", message.get("message", ""), re.M)
    if message.get("sha") != head or len(ids) != 1:
        raise hosted.Error("one designated rehearsal trailer required on the reviewed head")
    run = ids[0]
    if api.get(f"actions/runs/{run}").get("conclusion") != "success":
        raise hosted.Error("designated rehearsal did not succeed")
    tag = parse_release_manifest(op.manifest_bytes).asset_release_tag
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        candidate = tmp / tag
        candidate.mkdir()
        for name in (qualification.CANDIDATE_FILE, qualification.MANIFEST_FILE):
            (candidate / name).write_bytes(hosted.file_at(repo, commit, f"patches/model-hub-engine-candidates/{tag}/{name}"))
        reviewed = qualification.load_candidate(candidate)
        if (reviewed.manifest_bytes, reviewed.min_avibe_version) != (op.manifest_bytes, op.min_avibe_version):
            raise hosted.Error("candidate differs from allocation")
        for name in (qualification.UNTRUSTED_REHEARSAL_ARTIFACT, qualification.UNTRUSTED_ASSETS_ARTIFACT):
            subprocess.run(["gh", "run", "download", run, "--repo", producer.REPOSITORY, "--name", name, "--dir", str(tmp / name)],
                           check=True, env={"GH_TOKEN": token, "PATH": os.environ["PATH"], "HOME": directory})
        result = json.loads((tmp / qualification.UNTRUSTED_REHEARSAL_ARTIFACT / "untrusted_rehearsal.json").read_bytes())
        if str(result.get("run_id")) != run:
            raise hosted.Error("artifact differs from designated rehearsal")
        rest = qualification._api(token)
        roles = qualification.trusted_released_context(reviewed, api=rest, output_dir=tmp / "released")
        bundled = [p.read_bytes() for p in (tmp / "released").glob("bundled-manifest-*.json")]
        manifests = producer.baseline_manifests(parent, bundled)
        qualification.verify_untrusted_rehearsal(result, candidate=reviewed, api=rest, harness_sha256=qualification.harness_digest(),
                                                 baselines={k: hashlib.sha256(v).hexdigest() for k, v in manifests.items()}, avibe=roles)
        qualification.build_evidence(candidate, tmp / qualification.UNTRUSTED_ASSETS_ARTIFACT)
        qualification.require_published(candidate, tmp / "availability")


def wait_for_parent(fetch, *, allowed, parent, roots, verifier, timeout=600, sleep=time.sleep):
    """Wait only for a known older sequence, never reinterpret a denial or a divergent index."""
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        current = None if value is None else producer.authenticate(value, roots=roots, verifier=verifier)
        if (current is None and parent is None) or (current and current.raw in allowed):
            return current
        if current and (parent is None or current.sequence >= parent.sequence):
            raise hosted.Error("unexpected or superseding index; refusing stale publication")
        if time.monotonic() >= deadline:
            raise hosted.Error("predecessor not published; rerun the original push after recovery")
        sleep(5)


def publish_batch(entries, *, bucket, roots, verifier, envelope_for, fetch_public=producer.fetch_public_index):
    """One writer drains one landed push. Each public readback precedes the next PUT."""
    def read():
        stored = bucket.read()
        return stored[0] if stored else None

    parent = entries[0][3]
    current = wait_for_parent(read, allowed=[row[4].raw for row in entries] + ([parent.raw] if parent else []),
                              parent=parent, roots=roots, verifier=verifier)
    for _, _, _, previous, payload, _ in entries:
        # On recovery the authenticated origin can already hold an exact later
        # member of this batch. Don't rewrite predecessors. Recheck the current
        # member itself, including both reads, even if no PUT is necessary.
        if current and payload.sequence < current.sequence:
            continue
        envelope = envelope_for(payload)
        if producer.authenticate(envelope, roots=roots, verifier=verifier).raw != payload.raw:
            raise hosted.Error("signed envelope differs from landed bytes")
        producer.publish(envelope, bucket=bucket, roots=roots, verifier=verifier, fetch_public=fetch_public,
                         guard=lambda stored, raw=payload.raw, prev=previous: hosted.check_published(
                             stored, raw, prev.raw if prev else None, roots, verifier))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("prepare", "sign", "publish"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--avault", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("HOSTED_COMMISSIONED") != "true" or producer.run_context_problems(os.environ, events={"push"}, workflow_path=INDEX_WORKFLOW):
        raise hosted.Error("not the commissioned master index push")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    sha, token = os.environ["GITHUB_SHA"], os.environ["GITHUB_TOKEN"]
    if event.get("ref") != "refs/heads/master" or event.get("after") != sha or event.get("forced") is not False:
        raise hosted.Error("not an ordinary master push")
    api = gate.ci.GitHubApi(producer.REPOSITORY, token)
    gate.ci._require_on_master(api, sha)
    entries = records(args.data, event["before"], sha)
    roots = channel.parse_trust_roots(hosted.file_at(args.data, sha, hosted.ROOT))
    current_root = api.get("contents/" + hosted.ROOT, query={"ref": "master"})["sha"]
    if (len(roots.roots) != 1 or entries[0][4].sequence < roots.sequence_floor
            or current_root != hosted.git(args.data, "rev-parse", f"{sha}:{hosted.ROOT}").decode().strip()):
        raise hosted.Error("no matching commissioned root")
    verifier = producer.avault_verifier(args.avault)
    if args.role == "prepare":
        for commit, path, blob, parent, payload, op in entries:
            _, head = gate.prove_landing(api, commit=commit, path=path, blob=blob)
            if isinstance(op, producer.Promote):
                qualify_promotion(args.data, commit, op, parent, head=head, api=api, token=token)
        parent = entries[0][3]
        allowed = [row[4].raw for row in entries] + ([parent.raw] if parent else [])
        wait_for_parent(producer.fetch_public_index, allowed=allowed, parent=parent, roots=roots, verifier=verifier)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
            print("sequences=" + json.dumps([row[4].sequence for row in entries]), file=fp)
        return
    run, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if args.role == "sign":
        seq = int(os.environ["ALLOCATION_SEQUENCE"])
        payload = next(row[4] for row in entries if row[4].sequence == seq)
        name = artifacts.artifact_name(run, sha, seq, payload.raw, attempt=attempt)
        credential = json.loads(os.environ.pop("HOSTED_SIGNING_BUNDLE"))
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            binary = Path(args.avault).resolve(strict=True)
            hosted.avault_run(binary, ["key", "import"], home, data=json.dumps(credential["import"]).encode())
            envelope = producer.sign_payload(payload.raw, key_id=roots.roots[0].key_id, key_name=hosted.KEY_NAME,
                key_envelope=credential["envelope"], roots=roots, verifier=verifier,
                sign=lambda request: json.loads(hosted.avault_run(binary, ["sign"], home, data=request))["signature"])
        (Path(os.environ["RUNNER_TEMP"]) / "model-hub-engine.json").write_bytes(envelope)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
            print(f"artifact_name={name}", file=fp)
        return
    def envelope_for(payload):
        name = artifacts.artifact_name(run, sha, payload.sequence, payload.raw, attempt=attempt)
        identifier, digest = artifacts.wait_for_artifact(qualification._api(token), run, sha, name, attempt=attempt)
        return artifacts.download(identifier, digest, token)

    account = os.environ["R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise hosted.Error("unexpected writer endpoint")
    bucket = producer.IndexBucket("avibe", f"https://{account}.r2.cloudflarestorage.com", producer._run_command)
    publish_batch(entries, bucket=bucket, roots=roots, verifier=verifier, envelope_for=envelope_for)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"index workflow refused ({type(exc).__name__})")
