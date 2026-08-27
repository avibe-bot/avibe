from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from config import paths
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
    runtime_platform_tag,
    safe_path_part,
)
from core.process_isolation import isolated_subprocess_kwargs


logger = logging.getLogger(__name__)

_TMUX_MANIFEST_RESOURCE = "tmux_runtime_manifest.json"
_RELEASED_PACKAGED_MANIFEST_SHA256 = "ee2826f881c236718ff18b2d1f939afb9417c584df5f29b796129c80691d2e63"
_RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256 = {
    "88323402bd28d21103239caf009b130086ebf334807de485d4a1e1c7188ee810": (
        "5f8b6a7eda2ccd5bc283368e93e0e5c45b78071b5f90df7e394cf9a7f7ed6373"
    ),
    "073f6e2c2baa7eb5d643563600ee6052ca8619f3ec5a0cfdf99c56397fb72c94": (
        "9adf4f75e12bce1e1a3b53696e38b60854761bb7945a7c09a806999f1995b870"
    ),
    "fd4a2206c5e468dd2ee4e9a65f2d40e0762551965d7fdbe849c494ab14f513e9": (
        "6d1796de251b47b183af1b3eb6e229161e31560e89b9a8a1159533071eae2970"
    ),
    "002a6f4fd52212600fa0d72d865dcf328e5b8b6e83c179788144d8587b75677a": (
        "5a06c01c36998aaf1726ccaaeae01dded2b5bf82dbd22a01889f0d05b4d11c80"
    ),
}
_TMUX_SPEC = ManagedRuntimeSpec(
    runtime_id="tmux",
    manifest_resource=_TMUX_MANIFEST_RESOURCE,
    version_field="tmux_version",
    default_bin_path="tmux",
    allow_legacy_missing_runtime_id=True,
)


