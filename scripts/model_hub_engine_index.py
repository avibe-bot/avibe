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
from scripts import model_hub_engine_emergency as emergency  # noqa: E402
from scripts import model_hub_engine_recovery as recovery  # noqa: E402
from scripts import model_hub_engine_merge as gate  # noqa: E402
from scripts import model_hub_engine_qualification as qualification  # noqa: E402
from scripts import model_hub_engine_hosted as hosted  # noqa: E402

producer, channel = hosted.producer, hosted.channel
INDEX_WORKFLOW = ".github/workflows/model-hub-engine-index.yml"


def candidates(repo, before, after, *, proposed=False):
    """Yield independent, lazy row readers, preserving earlier results on a bad suffix."""
    changes = (gate.added_blobs(repo, before, after) if proposed
               else gate.iter_push_additions(repo, before=before, after=after))
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
    for seq, (commit, path, blob) in enumerate(changes, start=len(names) + 1):
        if path != emergency.ledger_path(seq):
            raise hosted.Error('allocation path disagrees with sequence')
        yield seq, (lambda n=seq, c=commit, p=path, b=blob: allocation(repo, n, c, p, b))


def allocation(repo, seq, commit, path, blob):
    raw = hosted.file_at(repo, commit, path)
    payload = channel.parse_payload(raw)
    if payload.sequence != seq:
        raise hosted.Error('allocation sequence disagrees with landing')
    declared = emergency.declaration(repo, commit, payload)
    parent = declared[0] if declared else (None if seq == 1 else channel.parse_payload(
        hosted.file_at(repo, commit, emergency.ledger_path(seq - 1))))
    if parent and not declared and parent.sequence != seq - 1:
        raise hosted.Error('predecessor sequence disagrees with allocation')
    ops = [producer.Promote(r.manifest_bytes, r.min_avibe_version) for r in payload.releases
           if not parent or not parent.release(r.asset_release_tag)]
    ops += [producer.Revoke(r.asset_release_tag, r.rollback_to, r.reason) for r in payload.revoked
            if not parent or not parent.tombstone(r.asset_release_tag)]
    if (len(ops) != 1 or (declared and not isinstance(ops[0], producer.Revoke))
            or producer.plan_payload(parent, ops[0], sequence=seq, now=payload.issued_at) != raw):
        raise hosted.Error('allocation must be exactly one canonical promotion or withdrawal')
    return commit, path, blob, parent, payload, ops[0]


def iter_records(repo, before, after, *, proposed=False):
    for _, read in candidates(repo, before, after, proposed=proposed):
        yield read()


def delivery_plan(readers, prove):
    accepted = []
    blocked_at = None
    try:
        for seq, read in readers:
            try:
                row = read()
                parent = row[3]
                supersedes = parent and parent.sequence < seq - 1
                if blocked_at is not None and not (supersedes and parent.sequence < blocked_at):
                    continue
                prove(row)
                if supersedes:
                    accepted = [r for r in accepted if r[4].sequence <= parent.sequence]
                    blocked_at = None
                accepted.append(row)
            except Exception:
                if blocked_at is None:
                    blocked_at = seq
    except Exception:
        if not accepted:
            raise
        return accepted, True
    if blocked_at is not None and not accepted:
        raise hosted.Error(f'no authorized allocation before rejected sequence {blocked_at}')
    return accepted, blocked_at is not None


def selected_records(readers, sequences):
    if (not isinstance(sequences, list) or not sequences or any(type(n) is not int or n <= 0 for n in sequences)
            or sequences != sorted(set(sequences))):
        raise hosted.Error('invalid authorized sequence selection')
    chosen = []
    for seq, read in readers:
        if seq == sequences[len(chosen)]:
            chosen.append(read())
            if len(chosen) == len(sequences):
                return chosen
    raise hosted.Error('authorized selection missing from landing')


def records(repo, before, after):
    return list(iter_records(repo, before, after))


def evidence_path(sequence):
    return f"patches/model-hub-engine-allocation-evidence/{sequence:016}.json"


def load_receipt(repo, commit, payload, parent):
    """Read only the immutable input preflight checked; no moving stable or Actions lookup."""
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
    return result, record[2].decode()


def qualify_promotion(repo, commit, op, parent, payload, *, api, token):
    """Pre-merge only: independently check the committed evidence while artifacts exist."""
    from core.model_hub_engine_manifest import parse_release_manifest

    frozen, _ = load_receipt(repo, commit, payload, parent)
    run, attempt = str(frozen["run_id"]), frozen["run_attempt"]
    source = api.get(f"actions/runs/{run}/attempts/{attempt}")
    if (source.get("run_attempt") != attempt or str(source.get("id")) != run
            or source.get("status") != "completed" or source.get("conclusion") != "success"):
        raise hosted.Error("designated rehearsal attempt did not succeed")
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
        if json.dumps(result, sort_keys=True) != json.dumps(frozen, sort_keys=True):
            raise hosted.Error("downloaded rehearsal differs from immutable allocation evidence")
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
        def guard(stored, raw=payload.raw, prev=previous, seq=payload.sequence):
            hosted.check_published(stored, raw, prev.raw if prev else None, roots, verifier)
            if prev and prev.sequence < seq - 1 and stored != envelope:
                # The authenticated origin AND public object must still be this
                # reviewed parent. A racing PUT after this read loses the ETag CAS.
                served = fetch_public()
                if served != stored or producer.authenticate(served, roots=roots, verifier=verifier).raw != prev.raw:
                    raise hosted.Error('emergency withdrawal effective parent is not the verified public state')
        producer.publish(envelope, bucket=bucket, roots=roots, verifier=verifier, fetch_public=fetch_public, guard=guard)


