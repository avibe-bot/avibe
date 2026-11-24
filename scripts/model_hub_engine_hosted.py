"""Hosted channel signer. Executed from a reviewed tooling commit, never the data tree.

Git allocates exact payload bytes before signing. One approved dispatch starts
independent signer and writer jobs. Ephemeral artifacts identify transport, not
state. Re-signing those same bytes with the dedicated RFC6979 key is deterministic.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_artifact as artifacts  # noqa: E402
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
            match = re.fullmatch(rf"A\t{LEDGER}/([0-9]{{16}})\.payload\.json", line)
            if not match or not commit:
                raise Error("ledger is append-only: unexpected path, rewrite or deletion")
            sequence = int(match[1])
            if sequence in files:
                raise Error("duplicate ledger path")
            files[sequence] = line.split("\t")[1]
            additions[sequence] = commit
    if not files or sorted(files) != list(range(1, len(files) + 1)):
        raise Error("missing or non-contiguous PR allocations")
    last = len(files)
    tip = additions[last]
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
    identities = {}
    for n in range(1, last + 1):
        raw = file_at(repo, sha, files[n])
        payload = channel.parse_payload(raw)
        if payload.sequence != n:
            raise Error("sequence and PR path differ")
        channel.check_successor(identities, previous.revoked if previous else (), payload)
        identities.update(producer._identities(payload))
        if n == last:
            if n < roots.sequence_floor:
                raise Error("allocation below shipped floor")
            parent = previous.raw if previous else None
            check_published(published, raw, parent, roots, verifier)
            if operation != "resume":
                if isinstance(change, producer.Promote) and any((baselines or {}).get(t) != d for t, d in producer.standing_releases(previous).items()):
                    raise Error("rehearsal lacks a standing release")
                if change is None or producer.plan_payload(previous, change, now=payload.issued_at, sequence=n) != raw:
                    raise Error("reviewed operation does not produce the exact allocated bytes")
            return raw, parent
        previous = payload
    raise Error("empty ledger")


def check_published(published, allocated, parent, roots, verifier):
    if published is None:
        if parent is not None:
            raise Error("missing published predecessor")
        return
    envelope, _ = channel.authenticate(published, roots=roots, verifier=verifier)
    if envelope.payload not in (allocated, parent):
        raise Error("published object diverges from current allocation or its exact predecessor")


def check_fresh_approvals(api, run, attempt):
    # GitHub approval history is a run resource, not attempt-scoped. Never use
    # a previous attempt's approval: recovery is a fresh approved dispatch.
    if attempt != "1":
        raise Error("reruns cannot reuse approvals; dispatch a fresh run")
    approvals = api(f"/repos/{producer.REPOSITORY}/actions/runs/{run}/approvals")
    for environment in (producer.SIGNER_ENVIRONMENT, producer.WRITER_ENVIRONMENT):
        base = f"/repos/{producer.REPOSITORY}/environments/{environment}"
        problems, reviewers = producer.environment_protection_problems(api(base), api(base + "/deployment-branch-policies"))
        problems += producer.approval_problems(approvals, environment=environment, reviewers=reviewers)
        if problems:
            raise Error("both protected environments must be approved for this fresh run")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "promote", "revoke", "resume"))
    parser.add_argument("--avault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--role", choices=("sign", "publish"))
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
    run = os.environ["GITHUB_RUN_ID"]
    check_fresh_approvals(api, run, os.environ.get("GITHUB_RUN_ATTEMPT"))
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
    raw, parent = validate(repo, sha, operation=args.action, roots=roots, verifier=verifier, published=producer.fetch_public_index(),
                           api=api, change=change, baselines=json.loads(os.environ.get("REHEARSAL_BASELINES") or "{}"))
    sequence = channel.parse_payload(raw).sequence
    source = os.environ.get("SOURCE_RUN_ID") if args.action == "resume" else run
    name = artifacts.artifact_name(source or "", sha, sequence, raw)
    if args.role == "sign":
        if args.action == "resume":
            raise Error("artifact recovery never signs")
        credential = json.loads(signing_bundle)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            avault_run(binary, ["key", "import"], home, data=json.dumps(credential["import"]).encode())
            def sign(request):
                return json.loads(avault_run(binary, ["sign"], home, data=request))["signature"]
            data = producer.sign_payload(raw, key_id=roots.roots[0].key_id, key_name=KEY_NAME,
                                         key_envelope=credential["envelope"], roots=roots, verifier=verifier, sign=sign)
        args.output.write_bytes(data)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fp:
            print(f"artifact_name={name}", file=fp)
        return
    if args.role != "publish":
        raise Error("no privileged role")
    identifier, digest = artifacts.wait_for_artifact(api, source, sha, name)
    envelope = artifacts.download(identifier, digest, token)
    authenticated, _ = channel.authenticate(envelope, roots=roots, verifier=verifier)
    if authenticated.payload != raw:
        raise Error("signed payload does not match PR allocation byte for byte")
    account = os.environ["R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9a-f]{32}", account):
        raise Error("unexpected writer endpoint")
    bucket = producer.IndexBucket("avibe", f"https://{account}.r2.cloudflarestorage.com", producer._run_command)
    def guard(stored):
        # Called on exactly the bucket read whose ETag governs this write.
        if api(f"/repos/{producer.REPOSITORY}/git/ref/heads/master")["object"]["sha"] != sha:
            raise Error("master advanced during artifact wait")
        check_published(stored, raw, parent, roots, verifier)
    producer.publish(envelope, bucket=bucket, roots=roots, verifier=verifier, guard=guard)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"hosted channel action refused ({type(exc).__name__})")  # no credential-bearing diagnostics
