"""Published-pin-only managed sidecar. All operations are synchronous/off-loop.

Installation never runs the binary, changes the direct setting or provisions a
certificate. The lifecycle, not the installer, owns starting network resources.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import paths
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeManifest, ManagedRuntimeSpec


_TARGETS = {
    "darwin-arm64": "aarch64-apple-darwin",
    "darwin-x64": "x86_64-apple-darwin",
    "linux-arm64": "aarch64-unknown-linux-musl",
    "linux-x64": "x86_64-unknown-linux-musl",
    "win32-x64": "x86_64-pc-windows-msvc",
}
_RELEASE_ROOT = "https://github.com/avibe-bot/avibe/releases"
_SPEC = ManagedRuntimeSpec(
    runtime_id="direct-transport",
    manifest_resource="direct_transport_runtime_manifest.json",
    version_field="direct_transport_version",
    default_bin_path="avibe-direct-transport",
    platform_aliases=(("win32-arm64", "win32-x64"),),
    include_manifest_digest_in_install_fingerprint=True,
    replace_invalid_target_on_repair=True,
)


class DirectTransportRuntimeManager(ManagedRuntimeManager):
    """No custom manifest, executable path, PATH fallback or environment override."""

    def __init__(self, *, runtime_dir: Path | None = None, offline: bool = False) -> None:
        super().__init__(
            spec=_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "direct-transport",
            offline=offline,
        )

    def _parse_manifest(self, payload: bytes, *, loaded_from: str) -> ManagedRuntimeManifest | None:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("unsupported manifest")
            if data.get("release_state") == "pending":
                self._install_reason = self._reason("runtime_unpublished")
                return None
            version = data.get("direct_transport_version")
            tag = data.get("release_tag")
            if (
                data.get("release_state") != "published"
                or data.get("source") != "avibe-bot/avibe"
                or not isinstance(version, str)
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
                or not isinstance(tag, str)
                or not re.fullmatch(rf"direct-transport-v{re.escape(version)}-[1-9][0-9]*", tag)
                or data.get("source_url") != f"{_RELEASE_ROOT}/tag/{tag}"
                or not isinstance(data.get("archives"), dict)
                or set(data["archives"]) != set(_TARGETS)
            ):
                raise ValueError("unpublished or incomplete release")
            for platform, target in _TARGETS.items():
                archive = data["archives"][platform]
                name = f"avibe-direct-transport-{target}.tar.gz"
                binary = "avibe-direct-transport.exe" if platform == "win32-x64" else "avibe-direct-transport"
                if (
                    not isinstance(archive, dict)
                    or archive.get("name") != name
                    or archive.get("url") != f"{_RELEASE_ROOT}/download/{tag}/{name}"
                    or archive.get("bin_path") != binary
                    or archive.get("format") != "tar.gz"
                    or type(archive.get("size")) is not int
                    or archive["size"] <= 0
                ):
                    raise ValueError("invalid release archive")
        except (ValueError, TypeError, RecursionError):
            self._install_reason = self._reason("manifest_invalid")
            return None
        return super()._parse_manifest(payload, loaded_from=loaded_from)

    def _manifest_identity_fields(self, manifest: ManagedRuntimeManifest) -> dict[str, str]:
        return {"manifest_sha256": manifest.digest}

    def _binary_matches_manifest(self, binary: Path, manifest: ManagedRuntimeManifest) -> bool:
        # The shared installer/resolver already verifies the pinned binary hash.
        # This sidecar has no --version mode: starting it is a lifecycle action,
        # not a status probe. Do not mutate it by local signing after verification.
        return True

    def ensure(self, **kwargs: Any) -> dict[str, Any]:
        # Pending/invalid pins must not even allocate the runtime/lock directory.
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return self._failure(self._install_reason or self._reason("manifest_missing"))
        if self._manifest_archive_for_platform(manifest) is None:
            return self._failure(self._install_reason or self._reason("platform_unsupported"), manifest=manifest)
        return super().ensure(**kwargs)

    def resolve_binary(self) -> Path | None:
        # The shared core deliberately permits older admitted disk snapshots.
        # A network-facing direct transport instead requires the current pin.
        manifest = self._load_manifest(allow_network=False)
        if manifest is None:
            return None
        archive = self._manifest_archive_for_platform(manifest)
        if archive is None:
            return None
        binary = super().resolve_binary()
        if binary is None:
            return None
        # I1 archives place the binary at the archive root. Shared resolution
        # has already constrained this directory to our managed versions tree.
        if self._verified_manifest_binary(binary.parent, manifest, archive) != binary:
            self._install_reason = self._reason("install_inspection_failed")
            return None
        return binary
