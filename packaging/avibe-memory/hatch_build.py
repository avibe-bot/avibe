from __future__ import annotations

from pathlib import Path
from runpy import run_path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_ROOT = Path(__file__).resolve().parent
_HELPER = _ROOT / "hatch_exact_peer.py"
if not _HELPER.is_file():
    _HELPER = _ROOT.parents[1] / "hatch_exact_peer.py"
pin_peer_dependency = run_path(str(_HELPER))["pin_peer_dependency"]


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        repository_root = root.parents[1]
        source_root = root if (root / "avibe_memory").is_dir() else repository_root
        sources = {
            source_root / "vibe" / "memory_runtime_manifest.json": (
                "vibe/memory_runtime_manifest.json"
            ),
        }
        if source_root != root:
            sources[source_root / "avibe_memory"] = "avibe_memory"
        if self.target_name == "sdist":
            sources[_HELPER] = "hatch_exact_peer.py"
        missing = [str(path) for path in sources if not path.exists()]
        if missing:
            raise RuntimeError(
                "avibe-memory build sources are missing: " + ", ".join(missing)
            )
        force_include = build_data.setdefault("force_include", {})
        force_include.update({str(path): target for path, target in sources.items()})

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if version == "editable":
            return
        pin_peer_dependency(
            artifact_path,
            project_name="avibe-memory",
            peer_name="avibe-os",
            package_version=self.metadata.version,
        )
