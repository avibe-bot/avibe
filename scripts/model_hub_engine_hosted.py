"""Hosted channel signer. Executed from a reviewed tooling commit, never the data tree.

An allocation is an append-only master PR adding the exact payload bytes. A second
append-only PR records the verified envelope before *any* upload. Thus a failed
writer can only resume those bytes; it never calls the signer. Git history, not
an artifact or the bucket, carries allocated and signed-but-unpublished state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_channel as producer  # noqa: E402
from core import model_hub_engine_channel as channel  # noqa: E402

LEDGER = "patches/model-hub-engine-channel-ledger"
ROOT = "vibe/model_hub_runtime/engine_channel_roots.json"
KEY_NAME = "MODEL_HUB_ENGINE_CHANNEL"
Error = producer.ProducerError


def avault_run(binary, args, home, *, data=b""):
    env = {"HOME": str(home), "AVAULT_HOME": str(home), "AVAULT_STORE": "file", "PATH": os.defpath}
    proc = subprocess.run([str(binary), *args], input=data, capture_output=True, env=env, timeout=30)
    if proc.returncode:
        raise Error("dedicated avault operation failed")  # never echo secret-bearing diagnostics
    return proc.stdout


def generate_bundle(binary: Path, output: Path) -> str:
    """Owner-only helper: keep the user's actual machine store entirely out of scope."""
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        generated = json.loads(avault_run(binary, ["keypair", "generate", "--name", KEY_NAME, "--curve", "secp256k1"], home))
        phrase = secrets.token_urlsafe(48)
        wrapped = json.loads(avault_run(binary, ["key", "export"], home, data=phrase.encode()))
        bundle = {"import": {"passphrase": phrase, "blob": wrapped}, "envelope": generated["envelope"]}
        # The bundle IS a signing credential. Never print it, even on failure.
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as dest:
            json.dump(bundle, dest, separators=(",", ":"))
        return generated["public_key"]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def file_at(repo, sha, path):
    return git(repo, "show", f"{sha}:{path}")


