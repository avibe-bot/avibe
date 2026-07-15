from __future__ import annotations

import hashlib
import importlib.resources as package_resources
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from sysconfig import get_platform
from typing import Any

from config import paths
from core.dependency_network import (
    dependency_error_details,
    dependency_error_message,
    fetch_bytes,
    fetch_to_path,
    probe_url,
    redact_url,
)
from core.process_isolation import isolated_subprocess_kwargs


logger = logging.getLogger(__name__)

_TMUX_MANIFEST_RESOURCE = "tmux_runtime_manifest.json"
_TMUX_RUNTIME_SOURCE_MANIFEST = "manifest"
_TMUX_INSTALL_LOCK = threading.Lock()


@dataclass(frozen=True)
class TmuxArchive:
    platform: str
    name: str
    url: str
    sha256: str
    size: int | None = None
    bin_path: str = "tmux"


@dataclass(frozen=True)
class TmuxManifest:
    schema_version: int
    tmux_version: str
    source: str
    source_url: str | None
    requires_utf8proc: bool
    terminfo: str | None
    archives: dict[str, TmuxArchive]
    digest: str
    loaded_from: str


class TmuxRuntimeManager:
    """Install and resolve Avibe's vendored tmux binary.

    The future Web Terminal will prefer this deterministic tmux over any system
    tmux to avoid client/server protocol skew. The manifest source must point at
    builds made with utf8proc for correct macOS CJK width handling, and with
    terminfo handled by the archive or target platform. TERM/terminfo wiring is
    owned by the terminal PTY spawn layer, not this dependency manager.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir or paths.get_runtime_dir() / "tmux"
        manifest_path_value = manifest_path or os.environ.get("VIBE_TMUX_MANIFEST_PATH")
        self.manifest_path = Path(manifest_path_value).expanduser() if manifest_path_value else None
        self.manifest_url = manifest_url if manifest_url is not None else os.environ.get("VIBE_TMUX_MANIFEST_URL")
        self.offline = _env_flag_enabled("VIBE_TMUX_OFFLINE", default=False) if offline is None else offline
        self._install_reason: str | None = None
        self._download_error: dict[str, Any] | None = None

    def ensure(self, *, force: bool = False) -> dict[str, Any]:
        if not _TMUX_INSTALL_LOCK.acquire(blocking=False):
            return {
                "ok": False,
                "skipped": True,
                "reason": "tmux_install_already_running",
                "message": "tmux install or repair is already running; try again shortly.",
            }
        try:
            manifest = self._load_manifest()
            if not manifest:
                return self._failure(self._install_reason or "tmux_manifest_missing")
            archive = self._manifest_archive_for_platform(manifest)
            if not archive:
                return self._failure(self._install_reason or "tmux_platform_unsupported", manifest=manifest)
            install_dir = self._manifest_install_dir(manifest, archive)
            existing = self._verified_manifest_binary(install_dir, manifest, archive)
            if existing and not force:
                return {
                    "ok": True,
                    "installed": True,
                    "changed": False,
                    "path": str(existing),
                    "version": manifest.tmux_version,
                    "platform": archive.platform,
                    "install_dir": str(install_dir),
                }
            archive_path = self._resolve_manifest_archive(archive)
            if not archive_path:
                if existing:
                    return {
                        "ok": True,
                        "installed": True,
                        "changed": False,
                        "path": str(existing),
                        "version": manifest.tmux_version,
                        "platform": archive.platform,
                        "install_dir": str(install_dir),
                        "reason": self._install_reason,
                    }
                return self._failure(self._install_reason or "tmux_archive_unavailable", manifest=manifest, archive=archive)
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir = Path(tempfile.mkdtemp(prefix="manifest-", dir=self.runtime_dir))
            try:
                with tarfile.open(archive_path, "r:gz") as tar:
                    _safe_extract_tar(tar, tmp_dir)
                binary = tmp_dir / archive.bin_path
                if not binary.is_file():
                    return self._failure("tmux_install_missing_bin", manifest=manifest, archive=archive)
                _make_executable(binary)
                signing = self._prepare_macos_binary(binary)
                if not signing.get("ok"):
                    return {
                        **self._failure(str(signing.get("reason") or "tmux_codesign_failed"), manifest=manifest, archive=archive),
                        "signing": signing,
                    }
                if not _tmux_binary_runnable(binary):
                    return self._failure("tmux_binary_not_runnable", manifest=manifest, archive=archive)
                if install_dir.exists():
                    shutil.rmtree(install_dir)
                install_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_dir), str(install_dir))
                binary = install_dir / archive.bin_path
                self._write_manifest_install_metadata(install_dir, manifest, archive)
                self._write_current_pointer(manifest, archive, install_dir)
                self._install_reason = None
                return {
                    "ok": True,
                    "installed": True,
                    "changed": True,
                    "path": str(binary),
                    "version": manifest.tmux_version,
                    "platform": archive.platform,
                    "install_dir": str(install_dir),
                    "signing": signing,
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to install tmux runtime")
                return self._failure("tmux_install_failed", manifest=manifest, archive=archive, message=str(exc))
            finally:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        finally:
            _TMUX_INSTALL_LOCK.release()

    def resolve_binary(self) -> Path | None:
        manifest = self._load_manifest()
        if not manifest:
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if not archive:
            return None
        binary = self._verified_manifest_binary(self._manifest_install_dir(manifest, archive), manifest, archive)
        if binary:
            return binary
        return self._verified_manifest_binary(self._legacy_manifest_install_dir(manifest, archive), manifest, archive)

    def status(self) -> dict[str, Any]:
        manifest = self._load_manifest()
        platform_tag = _runtime_platform_tag()
        archive = manifest.archives.get(platform_tag) if manifest else None
        install_dir = self._manifest_install_dir(manifest, archive) if manifest and archive else None
        binary = self.resolve_binary() if manifest and archive else None
        version = _tmux_binary_version(binary) if binary else None
        return {
            "id": "tmux",
            "provider": _TMUX_RUNTIME_SOURCE_MANIFEST,
            "platform": platform_tag,
            "installed": binary is not None,
            "version": version or (manifest.tmux_version if manifest else None),
            "status": "ready" if binary else "missing",
            "path": str(binary) if binary else None,
            "install_dir": str(install_dir) if install_dir else None,
            "manifest": _manifest_status_payload(manifest),
            "archive": _archive_status_payload(archive),
            "reason": self._install_reason,
            "download_error": self._download_error,
        }

    def probe_archive_reachability(self, *, timeout: float = 10.0) -> dict[str, Any]:
        manifest = self._load_manifest()
        if manifest is None:
            return {
                "ok": False,
                "checked": bool(self._download_error),
                "reason": self._install_reason or "tmux_manifest_missing",
                "download_error": self._download_error,
            }
        archive = self._manifest_archive_for_platform(manifest)
        if archive is None:
            return {"ok": False, "checked": False, "reason": self._install_reason}
        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            return {
                "ok": False,
                "checked": False,
                "reason": "tmux_archive_url_unsupported",
                "url": redact_url(archive.url),
            }
        return probe_url(
            archive.url,
            timeout=timeout,
            opener=urllib.request.urlopen,
            user_agent="avibe-tmux-doctor",
        )

    def _load_manifest(self) -> TmuxManifest | None:
        payload: bytes | None = None
        loaded_from = ""
        if self.manifest_path:
            if not self.manifest_path.exists():
                self._install_reason = "tmux_manifest_missing"
                return None
            payload = self.manifest_path.read_bytes()
            loaded_from = str(self.manifest_path)
        elif self.manifest_url:
            if self.offline:
                self._install_reason = "tmux_manifest_unavailable_offline"
                return None
            try:
                payload = fetch_bytes(
                    self.manifest_url,
                    timeout=30,
                    opener=urllib.request.urlopen,
                )
                loaded_from = self.manifest_url
            except Exception as exc:
                logger.exception("Failed to download tmux manifest from %s", self.manifest_url)
                self._install_reason = "tmux_manifest_download_failed"
                self._download_error = dependency_error_details(exc, self.manifest_url)
                return None
        else:
            try:
                resource = package_resources.files("vibe").joinpath(_TMUX_MANIFEST_RESOURCE)
            except Exception:
                resource = None
            if resource is None or not resource.is_file():
                self._install_reason = "tmux_manifest_missing"
                return None
            payload = resource.read_bytes()
            loaded_from = f"package:{_TMUX_MANIFEST_RESOURCE}"
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload.decode("utf-8"))
            archives = {
                platform_tag: TmuxArchive(
                    platform=platform_tag,
                    name=str(item["name"]),
                    url=str(item["url"]),
                    sha256=str(item["sha256"]),
                    size=int(item["size"]) if item.get("size") is not None else None,
                    bin_path=str(item.get("bin_path") or "tmux"),
                )
                for platform_tag, item in (data.get("archives") or {}).items()
                if isinstance(item, dict)
            }
            manifest = TmuxManifest(
                schema_version=int(data.get("schema_version")),
                tmux_version=str(data.get("tmux_version") or ""),
                source=str(data.get("source") or ""),
                source_url=str(data.get("source_url") or "") or None,
                requires_utf8proc=bool(data.get("requires_utf8proc")),
                terminfo=str(data.get("terminfo") or "") or None,
                archives=archives,
                digest=digest,
                loaded_from=loaded_from,
            )
        except Exception:
            self._install_reason = "tmux_manifest_invalid"
            return None
        if manifest.schema_version != 1 or not manifest.tmux_version or not manifest.archives:
            self._install_reason = "tmux_manifest_invalid"
            return None
        return manifest

    def _manifest_archive_for_platform(self, manifest: TmuxManifest) -> TmuxArchive | None:
        platform_tag = _runtime_platform_tag()
        archive = manifest.archives.get(platform_tag)
        if not archive:
            self._install_reason = "tmux_platform_unsupported"
            return None
        return archive

    def _resolve_manifest_archive(self, archive: TmuxArchive) -> Path | None:
        cached = self.runtime_dir / "downloads" / archive.name
        if cached.exists() and self._downloaded_archive_matches(cached, archive):
            return cached
        if self.offline:
            self._install_reason = "tmux_archive_unavailable_offline"
            return None
        parsed = urllib.parse.urlparse(archive.url)
        if parsed.scheme not in {"https", "file"}:
            self._install_reason = "tmux_archive_url_unsupported"
            return None
        tmp_path = cached.with_suffix(cached.suffix + ".tmp")
        cached.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetch_to_path(
                archive.url,
                tmp_path,
                timeout=60,
                opener=urllib.request.urlopen,
            )
            self._download_error = None
            if not self._downloaded_archive_matches(tmp_path, archive):
                tmp_path.unlink(missing_ok=True)
                return None
            tmp_path.replace(cached)
            return cached
        except Exception as exc:
            logger.exception("Failed to download tmux archive from %s", archive.url)
            tmp_path.unlink(missing_ok=True)
            self._install_reason = "tmux_archive_download_failed"
            self._download_error = dependency_error_details(exc, archive.url)
            return None

    def _downloaded_archive_matches(self, path: Path, archive: TmuxArchive) -> bool:
        if archive.size is not None and path.stat().st_size != archive.size:
            self._install_reason = "tmux_archive_size_mismatch"
            return False
        if _file_sha256(path) != archive.sha256:
            self._install_reason = "tmux_archive_checksum_mismatch"
            return False
        return True

    def _manifest_install_dir(self, manifest: TmuxManifest, archive: TmuxArchive) -> Path:
        fingerprint = hashlib.sha256(f"{manifest.digest}:{archive.sha256}".encode("utf-8")).hexdigest()[:16]
        return (
            self.runtime_dir
            / "versions"
            / _safe_path_part(manifest.tmux_version)
            / _safe_path_part(archive.platform)
            / fingerprint
        )

    def _legacy_manifest_install_dir(self, manifest: TmuxManifest, archive: TmuxArchive) -> Path:
        return self.runtime_dir / "versions" / _safe_path_part(manifest.tmux_version) / _safe_path_part(archive.platform)

    def _manifest_metadata_path(self, install_dir: Path) -> Path:
        return install_dir / ".avibe-tmux-runtime.json"

    def _verified_manifest_binary(self, install_dir: Path, manifest: TmuxManifest, archive: TmuxArchive) -> Path | None:
        binary = install_dir / archive.bin_path
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return None
        if self._manifest_install_matches(install_dir, manifest, archive) and _tmux_binary_runnable(binary):
            return binary
        return None

    def _manifest_install_matches(self, install_dir: Path, manifest: TmuxManifest, archive: TmuxArchive) -> bool:
        try:
            payload = json.loads(self._manifest_metadata_path(install_dir).read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            payload.get("provider") == _TMUX_RUNTIME_SOURCE_MANIFEST
            and payload.get("manifest_sha256") == manifest.digest
            and payload.get("tmux_version") == manifest.tmux_version
            and payload.get("platform") == archive.platform
            and payload.get("archive_sha256") == archive.sha256
            and payload.get("bin_path") == archive.bin_path
        )

    def _write_manifest_install_metadata(self, install_dir: Path, manifest: TmuxManifest, archive: TmuxArchive) -> None:
        self._manifest_metadata_path(install_dir).write_text(
            json.dumps(
                {
                    "provider": _TMUX_RUNTIME_SOURCE_MANIFEST,
                    "manifest_sha256": manifest.digest,
                    "tmux_version": manifest.tmux_version,
                    "platform": archive.platform,
                    "archive_name": archive.name,
                    "archive_sha256": archive.sha256,
                    "bin_path": archive.bin_path,
                    "manifest_source": manifest.loaded_from,
                    "source": manifest.source,
                    "requires_utf8proc": manifest.requires_utf8proc,
                    "terminfo": manifest.terminfo,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_current_pointer(self, manifest: TmuxManifest, archive: TmuxArchive, install_dir: Path) -> None:
        pointer = self.runtime_dir / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "provider": _TMUX_RUNTIME_SOURCE_MANIFEST,
                    "tmux_version": manifest.tmux_version,
                    "platform": archive.platform,
                    "install_dir": str(install_dir),
                    "manifest_sha256": manifest.digest,
                    "archive_sha256": archive.sha256,
                    "bin_path": archive.bin_path,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _prepare_macos_binary(self, binary: Path) -> dict[str, Any]:
        if sys_platform() != "darwin":
            return {"ok": True, "skipped": True, "reason": "not_macos"}
        quarantine = _strip_quarantine(binary)
        if _codesign_valid(binary):
            return {"ok": True, "changed": False, "quarantine": quarantine}
        codesign = shutil.which("codesign")
        if not codesign:
            return {"ok": False, "reason": "codesign_missing", "quarantine": quarantine}
        proc = subprocess.run(
            [codesign, "-f", "-s", "-", str(binary)],
            capture_output=True,
            text=True,
            timeout=30,
            **isolated_subprocess_kwargs(),
        )
        verified = proc.returncode == 0 and _codesign_valid(binary)
        return {
            "ok": verified,
            "changed": proc.returncode == 0,
            "reason": None if verified else ("codesign_failed" if proc.returncode != 0 else "codesign_verify_failed"),
            "output": _truncate((proc.stdout or "") + (proc.stderr or "")),
            "quarantine": quarantine,
        }

    def _failure(
        self,
        reason: str,
        *,
        manifest: TmuxManifest | None = None,
        archive: TmuxArchive | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        self._install_reason = reason
        return {
            "ok": False,
            "installed": False,
            "changed": False,
            "reason": reason,
            "message": message
            or (
                dependency_error_message(self._download_error, label="tmux dependency download")
                if self._download_error
                else _reason_message(reason)
            ),
            "version": manifest.tmux_version if manifest else None,
            "platform": archive.platform if archive else _runtime_platform_tag(),
            "path": None,
            "output": None,
            "download_error": self._download_error,
        }


def get_tmux_runtime_manager(**kwargs: Any) -> TmuxRuntimeManager:
    return TmuxRuntimeManager(**kwargs)


def ensure_tmux_installed(force: bool = False) -> dict[str, Any]:
    return get_tmux_runtime_manager().ensure(force=force)


def resolve_tmux_binary() -> Path | None:
    return get_tmux_runtime_manager().resolve_binary()


def tmux_status() -> dict[str, Any]:
    return get_tmux_runtime_manager().status()


def _runtime_platform_tag() -> str:
    raw = get_platform().lower()
    machine = raw.rsplit("-", 1)[-1]
    if machine == "universal2":
        machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = machine
    if raw.startswith("macosx"):
        os_name = "darwin"
    elif raw.startswith("linux"):
        os_name = "linux"
    elif raw.startswith("win"):
        os_name = "win32"
    else:
        os_name = os.name
    return f"{os_name}-{arch}"


def sys_platform() -> str:
    return sys.platform


def _codesign_valid(binary: Path) -> bool:
    codesign = shutil.which("codesign")
    if not codesign:
        return False
    try:
        proc = subprocess.run(
            [codesign, "-v", str(binary)],
            capture_output=True,
            text=True,
            timeout=10,
            **isolated_subprocess_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def _strip_quarantine(binary: Path) -> dict[str, Any]:
    xattr = shutil.which("xattr")
    if not xattr:
        return {"ok": True, "skipped": True, "reason": "xattr_missing"}
    proc = subprocess.run(
        [xattr, "-d", "com.apple.quarantine", str(binary)],
        capture_output=True,
        text=True,
        timeout=10,
        **isolated_subprocess_kwargs(),
    )
    if proc.returncode == 0:
        return {"ok": True, "changed": True}
    text = (proc.stderr or proc.stdout or "").lower()
    if "no such xattr" in text or "no such file" in text:
        return {"ok": True, "changed": False}
    return {"ok": False, "changed": False, "reason": "xattr_failed", "output": _truncate(proc.stderr or proc.stdout or "")}


def _tmux_binary_runnable(binary: Path) -> bool:
    return _tmux_binary_version(binary) is not None


def _tmux_binary_version(binary: Path | None) -> str | None:
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary), "-V"],
            capture_output=True,
            text=True,
            timeout=5,
            **isolated_subprocess_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    if not text:
        return None
    parts = text.split()
    return parts[-1] if parts else text


def _safe_extract_tar(tar: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    for member in tar.getmembers():
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError(f"Unsupported tmux archive member type: {member.name}")
        if _tar_archive_path_is_unsafe(member.name):
            raise ValueError(f"Unsafe tmux archive member path: {member.name}")
        if (member.issym() or member.islnk()) and _tar_archive_path_is_unsafe(member.linkname):
            raise ValueError(f"Unsafe tmux archive link target: {member.name}")
        target = (destination / member.name).resolve()
        if target != destination_resolved and destination_resolved not in target.parents:
            raise ValueError(f"Unsafe tmux archive member path: {member.name}")
        if member.issym():
            link_target = Path(member.linkname)
            resolved_link = (target.parent / link_target).resolve() if not link_target.is_absolute() else link_target.resolve()
            if resolved_link != destination_resolved and destination_resolved not in resolved_link.parents:
                raise ValueError(f"Unsafe tmux archive link target: {member.name}")
        if member.islnk():
            link_target = Path(member.linkname)
            resolved_link = (destination / link_target).resolve()
            if resolved_link != destination_resolved and destination_resolved not in resolved_link.parents:
                raise ValueError(f"Unsafe tmux archive link target: {member.name}")
    if sys.version_info >= (3, 12):
        tar.extractall(destination, filter="data")
    else:
        tar.extractall(destination)


def _tar_archive_path_is_unsafe(value: str) -> bool:
    if not value:
        return True
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute():
        return True
    if windows_path.drive or windows_path.root:
        return True
    return ".." in posix_path.parts or ".." in windows_path.parts


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _safe_path_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip(".-") or "unknown"


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _manifest_status_payload(manifest: TmuxManifest | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    return {
        "schema_version": manifest.schema_version,
        "tmux_version": manifest.tmux_version,
        "source": manifest.source,
        "source_url": manifest.source_url,
        "requires_utf8proc": manifest.requires_utf8proc,
        "terminfo": manifest.terminfo,
        "sha256": manifest.digest,
        "loaded_from": manifest.loaded_from,
    }


def _archive_status_payload(archive: TmuxArchive | None) -> dict[str, Any] | None:
    if archive is None:
        return None
    return {
        "platform": archive.platform,
        "name": archive.name,
        "url": redact_url(archive.url),
        "sha256": archive.sha256,
        "size": archive.size,
        "bin_path": archive.bin_path,
    }


def _reason_message(reason: str) -> str:
    messages = {
        "tmux_archive_checksum_mismatch": "tmux archive checksum did not match the pinned manifest.",
        "tmux_archive_size_mismatch": "tmux archive size did not match the pinned manifest.",
        "tmux_platform_unsupported": "No pinned tmux runtime is available for this platform.",
        "tmux_manifest_missing": "tmux runtime manifest is missing.",
        "tmux_binary_not_runnable": "tmux runtime binary could not be executed after installation.",
        "tmux_install_failed": "tmux runtime install failed.",
    }
    return messages.get(reason, reason)


def _truncate(output: str, limit: int = 4096) -> str:
    return output if len(output) <= limit else "...(truncated)\n" + output[-limit:]