class TmuxRuntimeManager(ManagedRuntimeManager):
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
        manifest_path_value = manifest_path or os.environ.get("VIBE_TMUX_MANIFEST_PATH")
        super().__init__(
            spec=_TMUX_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "tmux",
            manifest_path=manifest_path_value,
            manifest_url=manifest_url if manifest_url is not None else os.environ.get("VIBE_TMUX_MANIFEST_URL"),
            offline=env_flag_enabled("VIBE_TMUX_OFFLINE") if offline is None else offline,
        )

    def _parse_manifest(self, payload: bytes, *, loaded_from: str) -> ManagedRuntimeManifest | None:
        """Read released schema-1 manifests that predate binary digests."""

        compatible_payload = payload
        original_data: dict[str, Any] | None = None
        enriched = False
        try:
            data = json.loads(payload.decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("archives"), dict):
                original_data = data
                archives: dict[str, Any] = {}
                for platform_tag, raw_archive in data["archives"].items():
                    if not isinstance(raw_archive, dict):
                        archives[platform_tag] = raw_archive
                        continue
                    archive = dict(raw_archive)
                    if archive.get("binary_sha256") is None:
                        archive_sha256 = str(archive.get("sha256") or "").lower()
                        binary_sha256 = _RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256.get(archive_sha256)
                        if binary_sha256 is not None:
                            archive["binary_sha256"] = binary_sha256
                            enriched = True
                    archives[platform_tag] = archive
                if enriched:
                    compatible_payload = json.dumps(
                        {**data, "archives": archives},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
        except (UnicodeError, ValueError, TypeError):
            pass

        manifest = super()._parse_manifest(compatible_payload, loaded_from=loaded_from)
        if manifest is None or not enriched or original_data is None:
            return manifest
        return replace(
            manifest,
            digest=hashlib.sha256(payload).hexdigest(),
            payload=original_data,
        )

    def _manifest_install_candidates(
        self,
        manifest: ManagedRuntimeManifest,
        archive: ManagedRuntimeArchive,
    ) -> Iterator[Path]:
        """Include the two install layouts written by released tmux managers."""

        version_dir = (
            self.runtime_dir
            / "versions"
            / safe_path_part(manifest.runtime_version)
            / safe_path_part(archive.platform)
        )
        released_manifest_digests = [manifest.digest]
        if archive.sha256 in _RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256:
            released_manifest_digests.append(_RELEASED_PACKAGED_MANIFEST_SHA256)
        released_fingerprints = (
            hashlib.sha256(f"{manifest_sha256}:{archive.sha256}".encode("utf-8")).hexdigest()[:16]
            for manifest_sha256 in dict.fromkeys(released_manifest_digests)
        )
        candidates = (
            *super()._manifest_install_candidates(manifest, archive),
            *(version_dir / fingerprint for fingerprint in released_fingerprints),
            version_dir,
        )
        yield from dict.fromkeys(candidates)

    def _metadata_matches_install_target(
        self,
        metadata: Mapping[str, Any],
        target: Mapping[str, str],
    ) -> bool:
        if super()._metadata_matches_install_target(metadata, target):
            return True
        archive_sha256 = target.get("archive_sha256")
        return (
            metadata.get("runtime_id") is None
            and metadata.get("provider") == self.spec.record_provider
            and metadata.get("manifest_sha256") == _RELEASED_PACKAGED_MANIFEST_SHA256
            and metadata.get("tmux_version") == target.get("runtime_version")
            and metadata.get("archive_sha256") == archive_sha256
            and metadata.get("bin_path") == self.spec.default_bin_path
            and archive_sha256 in _RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256
            and target.get("binary_sha256")
            == _RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256.get(archive_sha256)
        )

    def resolve_binary(self) -> Path | None:
        binary = super().resolve_binary()
        if binary is not None:
            return binary
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if archive is None:
            return None
        for install_dir in self._manifest_install_candidates(manifest, archive):
            binary = self._verified_manifest_binary(install_dir, manifest, archive)
            if binary is not None:
                self._install_reason = None
                return binary
        return None

    def status(self) -> dict[str, Any]:
        manifest = self.load_manifest_for_diagnostics()
        archive = self._manifest_archive_for_platform(manifest) if manifest else None
        shared_status = super().status()
        binary = Path(shared_status["path"]) if shared_status.get("path") else None
        version = _tmux_binary_version(binary) if binary else None
        install_dir: str | None = None
        if binary is not None and manifest is not None and archive is not None:
            candidates: list[Path] = []
            if isinstance(shared_status.get("install_dir"), str):
                candidates.append(Path(shared_status["install_dir"]))
            candidates.extend(self._manifest_install_candidates(manifest, archive))
            for candidate in dict.fromkeys(candidates):
                if self._verified_manifest_binary(candidate, manifest, archive) == binary:
                    install_dir = str(candidate)
                    break
        return {
            "id": self.spec.runtime_id,
            "provider": self.spec.record_provider,
            "platform": runtime_platform_tag(),
            "installed": binary is not None,
            "version": version or (manifest.runtime_version if manifest else None),
            "status": "ready" if binary else "missing",
            "path": str(binary) if binary else None,
            "install_dir": install_dir if binary else None,
            "manifest": self._manifest_status_payload(manifest),
            "archive": self._archive_status_payload(archive),
            "reason": self._install_reason if binary is None else None,
            "download_error": self._download_error,
        }

    def _manifest_status_payload(self, manifest: ManagedRuntimeManifest | None) -> dict[str, Any] | None:
        payload = super()._manifest_status_payload(manifest)
        if payload is not None and manifest is not None:
            payload.update(
                {
                    "requires_utf8proc": bool(manifest.payload.get("requires_utf8proc")),
                    "terminfo": str(manifest.payload.get("terminfo") or "") or None,
                }
            )
        return payload

    def _prepare_binary(self, binary: Path) -> dict[str, Any]:
        return self._prepare_macos_binary(binary)

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

    def _binary_version(self, binary: Path | None) -> str | None:
        return _tmux_binary_version(binary)

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        del manifest
        return _tmux_binary_runnable(binary)


def get_tmux_runtime_manager(**kwargs: Any) -> TmuxRuntimeManager:
    return TmuxRuntimeManager(**kwargs)


def ensure_tmux_installed(force: bool = False) -> dict[str, Any]:
    result = get_tmux_runtime_manager().ensure(force=force)
    if (
        not result.get("ok")
        and not result.get("download_error")
        and result.get("message") == result.get("reason")
    ):
        reason = str(result.get("reason") or "tmux_install_failed")
        result["message"] = _tmux_failure_message(reason)
    return result


def resolve_tmux_binary() -> Path | None:
    return get_tmux_runtime_manager().resolve_binary()


def tmux_status() -> dict[str, Any]:
    return get_tmux_runtime_manager().status()


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
    return {
        "ok": False,
        "changed": False,
        "reason": "xattr_failed",
        "output": _truncate(proc.stderr or proc.stdout or ""),
    }


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


def _tmux_failure_message(reason: str) -> str:
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
