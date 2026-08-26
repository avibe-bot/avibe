from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


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
        missing = [str(path) for path in sources if not path.exists()]
        if missing:
            raise RuntimeError(
                "avibe-memory build sources are missing: " + ", ".join(missing)
            )
        force_include = build_data.setdefault("force_include", {})
        force_include.update({str(path): target for path, target in sources.items()})
