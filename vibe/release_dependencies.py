"""Resolve the *target* wheel and its release constraints before installation.

Stdlib-only: install.sh embeds this module so bootstrap needs no installed Avibe.
No package code is imported, and no archive members are extracted to disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CONSTRAINT_MEMBER = "vibe/dependency_constraints.txt"
PYPI_METADATA = "https://pypi.org/pypi/avibe-os/{version}json"
RELEASE_INDEX = "https://dl.avibe.bot/index/releases.json"
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024


def _get(url: str, limit: int) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Release downloads require HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "Avibe-release-installer"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("Release download redirected away from HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Release download exceeds size limit")
    return data


def _stable_version(value: str) -> tuple[int, ...] | None:
    return tuple(map(int, value.split("."))) if re.fullmatch(r"\d+\.\d+\.\d+", value) else None


def _official_wheel(version: str | None) -> tuple[str, str]:
    """PyPI first; the first-party release mirror is an independent route."""
    try:
        metadata = json.loads(_get(PYPI_METADATA.format(version=f"{version}/" if version else ""), MAX_METADATA_BYTES))
        wheels = [f for f in metadata["urls"] if f["filename"].endswith("-py3-none-any.whl") and not f.get("yanked")]
        if len(wheels) != 1:
            raise ValueError("Expected one non-yanked universal Avibe wheel")
        return wheels[0]["url"], wheels[0]["digests"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return _mirror_wheel(version)


def _mirror_wheel(version: str | None) -> tuple[str, str]:
    metadata = json.loads(_get(RELEASE_INDEX, MAX_METADATA_BYTES))
    if metadata.get("schema_version") != 1 or metadata.get("repository") != "avibe-bot/avibe":
        raise ValueError("Unexpected release mirror index")
    candidates = []
    for release in metadata.get("releases", []):
        tag = release.get("tag", "")
        selected = tag.removeprefix("gh-v").removeprefix("v")
        if version is not None and selected != version:
            continue
        key = _stable_version(selected)
        if version is None and (key is None or release.get("prerelease")):
            continue
        for asset in release.get("assets", []):
            if asset.get("name") == f"avibe_os-{selected}-py3-none-any.whl":
                # The index supplies digests, never an arbitrary download host.
                url = "https://dl.avibe.bot/releases/" + urllib.parse.quote(tag, safe="") + "/" + asset["name"]
                candidates.append((key or (), url, asset["sha256"]))
    if not candidates:
        raise ValueError("No matching official Avibe release wheel")
    _, url, digest = max(candidates)
    return url, digest


def prepare_release(spec: str, destination: Path) -> tuple[str, Path | None]:
    """Return a local wheel and its constraints, with explicit legacy compatibility.

    Only old published versions (<=3.2.5) may omit constraints. Source/editable
    installs are developer-owned and retain their existing package-manager path.
    """
    destination.mkdir(parents=True, exist_ok=True)
    direct = re.fullmatch(r"avibe[-_]os\s*@\s*(https://\S+)", spec)
    if direct:
        spec = direct[1]
    match = re.fullmatch(r"avibe[-_]os(?:==([0-9][A-Za-z0-9.+-]*))?", spec)
    parsed = urllib.parse.urlsplit(spec)
    local = Path(spec).expanduser()
    if match:
        url, digest = _official_wheel(match[1])
    elif parsed.scheme == "https" and parsed.path.endswith(".whl"):
        url = urllib.parse.urlunsplit(parsed._replace(fragment=""))
        digest = urllib.parse.parse_qs(parsed.fragment).get("sha256", [None])[0]
    elif local.is_file() and local.suffix == ".whl":
        url, digest = "", None
    else:
        # No default fallback ever enters this path. Explicit development sources
        # remain supported, and cannot be mislabeled a locked release install.
        return spec, None

    if url:
        try:
            data = _get(url, MAX_WHEEL_BYTES)
        except OSError:
            if not match or urllib.parse.urlsplit(url).hostname == "dl.avibe.bot":
                raise
            # Metadata can be reachable while the artifact CDN is not. Retry
            # only the same release, never resolve a different latest version.
            selected = Path(urllib.parse.urlsplit(url).path).name.removeprefix("avibe_os-").removesuffix("-py3-none-any.whl")
            url, digest = _mirror_wheel(selected)
            data = _get(url, MAX_WHEEL_BYTES)
        if (match or digest is not None) and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("Avibe wheel SHA-256 mismatch")
        name = Path(urllib.parse.urlsplit(url).path).name
        if not re.fullmatch(r"avibe_os-[A-Za-z0-9_.+!-]+-py3-none-any\.whl", name):
            raise ValueError("Expected a universal avibe-os wheel")
        wheel = destination / name
        wheel.write_bytes(data)
    else:
        wheel = local.resolve()

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1 or archive.getinfo(metadata_names[0]).file_size > MAX_METADATA_BYTES:
            raise ValueError("Invalid Avibe wheel metadata")
        from email.parser import BytesParser
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        if metadata.get("Name", "").lower().replace("_", "-") != "avibe-os":
            raise ValueError("Expected an avibe-os wheel")
        version = metadata.get("Version", "")
        if match and match[1] and version != match[1]:
            raise ValueError("Avibe wheel version does not match the requested release")
        if CONSTRAINT_MEMBER not in names:
            legacy_match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(?:a|b|rc)\d+)?", version)
            legacy = _stable_version(legacy_match[1]) if legacy_match else None
            if legacy is not None and legacy <= (3, 2, 5):
                return str(wheel), None
            raise ValueError("Avibe release wheel is missing dependency constraints")
        info = archive.getinfo(CONSTRAINT_MEMBER)
        if info.file_size > MAX_METADATA_BYTES or names.count(CONSTRAINT_MEMBER) != 1:
            raise ValueError("Invalid release constraints")
        content = archive.read(info).decode("utf-8")
        # Refuse pip options, URLs and includes in this installer-owned file.
        requirements = content.replace("\\\n", "").splitlines()
        count = 0
        for line in requirements:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9_.+!-]+(?:\s|;|$)", line):
                raise ValueError("Release constraints must contain exact registry pins")
            if re.search(r"--(?!hash=sha256:[0-9a-f]{64}(?:\s|$))", line):
                raise ValueError("Unexpected option in release constraints")
            count += 1
        if not count:
            raise ValueError("Empty release dependency constraints")
    constraints = destination / "dependency_constraints.txt"
    constraints.write_text(content, encoding="utf-8")
    return str(wheel), constraints


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    spec, constraints = prepare_release(args.spec, args.destination)
    (args.destination / "package-spec").write_text(spec, encoding="utf-8")
    (args.destination / "constraints-path").write_text(str(constraints) if constraints else "", encoding="utf-8")


if __name__ == "__main__":
    main()
