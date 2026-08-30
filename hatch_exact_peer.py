from __future__ import annotations

import base64
import csv
from email import policy
from email.parser import BytesParser
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import zipfile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


_METADATA_POLICY = policy.compat32.clone(linesep="\n", max_line_length=0)


def pin_peer_dependency(
    artifact_path: str,
    *,
    project_name: str,
    peer_name: str,
    package_version: str,
    peer_extra: str | None = None,
) -> None:
    artifact = Path(artifact_path)
    normalized_version = _normalize_version(package_version)
    try:
        if artifact.suffix == ".whl":
            _pin_wheel(
                artifact,
                project_name=project_name,
                peer_name=peer_name,
                package_version=normalized_version,
                peer_extra=peer_extra,
            )
        elif artifact.name.endswith(".tar.gz"):
            _pin_sdist(
                artifact,
                project_name=project_name,
                peer_name=peer_name,
                package_version=normalized_version,
                peer_extra=peer_extra,
            )
        else:
            raise RuntimeError(f"Unsupported publishable artifact for exact peer metadata: {artifact.name}")
    except Exception:
        artifact.unlink(missing_ok=True)
        raise


def _normalize_version(value: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion as exc:
        raise RuntimeError(f"Cannot pin peer metadata to invalid package version {value!r}") from exc


def _pin_metadata(
    content: bytes,
    *,
    project_name: str,
    peer_name: str,
    package_version: str,
    peer_extra: str | None,
) -> bytes:
    metadata = BytesParser(policy=policy.compat32).parsebytes(content)
    metadata_name = metadata.get("Name")
    metadata_version = metadata.get("Version")
    if not metadata_name or canonicalize_name(metadata_name) != canonicalize_name(project_name):
        raise RuntimeError(f"Artifact metadata is not for {project_name}")
    if not metadata_version or _normalize_version(metadata_version) != package_version:
        raise RuntimeError(
            f"{project_name} artifact metadata version {metadata_version!r} does not match built version {package_version}"
        )

    requirements = metadata.get_all("Requires-Dist") or []
    parsed: list[Requirement] = []
    for value in requirements:
        try:
            parsed.append(Requirement(value))
        except InvalidRequirement as exc:
            raise RuntimeError(f"{project_name} emitted invalid dependency metadata: {value}") from exc
    peer_indexes = [
        index
        for index, requirement in enumerate(parsed)
        if canonicalize_name(requirement.name) == canonicalize_name(peer_name)
    ]
    if len(peer_indexes) != 1:
        raise RuntimeError(f"{project_name} must declare exactly one {peer_name} dependency")

    peer_index = peer_indexes[0]
    peer_requirement = parsed[peer_index]
    if peer_requirement.url or peer_requirement.extras:
        raise RuntimeError(f"{project_name} cannot exact-pin a URL or extra-qualified {peer_name} dependency")
    if peer_extra is None:
        if peer_requirement.marker is not None:
            raise RuntimeError(f"{project_name} must require {peer_name} without an environment marker")
    elif peer_requirement.marker is None or not _is_exact_extra_marker(peer_requirement, peer_extra):
        raise RuntimeError(f"{project_name} must expose {peer_name} only through the {peer_extra!r} extra")

    marker = f"; {peer_requirement.marker}" if peer_requirement.marker is not None else ""
    requirements[peer_index] = f"{peer_name}=={package_version}{marker}"
    del metadata["Requires-Dist"]
    for requirement in requirements:
        metadata["Requires-Dist"] = requirement

    pinned = metadata.as_bytes(policy=_METADATA_POLICY)
    _verify_exact_pin(
        pinned,
        project_name=project_name,
        peer_name=peer_name,
        package_version=package_version,
        peer_extra=peer_extra,
    )
    return pinned


def _is_exact_extra_marker(requirement: Requirement, peer_extra: str) -> bool:
    if requirement.marker is None:
        return False
    return requirement.marker.evaluate({"extra": peer_extra}) and not requirement.marker.evaluate(
        {"extra": f"not-{peer_extra}"}
    )


def _verify_exact_pin(
    content: bytes,
    *,
    project_name: str,
    peer_name: str,
    package_version: str,
    peer_extra: str | None,
) -> None:
    metadata = BytesParser(policy=policy.compat32).parsebytes(content)
    requirements = metadata.get_all("Requires-Dist") or []
    peers = [
        Requirement(value)
        for value in requirements
        if canonicalize_name(Requirement(value).name) == canonicalize_name(peer_name)
    ]
    if len(peers) != 1 or str(peers[0].specifier) != f"=={package_version}":
        raise RuntimeError(f"{project_name} artifact did not emit the exact {peer_name}=={package_version} contract")
    if peer_extra is None:
        marker_matches = peers[0].marker is None
    else:
        marker_matches = peers[0].marker is not None and _is_exact_extra_marker(peers[0], peer_extra)
    if not marker_matches:
        raise RuntimeError(f"{project_name} artifact emitted the wrong marker for {peer_name}")


def _pin_wheel(
    artifact: Path,
    *,
    project_name: str,
    peer_name: str,
    package_version: str,
    peer_extra: str | None,
) -> None:
    with zipfile.ZipFile(artifact) as wheel:
        infos = wheel.infolist()
        entries = {info.filename: wheel.read(info.filename) for info in infos}
    if len(entries) != len(infos):
        raise RuntimeError(f"Wheel contains duplicate paths: {artifact.name}")

    metadata_names = [name for name in entries if name.endswith(".dist-info/METADATA")]
    record_names = [name for name in entries if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(record_names) != 1:
        raise RuntimeError(f"Wheel must contain one METADATA and one RECORD file: {artifact.name}")
    metadata_name = metadata_names[0]
    record_name = record_names[0]
    if metadata_name.rsplit("/", 1)[0] != record_name.rsplit("/", 1)[0]:
        raise RuntimeError(f"Wheel METADATA and RECORD identities disagree: {artifact.name}")

    entries[metadata_name] = _pin_metadata(
        entries[metadata_name],
        project_name=project_name,
        peer_name=peer_name,
        package_version=package_version,
        peer_extra=peer_extra,
    )
    entries[record_name] = _updated_record(
        entries[record_name], metadata_name=metadata_name, metadata=entries[metadata_name], record_name=record_name
    )

    temporary = _temporary_artifact_path(artifact)
    try:
        with zipfile.ZipFile(temporary, "w") as wheel:
            for info in infos:
                wheel.writestr(info, entries[info.filename])
        os.replace(temporary, artifact)
    finally:
        temporary.unlink(missing_ok=True)


def _updated_record(content: bytes, *, metadata_name: str, metadata: bytes, record_name: str) -> bytes:
    rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))
    metadata_rows = [row for row in rows if row and row[0] == metadata_name]
    record_rows = [row for row in rows if row and row[0] == record_name]
    if len(metadata_rows) != 1 or len(record_rows) != 1:
        raise RuntimeError("Wheel RECORD must name METADATA and itself exactly once")

    digest = base64.urlsafe_b64encode(hashlib.sha256(metadata).digest()).rstrip(b"=").decode("ascii")
    metadata_rows[0][1:] = [f"sha256={digest}", str(len(metadata))]
    record_rows[0][1:] = ["", ""]
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode("utf-8")


