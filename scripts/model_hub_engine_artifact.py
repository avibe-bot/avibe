"""Read one immutable envelope from the exact merged-index run and attempt.

GitHub's zip digest identifies transport bytes, not signing authority. The caller
must independently verify the inner envelope against the PR allocation and root.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import urllib.request
import zipfile

from scripts import model_hub_engine_channel as producer
from core.model_hub_engine_channel import MAX_ENVELOPE_BYTES

Error = producer.ProducerError
ZIP_LIMIT = MAX_ENVELOPE_BYTES + 65536


def artifact_name(run, sha, sequence, raw, *, attempt):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", attempt):
        raise Error("invalid run attempt")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Error("invalid artifact coordinates")
    return f"engine-{run}-attempt-{attempt}-{sha}-{sequence}-{hashlib.sha256(raw).hexdigest()}"


def wait_for_artifact(api, run, sha, name, *, attempt, timeout=300, clock=time.monotonic, sleep=time.sleep):
    base = f"/repos/{producer.REPOSITORY}/actions/runs/{run}"
    source = api(base) or {}
    if (str(source.get("id")) != run or str(source.get("run_attempt")) != attempt or source.get("head_sha") != sha
            or source.get("head_branch") != "master" or source.get("event") not in {"push", "workflow_dispatch"}
            or source.get("path") != ".github/workflows/model-hub-engine-index.yml" or (source.get("repository") or {}).get("full_name") != producer.REPOSITORY):
        raise Error("artifact source is not the exact channel workflow, repo, ref, SHA, run and attempt")
    deadline = clock() + timeout
    while True:
        listing = api(base + "/artifacts?name=" + name) or {}
        rows = listing.get("artifacts", [])
        if rows:
            if len(rows) != 1 or listing.get("total_count") != 1:
                raise Error("ambiguous artifact")
            row = rows[0]
            origin = row.get("workflow_run") or {}
            if (row.get("name") != name or row.get("expired") is not False or str(origin.get("id")) != run
                    or origin.get("head_sha") != sha or origin.get("repository_id") != source["repository"]["id"]
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("digest") or "")
                    or type(row.get("id")) is not int or row["id"] <= 0
                    or not 0 < row.get("size_in_bytes", 0) <= ZIP_LIMIT):
                raise Error("artifact metadata does not match this allocation")
            return row["id"], row["digest"]
        if clock() >= deadline:
            raise Error("signed artifact unavailable before deadline; rerun all jobs on the same original push")
        sleep(5)


def unpack(raw, digest):
    if len(raw) > ZIP_LIMIT or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise Error("artifact archive digest mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = archive.infolist()
        if len(files) != 1 or files[0].filename != "model-hub-engine.json" or not 0 < files[0].file_size <= MAX_ENVELOPE_BYTES:
            raise Error("artifact must contain exactly one bounded envelope")
        return archive.read(files[0])  # read bytes, never extract a path or execute an artifact


def download(artifact_id, digest, token):
    url = f"https://api.github.com/repos/{producer.REPOSITORY}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(url, headers={"User-Agent": "avibe-channel-writer"})
    req.add_unredirected_header("Authorization", "Bearer " + token)
    class HttpsOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not newurl.startswith("https://"):
                raise Error("artifact redirect did not use HTTPS")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    with urllib.request.build_opener(HttpsOnly).open(req, timeout=30) as response:
        return unpack(response.read(ZIP_LIMIT + 1), digest)