def ledger_at(repo, sha, *, api):
    """Inspect Git objects (no imports, checkout filters, hooks or candidate executables)."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Error("invalid run commit")
    if git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Error("ledger requires complete master history")
    files = {}
    # Inspect *all* changes, including a deletion that removed a path from HEAD.
    history = git(repo, "log", "--first-parent", "--root", "--diff-merges=first-parent", "--no-renames",
                  "--format=commit:%H", "--name-status", sha, "--", LEDGER).decode().splitlines()
    commit = None
    additions = {}
    for line in history:
        if line.startswith("commit:"):
            commit = line[7:]
        elif line:
            match = re.fullmatch(rf"A\t{LEDGER}/([0-9]{{16}})\.(payload|envelope)\.json", line)
            if not match or not commit:
                raise Error("ledger is append-only: unexpected path, rewrite or deletion")
            sequence, kind = int(match[1]), match[2]
            if (sequence, kind) in files:
                raise Error("duplicate ledger path")
            path = line.split("\t")[1]
            files[sequence, kind] = path
            additions[sequence, kind] = commit
    if not files:
        raise Error("no PR allocation")
    last = max(n for n, _ in files)
    if last < 1 or {n for n, kind in files if kind == "payload"} != set(range(1, min(last, len(files)) + 1)) or last > len(files):
        raise Error("non-contiguous allocation history")
    if any((n, "envelope") not in files for n in range(1, last)):
        raise Error("unsettled earlier allocation")
    # Require actual merged pull requests, not just commits resembling merges.
    for kind in ("payload", "envelope"):
        tip = additions.get((last, kind))
        if not tip:
            continue
        prs = api(f"/repos/{producer.REPOSITORY}/commits/{tip}/pulls") or []
        if not any(p.get("merged_at") and p.get("merge_commit_sha") == tip and p.get("base", {}).get("ref") == "master"
                   and p["base"].get("repo", {}).get("full_name") == producer.REPOSITORY for p in prs):
            raise Error("latest ledger record was not added by a merged master PR")
    return last, files


def validate(repo, sha, *, operation, roots, verifier, published, api, change=None, baselines=None):
    current = api(f"/repos/{producer.REPOSITORY}/git/ref/heads/master") or {}
    if (current.get("object") or {}).get("sha") != sha:
        raise Error("stale master dispatch")
    last, files = ledger_at(repo, sha, api=api)
    previous = None
    previous_envelope = None
    for n in range(1, last + 1):
        raw = file_at(repo, sha, files[n, "payload"])
        payload = channel.parse_payload(raw)
        if payload.sequence != n:
            raise Error("sequence and PR path differ")
        if previous:
            channel.check_successor(producer._identities(previous), previous.revoked, payload)
        receipt_path = files.get((n, "envelope"))
        receipt = file_at(repo, sha, receipt_path) if receipt_path else None
        if receipt is not None:
            envelope, _ = channel.authenticate(receipt, roots=roots, verifier=verifier)
            if envelope.payload != raw:
                raise Error("receipt and PR allocation differ")
        if n == last:
            if n < roots.sequence_floor:
                raise Error("allocation below shipped floor")
            if operation == "resume":
                if receipt is None:
                    raise Error("resume requires a committed signed receipt")
                if published not in (previous_envelope, receipt):
                    raise Error("published object differs from committed history; investigate")
                return receipt
            if receipt is not None or published != previous_envelope:
                raise Error("resume the committed envelope; do not allocate or sign")
            if isinstance(change, producer.Promote) and any((baselines or {}).get(tag) != digest for tag, digest in producer.standing_releases(previous).items()):
                raise Error("rehearsal lacks a standing release")
            if change is None or producer.plan_payload(previous, change, now=payload.issued_at, sequence=n) != raw:
                raise Error("reviewed operation does not produce the exact allocated bytes")
            return raw
        previous, previous_envelope = payload, receipt
    raise Error("empty ledger")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "promote", "revoke", "resume"))
    parser.add_argument("--avault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--authorize-only", action="store_true")
    args = parser.parse_args(argv)
    binary = args.avault.resolve(strict=True)
    if args.action == "generate":
        print(generate_bundle(binary, args.output))
        return
    if os.environ.get("HOSTED_COMMISSIONED") != "true" or producer.run_context_problems(os.environ):
        raise Error("not a commissioned master dispatch")
    signing_bundle = os.environ.pop("HOSTED_SIGNING_BUNDLE", None)
    repo, sha = args.data, os.environ["GITHUB_SHA"]
    roots = channel.parse_trust_roots(file_at(repo, sha, ROOT))
    if len(roots.roots) != 1:
        raise Error("one explicitly commissioned channel root required")
    token = os.environ["GITHUB_TOKEN"]
    api = lambda path: producer.github_api(path, token=token)  # noqa: E731
    environment = producer.WRITER_ENVIRONMENT if args.action == "resume" else producer.SIGNER_ENVIRONMENT
    base = f"/repos/{producer.REPOSITORY}/environments/{environment}"
    problems, reviewers = producer.environment_protection_problems(api(base), api(base + "/deployment-branch-policies"))
    problems += producer.approval_problems(api(f"/repos/{producer.REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}/approvals"),
                                           environment=environment, reviewers=reviewers)
    if problems:
        raise Error("approval or deployment protection missing")
    if args.authorize_only:
        return
    verifier = producer.avault_verifier(str(binary))
    change = None
    tag = os.environ.get("ASSET_RELEASE_TAG", "")
    if args.action == "promote":
        if not channel.ASSET_TAG_RE.fullmatch(tag):
            raise Error("invalid candidate tag")
        # Copy only the two allowlisted data blobs; no candidate checkout is imported.
        from scripts.model_hub_engine_qualification import load_candidate
        with tempfile.TemporaryDirectory() as directory:
            candidate_dir = Path(directory) / tag
            candidate_dir.mkdir()
            for name in ("candidate.json", "model-hub-engine-manifest.json"):
                (candidate_dir / name).write_bytes(file_at(repo, sha, f"patches/model-hub-engine-candidates/{tag}/{name}"))
            candidate = load_candidate(candidate_dir)
            change = producer.Promote(candidate.manifest_bytes, candidate.min_avibe_version)
    elif args.action == "revoke":
        change = producer.Revoke(tag, os.environ["ROLLBACK_TO"], os.environ["REASON"])
    data = validate(repo, sha, operation=args.action, roots=roots, verifier=verifier, published=producer.fetch_public_index(),
                    api=api, change=change, baselines=json.loads(os.environ.get("REHEARSAL_BASELINES") or "{}"))
    if args.action != "resume":
        credential = json.loads(signing_bundle)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            avault_run(binary, ["key", "import"], home, data=json.dumps(credential["import"]).encode())
            def sign(request):
                return json.loads(avault_run(binary, ["sign"], home, data=request))["signature"]
            data = producer.sign_payload(data, key_id=roots.roots[0].key_id, key_name=KEY_NAME,
                                        key_envelope=credential["envelope"], roots=roots, verifier=verifier, sign=sign)
    args.output.write_bytes(data)
    print("public envelope sha256:", hashlib.sha256(data).hexdigest())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"hosted channel action refused ({type(exc).__name__})")  # no credential-bearing diagnostics
