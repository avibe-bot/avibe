"""A signed channel may only spend a sequence on the exact immutable PR allocation."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import model_hub_engine_hosted as hosted
from scripts import model_hub_engine_index as index
from tests.engine_channel_support import manifest_bytes


def commit(repo):
    for args in (["add", "."], ["-c", "user.name=fixture", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"]):
        hosted.git(repo, *args)
    return hosted.git(repo, "rev-parse", "HEAD").decode().strip()


def fixture(tmp_path):
    repo = tmp_path / "data"
    repo.mkdir()
    hosted.git(repo, "init", "-q")
    (repo / "before").touch()
    before = commit(repo)
    change = hosted.producer.Promote(manifest_bytes(), "3.2.6")
    raw = hosted.producer.plan_payload(None, change, now=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1)
    path = repo / hosted.LEDGER / "0000000000000001.payload.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    sha = commit(repo)
    return repo, before, sha, path, change, raw


@pytest.mark.parametrize("failure", ["rewrite", "deleted", "sequence_gap", "noncanonical"])
def test_refuses_before_any_signature(tmp_path, failure):
    repo, before, sha, path, _, raw = fixture(tmp_path)
    if failure == "deleted":
        path.unlink()
    elif failure == "sequence_gap":
        path.rename(path.with_name("0000000000000003.payload.json"))
    else:
        if failure == "noncanonical":
            hosted.git(repo, "reset", "--soft", before)
        path.write_bytes(raw + b"\n")
    with pytest.raises(hosted.Error):
        index.records(repo, before, commit(repo))


@pytest.mark.skipif(not os.environ.get("AVIBE_TEST_HOSTED_KEYGEN"), reason="throwaway key only on isolated CI runner")
def test_isolated_owner_keygen_through_signed_pr_receipt_and_identical_resume(tmp_path, monkeypatch):
    # Only test-owned homes/stores; production root, domain payload and ledger never supplied.
    keygen = Path(os.environ["AVIBE_TEST_HOSTED_KEYGEN"])
    signer = Path(os.environ["AVIBE_TEST_AVAULT_VERIFIER"])
    repo, before, sha, payload_path, change, raw = fixture(tmp_path)
    credential = tmp_path / "private.json"
    result = subprocess.run([os.sys.executable, "-I", hosted.__file__, "generate", "--avault", str(keygen),
                             "--output", str(credential)], check=True, capture_output=True)
    public = result.stdout.decode().strip()
    assert re.fullmatch(r"0[23][0-9a-f]{64}", public) and not result.stderr
    roots_doc = {"schema_version": 1, "sequence_floor": 1, "roots": [{"key_id": "fixture", "public_key": public}]}
    roots = hosted.channel.parse_trust_roots(json.dumps(roots_doc).encode())
    verifier = hosted.producer.avault_verifier(str(signer))
    [(_, _, _, parent, payload, operation)] = index.records(repo, before, sha)
    assert parent is None and payload.raw == raw and operation == change
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
        hosted.check_published(published, raw, None, roots, verifier)
    # A stale writer cannot overwrite the first published allocation. Both
    # bad signature and divergent predecessor are rejected before the CAS.
    with pytest.raises(hosted.Error, match="diverges"):
        hosted.check_published(envelope, b"different allocation", None, roots, verifier)
    with pytest.raises(hosted.Error, match="superseding"):
        index.wait_for_parent(lambda: envelope, allowed=[], parent=None, roots=roots, verifier=verifier, timeout=0)
    index.wait_for_parent(lambda: envelope, allowed=[raw], parent=None, roots=roots, verifier=verifier, timeout=0)
    # Tampering after signing is never authenticated as the original bytes.
    with pytest.raises(hosted.channel.ChannelError):
        hosted.channel.authenticate(envelope.replace(b'"fixture"', b'"wrong"'), roots=roots, verifier=verifier)


@pytest.mark.parametrize("bad", ["run", "sha", "attempt", "event", "repo", "expired", "ambiguous", "digest", "missing", "ok"])
def test_artifact_wait_is_bounded_and_requires_exact_origin(bad):
    from scripts import model_hub_engine_artifact as artifacts

    sha, run = "f" * 40, "123"
    name = artifacts.artifact_name(run, sha, 1, b"public fixture", attempt="1")
    source = {"run_attempt": 2 if bad == "attempt" else 1, "head_sha": sha, "head_branch": "master", "id": 123,
              "event": "pull_request" if bad == "event" else "push", "path": index.INDEX_WORKFLOW,
              "repository": {"full_name": hosted.producer.REPOSITORY, "id": 100}}
    row = {"name": name, "id": 42, "expired": bad == "expired", "digest": "" if bad == "digest" else "sha256:" + sha + "0" * 24,
           "workflow_run": {"head_sha": "a" * 40 if bad == "sha" else sha, "id": 1 if bad == "run" else 123,
                            "repository_id": 5 if bad == "repo" else 100}, "size_in_bytes": 10}
    def api(path):
        if "/artifacts?" not in path:
            return source
        return {"total_count": 2 if bad == "ambiguous" else 1, "artifacts": [] if bad == "missing" else [row]}
    if bad == "ok":
        assert artifacts.wait_for_artifact(api, run, sha, name, attempt="1", timeout=0) == (42, row["digest"])
    else:
        with pytest.raises(hosted.Error):
            artifacts.wait_for_artifact(api, run, sha, name, attempt="1", timeout=0)


def test_artifact_hash_is_checked_before_using_its_single_file():
    from scripts import model_hub_engine_artifact as artifacts

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("model-hub-engine.json", b"public envelope fixture")
    raw = buf.getvalue()
    assert artifacts.unpack(raw, "sha256:" + hashlib.sha256(raw).hexdigest()) == b"public envelope fixture"
    with pytest.raises(hosted.Error, match="digest"):
        artifacts.unpack(raw, "sha256:" + "0" * 64)
