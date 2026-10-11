#!/usr/bin/env python3
"""Produce and verify desktop TEST release assets for the existing release workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from runpy import run_path


_versions = run_path(str(Path(__file__).with_name("release_package_version.py")))
package_version_from_release_tag = _versions["package_version_from_release_tag"]
_github = run_path(str(Path(__file__).with_name("github_release.py")))
DESKTOP_SOURCE_SCHEMA_VERSION = 2
CUA_DRIVER_SOURCES = Path(__file__).resolve().parents[1] / "desktop" / "cua-driver" / "sources.json"
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
    if re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        return tag[1:]
    match = RC_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("desktop TEST release requires a canonical gh-vX.Y.ZrcN or vX.Y.Z tag")
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


def prepare(
    version: str,
    tag: str,
    source_sha: str,
    config: Path,
    env_file: Path,
    target: str | None = None,
) -> None:
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
    if target is not None:
        if target not in TARGETS:
            raise ValueError("unknown desktop release target")
        if TARGETS[target][0] == "macos":
            bundle = override.setdefault("bundle", {})
            macos = bundle.setdefault("macOS", {})
            macos["files"] = {
                "Helpers/cua-driver": f"binaries/cua-driver-{target}",
            }
            resources = bundle.setdefault("resources", {})
            resources.update(
                {
                    "../cua-driver/policy.yaml": "computer-use/policy.yaml",
                    "../cua-driver/tools-v0.31.0.json": "computer-use/tools-v0.31.0.json",
                    "../cua-driver/LICENSE.md": "computer-use/LICENSE.md",
                    "../cua-driver/sources.json": "computer-use/sources.json",
                    f"binaries/cua-driver-{target}.provenance.json": "computer-use/driver-provenance.json",
                }
            )
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


def pinned_driver_source() -> dict:
    source = json.loads(CUA_DRIVER_SOURCES.read_text(encoding="utf-8"))["driver"]
    return {
        "version": source["version"],
        "tag": source["tag"],
        "source_commit": source["source_commit"],
        "release_checksums_sha256": source["release_checksums_sha256"],
        "archive": source["macos"]["asset"],
        "archive_sha256": source["macos"]["sha256"],
        "source_archive": source["source"]["archive"],
        "source_archive_sha256": source["source"]["sha256"],
        "patch_variant": source["patch"]["variant"],
        "patch_file": source["patch"]["file"],
        "patch_sha256": source["patch"]["sha256"],
        "tool_snapshot_sha256": source["tool_snapshot_sha256"],
    }


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


def driver_provenance(
    target: str,
    prepared_driver: Path,
    provenance_path: Path,
    *,
    packaged_driver: Path | None = None,
    packaged_cdhash: str | None = None,
) -> dict:
    if TARGETS[target][0] != "macos":
        raise ValueError("Cua Driver provenance is macOS-only")
    if prepared_driver.is_symlink() or not prepared_driver.is_file():
        raise ValueError("Invalid prepared Cua Driver provenance")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    required_hashes = (
        "source_archive_sha256",
        "patch_sha256",
        "tool_snapshot_sha256",
        "prepared_binary_sha256",
    )
    pinned = pinned_driver_source()
    patched_fields = (
        "version",
        "tag",
        "source_commit",
        "source_archive",
        "source_archive_sha256",
        "patch_variant",
        "patch_file",
        "patch_sha256",
        "tool_snapshot_sha256",
    )
    if (
        provenance.get("schema_version") != 2
        or any(provenance.get(field) != pinned[field] for field in patched_fields)
        or provenance.get("target") != target
        or provenance.get("arch") != TARGETS[target][1].replace("aarch64", "arm64")
        or provenance.get("build_origin") != "repository_patch_built"
        or any(re.fullmatch(r"[0-9a-f]{64}", str(provenance.get(field))) is None for field in required_hashes)
        or digest(prepared_driver) != provenance.get("prepared_binary_sha256")
    ):
        raise ValueError("Invalid prepared Cua Driver provenance")
    if packaged_driver is None and packaged_cdhash is None:
        return provenance
    if (
        packaged_driver is None
        or packaged_driver.is_symlink()
        or not packaged_driver.is_file()
        or re.fullmatch(r"[0-9a-f]{40,64}", str(packaged_cdhash)) is None
    ):
        raise ValueError("Invalid packaged Cua Driver provenance")
    return {
        **provenance,
        "packaged_sha256": digest(packaged_driver),
        "packaged_cdhash": packaged_cdhash,
    }


def record(*, version: str, target: str, tag: str, source_sha: str,
           installer: Path, runtime: Path, output: Path, signing: str,
           prepared_driver: Path | None = None,
           driver: Path | None = None, driver_provenance_path: Path | None = None,
           driver_cdhash: str | None = None) -> None:
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
    source = {
        "schema_version": DESKTOP_SOURCE_SCHEMA_VERSION,
        "tag": tag, "source_sha": source_sha,
        "version": version, "target": target,
        "package_version": manifest["runtime_version"],
    }
    driver_values = (prepared_driver, driver, driver_provenance_path, driver_cdhash)
    if TARGETS[target][0] == "macos":
        if any(value is None for value in driver_values):
            raise ValueError("macOS desktop artifacts require Cua Driver provenance")
        source["computer_use_driver"] = driver_provenance(
            target,
            prepared_driver,
            driver_provenance_path,
            packaged_driver=driver,
            packaged_cdhash=driver_cdhash,
        )
    elif any(value is not None for value in driver_values):
        raise ValueError("Windows desktop artifacts cannot carry macOS Cua Driver provenance")
    (output / names[2]).write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / names[3]).write_text(signing + "\n", encoding="utf-8")
    (output / names[4]).write_text(
        "".join(f"{digest(output / name)}  {name}\n" for name in sorted(names[:4])), encoding="utf-8",
    )


def verify(directory: Path, tag: str, source_sha: str, *, updater_enabled: bool = False) -> list[Path]:
    version = desktop_version_from_tag(tag)
    if SHA.fullmatch(source_sha) is None:
        raise ValueError("Invalid source SHA")
    expected = {name for target in TARGETS for name in asset_names(version, target)}
    if updater_enabled:
        updater = run_path(str(Path(__file__).with_name("desktop_updater.py")))
        for target in TARGETS:
            expected.update(updater["names"](version, target))
        public = os.environ.get("AVIBE_DESKTOP_UPDATER_PUBLIC_KEY", "")
        if not public.strip():
            raise ValueError("Missing updater verification public key")
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "public-key.txt"
            key.write_text(public, encoding="utf-8")
            updater["verify"](directory, tag, source_sha, key)
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
        schema_version = source.get("schema_version")
        if schema_version not in {1, DESKTOP_SOURCE_SCHEMA_VERSION}:
            raise ValueError("Desktop source provenance schema mismatch")
        expected_source = {"schema_version": schema_version, "tag": tag, "source_sha": source_sha,
                           "version": version, "target": target,
                           "package_version": package_version_from_release_tag(tag)}
        driver = source.pop("computer_use_driver", None)
        if TARGETS[target][0] == "macos":
            pinned = pinned_driver_source()
            legacy_fields = (
                "version",
                "tag",
                "source_commit",
                "release_checksums_sha256",
                "archive",
                "archive_sha256",
            )
            patched_fields = (
                "version",
                "tag",
                "source_commit",
                "source_archive",
                "source_archive_sha256",
                "patch_variant",
                "patch_file",
                "patch_sha256",
                "tool_snapshot_sha256",
            )
            legacy_hashes = (
                "release_checksums_sha256",
                "archive_sha256",
                "extracted_universal_sha256",
                "thinned_upstream_sha256",
                "packaged_sha256",
            )
            patched_hashes = (
                "source_archive_sha256",
                "patch_sha256",
                "tool_snapshot_sha256",
                "prepared_binary_sha256",
                "packaged_sha256",
            )
            if driver is None and schema_version == 1:
                # Schema-1 macOS artifacts predate the pinned Cua Driver
                # provenance block. Keep those already-published artifacts
                # verifiable. Schema 2 makes the block mandatory.
                pass
            elif not isinstance(driver, dict):
                raise ValueError("Desktop Cua Driver provenance mismatch")
            elif driver.get("schema_version") == 1:
                if (
                    any(driver.get(field) != pinned[field] for field in legacy_fields)
                    or driver.get("target") != target
                    or driver.get("arch")
                    != TARGETS[target][1].replace("aarch64", "arm64")
                    or driver.get("thinned_signature") != "upstream_preserved"
                    or re.fullmatch(
                        r"[0-9a-f]{40,64}", str(driver.get("packaged_cdhash"))
                    )
                    is None
                    or any(
                        re.fullmatch(r"[0-9a-f]{64}", str(driver.get(field))) is None
                        for field in legacy_hashes
                    )
                ):
                    raise ValueError("Desktop Cua Driver provenance mismatch")
            elif driver.get("schema_version") == 2:
                if (
                    any(driver.get(field) != pinned[field] for field in patched_fields)
                    or driver.get("target") != target
                    or driver.get("arch")
                    != TARGETS[target][1].replace("aarch64", "arm64")
                    or driver.get("build_origin") != "repository_patch_built"
                    or re.fullmatch(
                        r"[0-9a-f]{40,64}", str(driver.get("packaged_cdhash"))
                    )
                    is None
                    or any(
                        re.fullmatch(r"[0-9a-f]{64}", str(driver.get(field))) is None
                        for field in patched_hashes
                    )
                ):
                    raise ValueError("Desktop Cua Driver provenance mismatch")
            else:
                raise ValueError("Desktop Cua Driver provenance mismatch")
        elif driver is not None:
            raise ValueError("Windows desktop source unexpectedly carries Cua Driver provenance")
        if source != expected_source:
            raise ValueError("Desktop source provenance mismatch")
        if (directory / names[3]).read_text(encoding="utf-8") != signature(target):
            raise ValueError("Desktop TEST signature policy mismatch")
        validate_manifest(json.loads((directory / names[1]).read_text(encoding="utf-8")), target, tag)
    return [directory / name for name in sorted(expected)]


def check_remote(directory: Path, tag: str, source_sha: str, repo: str, *, complete: bool,
                 updater_enabled: bool = False) -> None:
    paths = verify(directory, tag, source_sha, updater_enabled=updater_enabled)
    if updater_enabled:
        if repo != "avibe-bot/avibe":
            raise ValueError("Updater repository mismatch")
        actual_source = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha"], text=True,
        ).strip()
        if actual_source != source_sha:
            raise ValueError("Live release tag no longer matches updater source")
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
    prep.add_argument("--target", choices=TARGETS)
    producer = sub.add_parser("record")
    producer.add_argument("--version", required=True)
    producer.add_argument("--target", choices=TARGETS, required=True)
    producer.add_argument("--tag", default="")
    producer.add_argument("--source-sha", required=True)
    producer.add_argument("--installer", type=Path, required=True)
    producer.add_argument("--runtime", type=Path, required=True)
    producer.add_argument("--output", type=Path, required=True)
    producer.add_argument("--signing", required=True)
    producer.add_argument("--prepared-driver", type=Path)
    producer.add_argument("--driver", type=Path)
    producer.add_argument("--driver-provenance-path", type=Path)
    producer.add_argument("--driver-cdhash")
    verifier = sub.add_parser("verify-driver-prepared")
    verifier.add_argument("--target", choices=TARGETS, required=True)
    verifier.add_argument("--driver", type=Path, required=True)
    verifier.add_argument("--provenance-path", type=Path, required=True)
    for command in ("verify", "check-remote"):
        consumer = sub.add_parser(command)
        consumer.add_argument("--directory", type=Path, required=True)
        consumer.add_argument("--updater-enabled", action="store_true")
        consumer.add_argument("--tag", required=True)
        consumer.add_argument("--source-sha", required=True)
        if command == "check-remote":
            consumer.add_argument("--repo", required=True)
            consumer.add_argument("--complete", action="store_true")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "resolve":
        # $GITHUB_OUTPUT is shared by the whole step: append, never replace
        # outputs the step already wrote (enabled/updater_enabled).
        with args["output"].open("a", encoding="utf-8") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in resolve(args["tag"]).items()))
    elif command == "prepare":
        prepare(**args)
    elif command == "record":
        record(**args)
    elif command == "verify-driver-prepared":
        driver_provenance(
            args["target"],
            args["driver"],
            args["provenance_path"],
        )
    elif command == "verify":
        verify(**args)
    else:
        check_remote(**args)


if __name__ == "__main__":
    main()
