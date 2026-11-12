#!/usr/bin/env python3
"""Prepare, publish and read back only the five-platform direct-transport release.

Preparation cannot publish. Publication requires a master dispatch with an exact
confirmation, re-proves qualification, and never writes the consumer manifest.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import ci_release_backstop as backstop  # noqa: E402
from scripts import release_workflow_artifacts as artifacts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPO = "avibe-bot/avibe"
WORKFLOW = ".github/workflows/direct-transport.yml"
TARGETS = {
    "darwin-arm64": "aarch64-apple-darwin", "darwin-x64": "x86_64-apple-darwin",
    "linux-arm64": "aarch64-unknown-linux-musl", "linux-x64": "x86_64-unknown-linux-musl",
    "win32-x64": "x86_64-pc-windows-msvc",
}
RECORD = "direct-transport-release.json"
NOTES = ("<!-- avibe:update-notification=none -->\n"
         "<!-- vibe-remote:update-notification=none -->\n\n"
         "Qualified stock Avibe direct-transport sidecar, not an Avibe application update "
         "or Apple XCFramework. Installation does not enable transport.\n")
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def identity(tag, source):
    version = tomllib.loads((ROOT / "direct-transport/Cargo.toml").read_text())["package"]["version"]
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source)), "source must be a full lowercase commit SHA")
    require(bool(re.fullmatch(r"direct-transport-v" + re.escape(version) + r"-[1-9][0-9]*", tag)),
            "tag must name the current crate version and positive release revision")
    return version


def api(path, payload=None, *, optional=False):
    command = ["gh", "api", f"repos/{REPO}/{path}"]
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=120)
    if optional and result.returncode and "HTTP 404" in result.stderr:
        return None
    require(result.returncode == 0, "GitHub request failed (no credential-bearing diagnostics logged)")
    return json.loads(result.stdout) if result.stdout.strip() else None


def release(tag):
    found = api(f"releases/tags/{tag}", optional=True)
    if found is not None:
        return found
    # Authenticated release lists also expose an interrupted draft publication.
    for page in range(1, 101):
        rows = api(f"releases?per_page=100&page={page}")
        require(isinstance(rows, list), "invalid release list")
        matches = [row for row in rows if row.get("tag_name") == tag]
        if matches:
            require(len(matches) == 1, "ambiguous release")
            return matches[0]
        if len(rows) < 100:
            return None
    raise ValueError("release pagination exceeded bound")


def latest():
    value = api("releases/latest")
    require(isinstance(value, dict) and value.get("draft") is False and value.get("prerelease") is False
            and re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value.get("tag_name", "")),
            "repository Latest must be a readable official stable release")
    return {key: value[key] for key in ("id", "tag_name")}


def qualified_run(source, run_id, attempt):
    run = artifacts.check_run(REPO, run_id, attempt, source, WORKFLOW, completed=True)
    require(run.get("event") in {"push", "workflow_dispatch"} and run.get("head_branch") == "master"
            and run.get("head_repository", {}).get("full_name") == REPO,
            "qualification must be a successful same-repository master run, never a PR artifact")
    return run


def verify_archive(path, target, source, run_id, attempt, external=None):
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_ARCHIVE,
            "invalid archive size or file")
    binary = "avibe-direct-transport" + (".exe" if target.endswith("windows-msvc") else "")
    with tarfile.open(path, "r:gz") as bundle:
        files, total, seen = {}, 0, set()
        for member in bundle:
            name = PurePosixPath(member.name)
            require(name.as_posix() == member.name and not name.is_absolute() and ".." not in name.parts
                    and "\\" not in member.name and ":" not in member.name and member.name not in seen,
                    "unsafe or duplicate tar member")
            seen.add(member.name)
            require(member.isfile() or member.isdir(), "archive links and devices are forbidden")
            require(member.name in {binary, "manifest.json", "Cargo.lock", "feature-graph.txt", "feature-evidence.json", "LICENSES"}
                    or member.name.startswith("LICENSES/"), "unexpected archive member")
            total += member.size
            require(0 <= member.size <= MAX_EXPANDED and total <= MAX_EXPANDED, "expanded archive exceeds bound")
            if member.isfile():
                files[member.name] = bundle.extractfile(member).read()
        required = {binary, "manifest.json", "Cargo.lock", "feature-graph.txt", "feature-evidence.json",
                    "LICENSES/NOTICE.md", "LICENSES/packages.json", "LICENSES/provenance.json"}
        rust = tomllib.loads((ROOT / "direct-transport/rust-toolchain.toml").read_text())["toolchain"]["channel"]
        required.update(f"LICENSES/rust-{rust}/{name}" for name in
                        ("COPYRIGHT.html", "licenses/Apache-2.0.txt", "licenses/MIT.txt"))
        require(required <= files.keys() and all(files[name] for name in required), "incomplete archive/notices")
        manifest = json.loads(files["manifest.json"])
        if external is not None:
            require(manifest == external, "inner/outer manifests differ")
        require(manifest.get("name") == "avibe-direct-transport" and manifest.get("target") == target
                and manifest.get("source_sha") == source and manifest.get("pull_request_head_sha") == source
                and manifest.get("workflow_run_id") == str(run_id) and manifest.get("workflow_run_attempt") == attempt,
                "archive source/run/target mismatch")
        cargo = tomllib.loads((ROOT / "direct-transport/Cargo.toml").read_text())
        require(manifest.get("version") == cargo["package"]["version"]
                and manifest.get("direct_dependencies") == cargo["dependencies"], "crate identity/dependencies mismatch")
        require(manifest.get("patched_dependencies") is False
                and manifest.get("distribution") == "qualified-workflow-artifact"
                and manifest.get("transport_liveness") == {"idle_timeout_ms": 6000, "keep_alive_ms": 1500},
                "not the stock qualified sidecar")
        require(manifest.get("binary") == binary and manifest.get("binary_sha256") == digest(files[binary])
                and manifest.get("binary_bytes") == len(files[binary]), "binary identity mismatch")
        require(b"AVIBE-PROBE-CID-" not in files[binary], "advisory binary cannot ship")
        require(files["Cargo.lock"] == (ROOT / "direct-transport/Cargo.lock").read_bytes()
                and manifest.get("cargo_lock_sha256") == digest(files["Cargo.lock"]), "lockfile mismatch")
        qualification = {
            "native_contract_tests": "passed", "resolved_feature_graph": "passed",
            "linux_static": "passed" if "linux" in target else "not-applicable",
            "linux_idle_and_outage_tolerance": "passed" if "linux" in target else "not-applicable",
            "linux_traffic_and_address_changes": "passed" if "linux" in target else "not-applicable",
            "private_apple_datapath": "passed-with-positive-control" if "apple" in target else "not-applicable",
        }
        require(manifest.get("qualification") == qualification, "missing required qualification")
        require(manifest.get("features") == json.loads(files["feature-evidence.json"]), "feature evidence mismatch")
        provenance = json.loads(files["LICENSES/provenance.json"])
        require(digest(files["LICENSES/provenance.json"]) == manifest.get("licenses_provenance_sha256")
                and provenance.get("lockfile_sha256") == manifest["cargo_lock_sha256"]
                and provenance.get("targets") == [target] and provenance.get("root_package") == "avibe-direct-transport"
                and provenance.get("policy_sha256") == file_hash(ROOT / "packaging/iroh-apple/license-sources.json"),
                "notice provenance mismatch")
        packages = json.loads(files["LICENSES/packages.json"])
        require(packages and packages == provenance.get("packages"), "empty/mismatched notice inventory")
        for package in packages:
            require(package.get("files") and all(files.get(name) for name in package["files"]), "missing package notice")
        return manifest


def prepare(tag, source, run_id, attempt, output):
    version = identity(tag, source)
    require(attempt > 0 and str(run_id).isdigit() and int(run_id) > 0, "invalid run/attempt")
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == source,
            "checkout must equal immutable release source")
    gh = backstop.GitHubApi(REPO, os.environ.get("GH_TOKEN", ""))
    backstop._require_on_master(gh, source)
    full = backstop.prove_backstop_sha(gh, source_sha=source)
    run = qualified_run(source, run_id, attempt)
    require(not output.exists(), "output must not already exist")
    output.mkdir(parents=True)
    manifest = {"schema_version": 1, "direct_transport_version": version, "release_state": "pending",
                "source": REPO, "source_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                "release_tag": tag, "archives": {}}
    record = {"schema_version": 1, "source_sha": source, "qualification_run_id": str(run_id),
              "qualification_attempt": attempt, "backstop": asdict(full), "artifacts": {}, "qualification": {},
              "candidate_manifest": manifest}
    with tempfile.TemporaryDirectory(prefix="sidecar-qualified-") as temp:
        for platform, target in TARGETS.items():
            job = artifacts.successful_job(REPO, run_id, attempt, target, run_started_at=run["run_started_at"])
            require(job is not None, f"missing qualified target job: {target}")
            name = f"avibe-direct-transport-{target}.tar.gz"
            artifact = artifacts.artifact_for_job(REPO, run_id, source, f"avibe-direct-transport-{target}-{source}", job)
            directory = Path(temp) / target
            artifacts.download_verified(REPO, artifact, directory)
            require({p.name for p in directory.iterdir()} == {name, name + ".sha256", "manifest.json"},
                    "unexpected workflow artifact layout")
            path = directory / name
            inner = verify_archive(path, target, source, run_id, attempt, json.loads((directory / "manifest.json").read_text()))
            checksum = file_hash(path)
            require((directory / (name + ".sha256")).read_text().split() == [checksum, name], "checksum sidecar mismatch")
            for filename in (name, name + ".sha256"):
                shutil.copyfile(directory / filename, output / filename)
            manifest["archives"][platform] = {
                "name": name, "url": f"https://github.com/{REPO}/releases/download/{tag}/{name}",
                "size": path.stat().st_size, "sha256": checksum, "binary_sha256": inner["binary_sha256"],
                "format": "tar.gz", "bin_path": inner["binary"],
            }
            record["artifacts"][target] = {"id": artifact["id"], "digest": artifact["digest"], "job_id": job["id"], "manifest": inner}
            if "linux" in target:
                record["qualification"][target] = {}
                for kind in ("idle", "network"):
                    evidence = artifacts.artifact_for_job(REPO, run_id, source, f"{kind}-qualification-{target}-{source}", job)
                    evidence_dir = Path(temp) / f"{kind}-{target}"
                    artifacts.download_verified(REPO, evidence, evidence_dir)
                    report = json.loads((evidence_dir / "report.json").read_text())
                    if kind == "idle":
                        require(report.get("all_passed") is True and report.get("mode") == "qualification"
                                and len(report.get("results", [])) == 11 and all(row.get("passed") is True for row in report["results"]),
                                "idle qualification incomplete")
                    else:
                        require(report.get("failures") == [], "network qualification incomplete")
                    record["qualification"][target][kind] = {"artifact_id": evidence["id"], "digest": evidence["digest"], "report": report}
    qualified_run(source, run_id, attempt)  # Fail if the producer was rerun during transfer.
    write_json(output / RECORD, record)
    return record


def public_download(url, destination, size):
    # Deliberately no authenticated API fallback: these are public availability proofs.
    request = urllib.request.Request(url, headers={"User-Agent": "avibe-sidecar-public-readback/1"})
    with urllib.request.urlopen(request, timeout=120) as incoming, destination.open("wb") as outgoing:
        total = 0
        while chunk := incoming.read(1024 * 1024):
            total += len(chunk)
            require(total <= size, "public asset exceeds pinned size")
            outgoing.write(chunk)
    require(total == size, "public asset size mismatch")


def verify_public(tag, source, directory, before):
    record = json.loads((directory / RECORD).read_text())
    state = release(tag)
    require(state is not None and state.get("tag_name") == tag and state.get("draft") is False
            and state.get("prerelease") is False and state.get("body") == NOTES + f"\nSource: `{source}`\n",
            "published release flags/notes mismatch")
    gh = backstop.GitHubApi(REPO, os.environ.get("GH_TOKEN", ""))
    require(backstop.resolve_tag_sha(gh, tag) == source, "public tag points to another source")
    assets = []
    for page in range(1, 101):
        rows = api(f"releases/{state['id']}/assets?per_page=100&page={page}")
        require(isinstance(rows, list), "invalid release assets")
        assets.extend(rows)
        if len(rows) < 100:
            break
    else:
        raise ValueError("too many release assets")
    expected = {p.name: p for p in directory.iterdir()}
    require(len(assets) == len(expected) and {a["name"] for a in assets} == expected.keys(), "public asset set mismatch")
    receipts = {}
    with tempfile.TemporaryDirectory(prefix="sidecar-public-") as temp:
        for asset in assets:
            name = asset["name"]
            local = expected[name]
            size, sha = local.stat().st_size, file_hash(local)
            url = f"https://github.com/{REPO}/releases/download/{tag}/{name}"
            require(asset.get("browser_download_url") == url and asset.get("size") == size
                    and asset.get("digest") == "sha256:" + sha and asset.get("state") == "uploaded", "public asset metadata mismatch")
            path = Path(temp) / name
            public_download(url, path, size)
            require(file_hash(path) == sha, "public downloaded bytes differ")
            receipts[name] = {"url": url, "size": size, "sha256": sha, "asset_id": asset["id"]}
        for platform, target in TARGETS.items():
            entry = record["candidate_manifest"]["archives"][platform]
            verify_archive(Path(temp) / entry["name"], target, source, record["qualification_run_id"], record["qualification_attempt"])
    require(latest() == before, "repository Latest changed; no pin emitted (inspect before retry)")
    pin = record["candidate_manifest"]
    pin["release_state"] = "published"
    pin["provenance"] = {"source_sha": source, "qualification_run_id": record["qualification_run_id"],
                         "qualification_attempt": record["qualification_attempt"], "release_record_sha256": file_hash(directory / RECORD)}
    return {"release_id": state["id"], "draft": False, "prerelease": False, "immutable": state.get("immutable"),
            "source_sha": source, "latest_before": before,
            "latest_after": before, "assets": receipts, "manifest_proposal": pin}


def publish(tag, source, directory, confirmation):
    record = json.loads((directory / RECORD).read_text())
    expected = f"publish {tag} {source} {record['qualification_run_id']}/{record['qualification_attempt']}"
    require(confirmation == expected, "exact publication confirmation required")
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REPOSITORY") == REPO and os.environ.get("GITHUB_REF") == "refs/heads/master"
            and os.environ.get("GITHUB_SHA") == source, "publication is only allowed from exact-source master workflow dispatch")
    # Reconstruct the complete packet from authenticated, exact-attempt artifacts;
    # no caller-supplied candidate file or fixture can authorize its own hashes.
    with tempfile.TemporaryDirectory(prefix="sidecar-reprove-") as temp:
        fresh = Path(temp) / "assets"
        prepare(tag, source, record["qualification_run_id"], record["qualification_attempt"], fresh)
        require({p.name for p in fresh.iterdir()} == {p.name for p in directory.iterdir()}
                and all(file_hash(p) == file_hash(directory / p.name) for p in fresh.iterdir()), "candidate packet changed")
    before = latest()
    refs = api(f"git/matching-refs/tags/{tag}")
    matches = [ref for ref in refs if ref.get("ref") == f"refs/tags/{tag}"]
    if matches:
        require(len(matches) == 1 and matches[0].get("object") == {"type": "commit", "sha": source,
                "url": f"https://api.github.com/repos/{REPO}/git/commits/{source}"}, "existing tag does not match immutable source")
    else:
        api("git/refs", {"ref": f"refs/tags/{tag}", "sha": source})
    state = release(tag)
    if state is None:
        state = api("releases", {"tag_name": tag, "target_commitish": source,
                    "name": f"Avibe direct transport {tag}", "body": NOTES + f"\nSource: `{source}`\n",
                    "draft": True, "prerelease": False, "make_latest": "false"})
    if state.get("draft") is True:
        require(state.get("tag_name") == tag and state.get("prerelease") is False
                and state.get("body") == NOTES + f"\nSource: `{source}`\n", "existing draft identity mismatch")
        existing = {a["name"]: a for a in state.get("assets", [])}
        require(existing.keys() <= {p.name for p in directory.iterdir()}, "draft has unexpected assets")
        for path in sorted(directory.iterdir()):
            if path.name in existing:
                require(existing[path.name].get("digest") == "sha256:" + file_hash(path)
                        and existing[path.name].get("size") == path.stat().st_size, "draft bytes differ; never clobber")
            else:
                subprocess.run(["gh", "release", "upload", tag, "--repo", REPO, str(path)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        gh = backstop.GitHubApi(REPO, os.environ.get("GH_TOKEN", ""))
        require(backstop.resolve_tag_sha(gh, tag) == source, "tag moved during upload; refusing publication")
        qualified_run(source, record["qualification_run_id"], record["qualification_attempt"])
        payload = {"draft": False, "prerelease": False, "make_latest": "false"}
        result = subprocess.run(["gh", "api", f"repos/{REPO}/releases/{state['id']}", "--method", "PATCH", "--input", "-"],
                                input=json.dumps(payload), text=True, capture_output=True, timeout=120)
        require(result.returncode == 0, "release finalization failed; inspect state before retry")
    return verify_public(tag, source, directory, before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "publish"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    try:
        prepare(args.tag, args.source, args.run_id, args.attempt, args.output)
        if args.command == "publish":
            receipt = publish(args.tag, args.source, args.output, args.confirmation)
            write_json(args.output.parent / "public-readback.json", receipt)
            write_json(args.output.parent / "direct_transport_runtime_manifest.proposal.json", receipt["manifest_proposal"])
    except Exception as exc:
        # Network errors may contain signed URLs; do not echo raw exceptions.
        print(f"Sidecar release proof failed ({type(exc).__name__}); no published pin emitted.", file=sys.stderr)
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