def _pin_sdist(
    artifact: Path,
    *,
    project_name: str,
    peer_name: str,
    package_version: str,
    peer_extra: str | None,
) -> None:
    with tarfile.open(artifact, "r:gz") as sdist:
        pax_headers = dict(sdist.pax_headers)
        members = sdist.getmembers()
        entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for member in members:
            source = sdist.extractfile(member) if member.isfile() else None
            entries.append((member, source.read() if source is not None else None))

    pkg_info_indexes = [
        index
        for index, (member, _content) in enumerate(entries)
        if member.isfile() and PurePosixPath(member.name).name == "PKG-INFO"
    ]
    if len(pkg_info_indexes) != 1:
        raise RuntimeError(f"Sdist must contain exactly one PKG-INFO file: {artifact.name}")
    pkg_info_index = pkg_info_indexes[0]
    member, content = entries[pkg_info_index]
    if content is None:
        raise RuntimeError(f"Sdist PKG-INFO is unreadable: {artifact.name}")
    pinned_content = _pin_metadata(
        content,
        project_name=project_name,
        peer_name=peer_name,
        package_version=package_version,
        peer_extra=peer_extra,
    )
    member.size = len(pinned_content)
    entries[pkg_info_index] = (
        member,
        pinned_content,
    )

    temporary = _temporary_artifact_path(artifact)
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax_headers) as sdist:
            for member, content in entries:
                sdist.addfile(member, io.BytesIO(content) if content is not None else None)
        os.replace(temporary, artifact)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_artifact_path(artifact: Path) -> Path:
    descriptor, path = tempfile.mkstemp(prefix=f".{artifact.name}.", suffix=".tmp", dir=artifact.parent)
    os.close(descriptor)
    return Path(path)
