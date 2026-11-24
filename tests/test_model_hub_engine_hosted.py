"""A signed channel may only spend a sequence on the exact immutable PR allocation."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import model_hub_engine_hosted as hosted
from tests.engine_channel_support import manifest_bytes


def commit(repo):
    for args in (["add", "."], ["-c", "user.name=fixture", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"]):
        hosted.git(repo, *args)
    return hosted.git(repo, "rev-parse", "HEAD").decode().strip()


def fixture(tmp_path):
    repo = tmp_path / "data"
    repo.mkdir()
    hosted.git(repo, "init", "-q")
    change = hosted.producer.Promote(manifest_bytes(), "3.2.6")
    raw = hosted.producer.plan_payload(None, change, now=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1)
    path = repo / hosted.LEDGER / "0000000000000001.payload.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    sha = commit(repo)
    return repo, sha, path, change, raw


def api_at(sha):
    def get(path):
        if path.endswith("/git/ref/heads/master"):
            return {"object": {"sha": sha}}
        assert path.endswith("/pulls")
        return [{"merged_at": "2026-01-01", "merge_commit_sha": path.split("/")[-2], "base": {
            "ref": "master", "repo": {"full_name": hosted.producer.REPOSITORY}}}]
    return get


@pytest.mark.parametrize("failure", ["rewritten", "deleted", "wrong_change", "not_pr", "stale_run", "unrecorded_published", "sequence_gap"])
def test_refuses_before_any_signature(tmp_path, failure):
    repo, sha, path, change, raw = fixture(tmp_path)
    operation, published = "promote", None
    if failure == "rewritten":
        path.write_bytes(raw + b"\n")
        sha = commit(repo)
    elif failure == "deleted":
        path.unlink()
        sha = commit(repo)
    elif failure == "wrong_change":
        change = hosted.producer.Promote(manifest_bytes(), "3.2.8")
    elif failure == "unrecorded_published":
        published = b"not the predecessor"
    elif failure == "sequence_gap":
        path.with_name("0000000000000003.payload.json").write_bytes(raw)
        sha = commit(repo)
    api = api_at("0" * 40 if failure == "stale_run" else sha)
    if failure == "not_pr":
        api = lambda p: [] if p.endswith("pulls") else {"object": {"sha": sha}}  # noqa: E731
    with pytest.raises((hosted.Error, hosted.channel.ChannelError)):
        hosted.validate(repo, sha, operation=operation, roots=hosted.channel.TrustRoots(),
                        verifier=None, published=published, api=api, change=change)


@pytest.mark.skipif(not os.environ.get("AVIBE_TEST_HOSTED_KEYGEN"), reason="throwaway key only on isolated CI runner")
def test_isolated_owner_keygen_through_signed_pr_receipt_and_identical_resume(tmp_path, monkeypatch):
    # Only test-owned homes/stores; production root, domain payload and ledger never supplied.
    keygen = Path(os.environ["AVIBE_TEST_HOSTED_KEYGEN"])
    signer = Path(os.environ["AVIBE_TEST_AVAULT_VERIFIER"])
    repo, sha, payload_path, change, raw = fixture(tmp_path)
    credential = tmp_path / "private.json"
    result = subprocess.run([os.sys.executable, "-I", hosted.__file__, "generate", "--avault", str(keygen),
                             "--output", str(credential)], check=True, capture_output=True)
    public = result.stdout.decode().strip()
    assert hosted.re.fullmatch(r"0[23][0-9a-f]{64}", public) and not result.stderr
    roots_doc = {"schema_version": 1, "sequence_floor": 1, "roots": [{"key_id": "fixture", "public_key": public}]}
    roots = hosted.channel.parse_trust_roots(json.dumps(roots_doc).encode())
    verifier = hosted.producer.avault_verifier(str(signer))
    assert hosted.validate(repo, sha, operation="promote", roots=roots, verifier=verifier, published=None,
                           api=api_at(sha), change=change) == (raw, None)
    secret = json.loads(credential.read_bytes())
    home = tmp_path / "only-on-test-runner"
    home.mkdir(mode=0o700)
    hosted.avault_run(signer, ["key", "import"], home, data=json.dumps(secret["import"]).encode())
    envelope = hosted.producer.sign_payload(raw, key_id="fixture", key_name=hosted.KEY_NAME, key_envelope=secret["envelope"],
        roots=roots, verifier=verifier, sign=lambda request: json.loads(hosted.avault_run(signer, ["sign"], home, data=request))["signature"])
    # Artifact loss cannot spend the same sequence on different bytes. Re-sign
    # in a separate process, not by caching/reusing the previous signature.
    again = hosted.producer.sign_payload(raw, key_id="fixture", key_name=hosted.KEY_NAME, key_envelope=secret["envelope"],
        roots=roots, verifier=verifier, sign=lambda request: json.loads(hosted.avault_run(signer, ["sign"], home, data=request))["signature"])
    assert again == envelope
    for published in (None, envelope):
        assert hosted.validate(repo, sha, operation="resume", roots=roots, verifier=verifier, published=published, api=api_at(sha)) == (raw, None)
    # Tampering after signing is never authenticated as the original bytes.
    with pytest.raises(hosted.channel.ChannelError):
        hosted.channel.authenticate(envelope.replace(b'"fixture"', b'"wrong"'), roots=roots, verifier=verifier)


@pytest.mark.parametrize("bad", ["run", "sha", "attempt", "event", "repo", "expired", "ambiguous", "digest", "missing", "ok"])
def test_artifact_wait_is_bounded_and_requires_exact_origin(bad):
    from scripts import model_hub_engine_artifact as artifacts

    sha, run = "f" * 40, "123"
    name = artifacts.artifact_name(run, sha, 1, b"public fixture")
    source = {"run_attempt": 2 if bad == "attempt" else 1, "head_sha": sha, "head_branch": "master", "id": 123,
              "event": "pull_request" if bad == "event" else "workflow_dispatch", "path": hosted.producer.WORKFLOW_PATH,
              "repository": {"full_name": hosted.producer.REPOSITORY, "id": 100}}
    row = {"name": name, "id": 42, "expired": bad == "expired", "digest": "" if bad == "digest" else "sha256:" + sha + "0" * 24,
           "workflow_run": {"head_sha": "a" * 40 if bad == "sha" else sha, "id": 1 if bad == "run" else 123,
                            "repository_id": 5 if bad == "repo" else 100}, "size_in_bytes": 10}
    def api(path):
        if "/artifacts?" not in path:
            return source
        return {"total_count": 2 if bad == "ambiguous" else 1, "artifacts": [] if bad == "missing" else [row]}
    if bad == "ok":
        assert artifacts.wait_for_artifact(api, run, sha, name, timeout=0) == (42, row["digest"])
    else:
        with pytest.raises(hosted.Error):
            artifacts.wait_for_artifact(api, run, sha, name, timeout=0)


def test_artifact_hash_is_checked_before_using_its_single_file():
    from scripts import model_hub_engine_artifact as artifacts

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("model-hub-engine.json", b"public envelope fixture")
    raw = buf.getvalue()
    assert artifacts.unpack(raw, "sha256:" + hashlib.sha256(raw).hexdigest()) == b"public envelope fixture"
    with pytest.raises(hosted.Error, match="digest"):
        artifacts.unpack(raw, "sha256:" + "0" * 64)


def test_a_rerun_cannot_reuse_approvals_from_the_first_attempt():
    with pytest.raises(hosted.Error, match="fresh run"):
        hosted.check_fresh_approvals(None, "123", "2")