def preflight(repo, before, after, *, api, token, single=False):
    """Read the synthetic PR/queue merge as Git data; never import its code."""
    request = recovery.read_request(repo, before, after, iter_records)
    if request:
        row, request_path, _ = request
        if single and hosted.git(repo, 'diff', '--name-status', '--no-renames', before, after).decode().splitlines() != ['A\t' + request_path]:
            raise hosted.Error('a recovery PR must add only its exact binding')
        recovery.require_missing(api, row)
    rows = list(iter_records(repo, before, after, proposed=True))
    if single and rows:
        if len(rows) != 1:
            raise hosted.Error("one allocation per PR")
        _, path, _, parent, payload, op = rows[0]
        allowed = {path} | ({evidence_path(payload.sequence)} if isinstance(op, producer.Promote) else set())
        if parent and parent.sequence < payload.sequence - 1:
            allowed.add(emergency.path(payload.sequence))
        changes = hosted.git(repo, "diff", "--name-status", "--no-renames", before, after).decode().splitlines()
        if sorted(changes) != sorted("A\t" + name for name in allowed):
            raise hosted.Error("allocation PR may add only its payload and required evidence")
    for commit, _, _, parent, payload, op in rows:
        if isinstance(op, producer.Promote):
            qualify_promotion(repo, commit, op, parent, payload, api=api, token=token)


def landing_range(repo, event_name, event, sha):
    if event_name == "workflow_dispatch":
        landing = (event.get("inputs") or {}).get("landing", "")
        if (not re.fullmatch("[0-9a-f]{40}", landing) or landing.encode() not in
                hosted.git(repo, "rev-list", "--first-parent", sha).splitlines()):
            raise hosted.Error("recovery requires an actual first-parent master landing")
        return hosted.git(repo, "rev-parse", landing + "^1").decode().strip(), landing
    if event.get("ref") != "refs/heads/master" or event.get("after") != sha or event.get("forced") is not False:
        raise hosted.Error("not an ordinary master push")
    return event["before"], sha


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("prepare", "sign", "publish", "preflight"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--avault")
    args = parser.parse_args(argv)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    sha, token = os.environ["GITHUB_SHA"], os.environ["GITHUB_TOKEN"]
    api = gate.ci.GitHubApi(producer.REPOSITORY, token)
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if args.role == "preflight":
        field = {"pull_request": "pull_request", "merge_group": "merge_group"}[event_name]
        base = event[field]["base"]["sha"] if field == "pull_request" else event[field]["base_sha"]
        if not all(re.fullmatch("[0-9a-f]{40}", s) for s in (sha, base)):
            raise hosted.Error("missing preflight Git identities")
        return preflight(args.data, base, sha, api=api, token=token, single=field == "pull_request")
    if os.environ.get("HOSTED_COMMISSIONED") != "true" or producer.run_context_problems(
            os.environ, events={"push", "workflow_dispatch"}, workflow_path=INDEX_WORKFLOW):
        raise hosted.Error("not the commissioned master index workflow")
    gate.ci._require_on_master(api, sha)
    before, landing = landing_range(args.data, event_name, event, sha)
    request = recovery.read_request(args.data, before, landing, iter_records)
    readers = candidates(args.data, before, landing)
    if request:
        row, request_path, request_blob = request
        if list(readers):
            raise hosted.Error('mixed allocation and recovery range: retry the individual landings')
        readers = [(row[4].sequence, lambda: row)]
    if args.role == "prepare":
        def prove(row):
            commit, path, blob, parent, payload, op = row
            if request:
                recovery.require_missing(api, row)
                gate.prove_landing(api, commit=landing, path=request_path, blob=request_blob, recovery=True)
                _, head, _ = gate.merged_identity(api, commit=commit, path=path, blob=blob)
            else:
                _, head = gate.prove_landing(api, commit=commit, path=path, blob=blob)
            if isinstance(op, producer.Promote):
                _, receipt_blob = load_receipt(args.data, commit, payload, parent)
                evidence = api.get("contents/" + evidence_path(payload.sequence), query={"ref": head})
                if evidence.get("type") != "file" or evidence.get("sha") != receipt_blob:
                    raise hosted.Error("landed evidence differs from preflighted head")
            if parent and parent.sequence < payload.sequence - 1:
                _, bound = emergency.declaration(args.data, commit, payload)
                if api.get('contents/' + emergency.path(payload.sequence), query={'ref': head}).get('sha') != bound:
                    raise hosted.Error('effective parent declaration differs from reviewed bytes')
        entries, rejected = delivery_plan(readers, prove)
        if not entries:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
                print("sequences=[]", file=fp)
            return
    else:
        entries = selected_records(readers, json.loads(os.environ["AUTHORIZED_SEQUENCES"]))
    roots = channel.parse_trust_roots(hosted.file_at(args.data, sha, hosted.ROOT))
    current_root = api.get("contents/" + hosted.ROOT, query={"ref": "master"})["sha"]
    if (len(roots.roots) != 1 or entries[0][4].sequence < roots.sequence_floor
            or current_root != hosted.git(args.data, "rev-parse", f"{sha}:{hosted.ROOT}").decode().strip()):
        raise hosted.Error("no matching commissioned root")
    verifier = producer.avault_verifier(args.avault)
    if args.role == "prepare":
        parent = entries[0][3]
        allowed = [row[4].raw for row in entries] + ([parent.raw] if parent else [])
        wait_for_parent(producer.fetch_public_index, allowed=allowed, parent=parent, roots=roots, verifier=verifier)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
            print("sequences=" + json.dumps([row[4].sequence for row in entries]), file=fp)
            print("rejected=" + str(rejected).lower(), file=fp)
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
