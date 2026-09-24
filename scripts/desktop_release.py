#!/usr/bin/env python3
"""Produce and verify desktop TEST release assets for the existing release workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from runpy import run_path


_versions = run_path(str(Path(__file__).with_name("release_package_version.py")))
package_version_from_release_tag = _versions["package_version_from_release_tag"]
_github = run_path(str(Path(__file__).with_name("github_release.py")))
TARGETS = {
    "aarch64-apple-darwin": ("macos", "aarch64", ".dmg"),
    "x86_64-apple-darwin": ("macos", "x86_64", ".dmg"),
    "x86_64-pc-windows-msvc": ("windows", "x86_64", ".exe"),
}
RC_TAG = re.compile(r"gh-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)rc(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def desktop_version_from_tag(tag: str) -> str:
    match = RC_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("desktop TEST release requires a canonical gh-vX.Y.ZrcN tag")
    package_version = package_version_from_release_tag(tag)
    return package_version.replace("rc", "-rc.")


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def resolve(tag: str, source_sha: str | None = None) -> dict[str, str]:
    version = desktop_version_from_tag(tag)
    actual = git_output("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if SHA.fullmatch(actual) is None or (source_sha is not None and actual != source_sha):
        raise ValueError("desktop release tag does not match the exact source SHA")
    return {"tag": tag, "source_sha": actual, "version": version,
            "package_version": package_version_from_release_tag(tag)}


def prepare(version: str, tag: str, source_sha: str, config: Path, env_file: Path) -> None:
    match = SEMVER.fullmatch(version)
    if match is None or any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in (match["pre"] or "").split(".")
    ):
        raise ValueError("version must be valid SemVer")
    override = {"version": version}
    if tag:
        resolved = resolve(tag, source_sha)
        if git_output("rev-parse", "HEAD") != source_sha or version != resolved["version"]:
            raise ValueError("desktop checkout/version does not match the release tag")
        # The reusable TEST path receives no secrets and overrides any identity
        # configuration. The manual path retains its optional credential policy.
        override["bundle"] = {
            "macOS": {"signingIdentity": "-"},
            "windows": {"certificateThumbprint": None, "signCommand": None},
        }
        with env_file.open("a", encoding="utf-8") as stream:
            for key in ("SETUPTOOLS_SCM_PRETEND_VERSION", "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS"):
                stream.write(f"{key}={resolved['package_version']}\n")
    config.write_text(json.dumps(override) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def signature(target: str) -> str:
    if TARGETS[target][0] == "macos":
        return "app-adhoc\ndmg-unsigned-unnotarized\n"
    return "installer-unsigned\n"


def asset_names(version: str, target: str) -> list[str]:
    prefix = f"Avibe_{version}_{target}"
    return [prefix + TARGETS[target][2], prefix + ".runtime-manifest.json",
            prefix + ".SOURCE.json", prefix + ".SIGNATURE", prefix + ".SHA256SUMS"]


def validate_manifest(manifest: dict, target: str, tag: str) -> None:
    expected_os, arch, _ = TARGETS[target]
    if (manifest.get("schema_version"), manifest.get("os"), manifest.get("arch")) != (2, expected_os, arch):
        raise ValueError("Runtime manifest target/schema mismatch")
    if tag and manifest.get("runtime_version") != package_version_from_release_tag(tag):
        raise ValueError("bundled Avibe version does not match the release tag")
    for field in ("archive_sha256", "tree_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(manifest.get(field))) is None:
            raise ValueError(f"Invalid Runtime {field}")
    for field in ("archive_size", "entry_count", "unpacked_size"):
        if type(manifest.get(field)) is not int or manifest[field] <= 0:
            raise ValueError(f"Invalid Runtime {field}")


def record(*, version: str, target: str, tag: str, source_sha: str,
           installer: Path, runtime: Path, output: Path, signing: str) -> None:
    if SHA.fullmatch(source_sha) is None:
        raise ValueError("Invalid source SHA")
    if tag and (version != desktop_version_from_tag(tag) or signing != signature(target).strip()):
        raise ValueError("TEST version/signature policy mismatch")
    manifest_path = runtime / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest, target, tag)
    archive = runtime / "runtime.zip"
    if (manifest.get("archive") != archive.name or archive.stat().st_size != manifest["archive_size"]
            or digest(archive) != manifest["archive_sha256"]):
        raise ValueError("Runtime archive hash/size mismatch")
    if installer.suffix != TARGETS[target][2] or installer.stat().st_size == 0:
        raise ValueError("Missing or wrong installer")
    output.mkdir(parents=True, exist_ok=True)
    if list(output.iterdir()):
        raise ValueError("Desktop artifact output must be empty")
    names = asset_names(version, target)
    shutil.copyfile(installer, output / names[0])
    shutil.copyfile(manifest_path, output / names[1])
    (output / names[2]).write_text(json.dumps({
        "schema_version": 1, "tag": tag, "source_sha": source_sha,
        "version": version, "target": target,
        "package_version": manifest["runtime_version"],
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / names[3]).write_text(signing + "\n", encoding="utf-8")
    (output / names[4]).write_text(
        "".join(f"{digest(output / name)}  {name}\n" for name in sorted(names[:4])), encoding="utf-8",
    )


def verify(directory: Path, tag: str, source_sha: str) -> list[Path]:
    version = desktop_version_from_tag(tag)
    if SHA.fullmatch(source_sha) is None:
        raise ValueError("Invalid source SHA")
    expected = {name for target in TARGETS for name in asset_names(version, target)}
    if {path.name for path in directory.iterdir()} != expected:
        raise ValueError("Desktop asset set must contain exactly all three targets and their metadata")
    for target in TARGETS:
        names = asset_names(version, target)
        for name in names:
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Missing/empty/non-regular desktop asset: {name}")
        checksums = "".join(f"{digest(directory / name)}  {name}\n" for name in sorted(names[:4]))
        if (directory / names[4]).read_text(encoding="utf-8") != checksums:
            raise ValueError("Desktop asset hash mismatch")
        source = json.loads((directory / names[2]).read_text(encoding="utf-8"))
        if source != {"schema_version": 1, "tag": tag, "source_sha": source_sha,
                      "version": version, "target": target,
                      "package_version": package_version_from_release_tag(tag)}:
            raise ValueError("Desktop source provenance mismatch")
        if (directory / names[3]).read_text(encoding="utf-8") != signature(target):
            raise ValueError("Desktop TEST signature policy mismatch")
        validate_manifest(json.loads((directory / names[1]).read_text(encoding="utf-8")), target, tag)
    return [directory / name for name in sorted(expected)]


def check_remote(directory: Path, tag: str, source_sha: str, repo: str, *, complete: bool) -> None:
    paths = verify(directory, tag, source_sha)
    state = _github["get_release"](repo, tag)
    if state is None:
        if complete:
            raise ValueError("Release is missing before finalization")
        return
    remote = json.loads(subprocess.check_output(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"], text=True,
    ))
    names = {asset["name"] for asset in remote["assets"]}
    expected = {path.name for path in paths}
    if (complete or not state.draft) and not expected <= names:
        raise ValueError("Missing desktop assets; published tags cannot be retrofitted, cut a new rc")
    if complete:
        # Read back uploaded bytes before the sole workflow finalizer publishes.
        with tempfile.TemporaryDirectory(prefix="desktop-release-verify-") as temporary:
            for path in paths:
                subprocess.run(["gh", "release", "download", tag, "--repo", repo,
                                "--pattern", path.name, "--dir", temporary], check=True)
                if digest(Path(temporary) / path.name) != digest(path):
                    raise ValueError(f"Uploaded desktop asset differs: {path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--tag", required=True)
    resolver.add_argument("--output", type=Path, required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--version", required=True)
    prep.add_argument("--tag", default="")
    prep.add_argument("--source-sha", default="")
    prep.add_argument("--config", type=Path, required=True)
    prep.add_argument("--env-file", type=Path, required=True)
    producer = sub.add_parser("record")
    producer.add_argument("--version", required=True)
    producer.add_argument("--target", choices=TARGETS, required=True)
    producer.add_argument("--tag", default="")
    producer.add_argument("--source-sha", required=True)
    producer.add_argument("--installer", type=Path, required=True)
    producer.add_argument("--runtime", type=Path, required=True)
    producer.add_argument("--output", type=Path, required=True)
    producer.add_argument("--signing", required=True)
    for command in ("verify", "check-remote"):
        consumer = sub.add_parser(command)
        consumer.add_argument("--directory", type=Path, required=True)
        consumer.add_argument("--tag", required=True)
        consumer.add_argument("--source-sha", required=True)
        if command == "check-remote":
            consumer.add_argument("--repo", required=True)
            consumer.add_argument("--complete", action="store_true")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "resolve":
        args["output"].write_text("".join(f"{key}={value}\n" for key, value in resolve(args["tag"]).items()),
                                  encoding="utf-8")
    elif command == "prepare":
        prepare(**args)
    elif command == "record":
        record(**args)
    elif command == "verify":
        verify(**args)
    else:
        check_remote(**args)


if __name__ == "__main__":
    main()
