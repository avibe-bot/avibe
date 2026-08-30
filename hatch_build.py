from __future__ import annotations

import zipfile
from pathlib import Path
from runpy import run_path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

MANIFEST_SOURCE = Path("vibe/show_runtime_manifest.json")
MANIFEST_WHEEL_PATH = "vibe/show_runtime_manifest.json"
_VALIDATION = run_path(str(Path(__file__).parent / "scripts" / "show_runtime_manifest_asset.py"))
validate_manifest_bytes = _VALIDATION["validate_manifest_bytes"]
validate_manifest_file = _VALIDATION["validate_manifest_file"]
pin_peer_dependency = run_path(str(Path(__file__).parent / "hatch_exact_peer.py"))["pin_peer_dependency"]


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        validate_manifest_file(Path(self.root) / MANIFEST_SOURCE)

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if version == "editable":
            return
        if self.target_name == "wheel":
            with zipfile.ZipFile(artifact_path) as wheel:
                try:
                    content = wheel.read(MANIFEST_WHEEL_PATH)
                except KeyError as exc:
                    raise RuntimeError(f"Built wheel is missing {MANIFEST_WHEEL_PATH}") from exc
            try:
                validate_manifest_bytes(content)
            except ValueError as exc:
                raise RuntimeError(f"Built wheel contains an invalid {MANIFEST_WHEEL_PATH}: {exc}") from exc
        pin_peer_dependency(
            artifact_path,
            project_name="avibe-os",
            peer_name="avibe-memory",
            package_version=self.metadata.version,
            peer_extra="memory",
        )
