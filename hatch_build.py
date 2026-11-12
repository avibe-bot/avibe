from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from runpy import run_path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.utils import get_normalized_dependency
from packaging.requirements import Requirement

MANIFEST_SOURCE = Path("vibe/show_runtime_manifest.json")
MANIFEST_WHEEL_PATH = "vibe/show_runtime_manifest.json"
ROUTER_PATH = "vibe/show_router.tsx"
_VALIDATION = run_path(str(Path(__file__).parent / "scripts" / "show_runtime_manifest_asset.py"))
validate_manifest_bytes = _VALIDATION["validate_manifest_bytes"]
validate_manifest_file = _VALIDATION["validate_manifest_file"]
_DEPENDENCIES = run_path(str(Path(__file__).parent / "scripts" / "release_dependency_constraints.py"))
CONSTRAINTS_PATH = _DEPENDENCIES["CONSTRAINTS_PATH"]
check_constraints = _DEPENDENCIES["check_constraints"]
pinned_requirements = _DEPENDENCIES["pinned_requirements"]


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        validate_manifest_file(Path(self.root) / MANIFEST_SOURCE)
        if not (Path(self.root) / ROUTER_PATH).is_file():
            raise RuntimeError(f"Cannot build Avibe without the generated {ROUTER_PATH}")

        pins = [
            get_normalized_dependency(Requirement(pin))
            for pin in pinned_requirements(check_constraints(Path(self.root)))
        ]
        # Keep source requirements (including extras), and intersect them with
        # exact pins for the entire runtime graph. Old installers need no -c.
        existing = set(self.metadata.core.dependencies)
        build_data["dependencies"].extend(pin for pin in pins if pin not in existing)
        if self.target_name == "sdist":
            import tomlkit

            # Frontends may read static sdist metadata without running a build
            # hook. Pin the archived pyproject too, never the developer's file.
            project = tomlkit.parse((Path(self.root) / "pyproject.toml").read_text(encoding="utf-8"))
            project["project"]["dependencies"] = list(self.metadata.core.dependencies) + build_data["dependencies"]
            self._release_metadata = tempfile.TemporaryDirectory(prefix="avibe-release-metadata-")
            archived_project = Path(self._release_metadata.name) / "pyproject.toml"
            archived_project.write_text(tomlkit.dumps(project), encoding="utf-8")
            build_data["force_include"].pop(str(Path(self.root) / "pyproject.toml"), None)
            build_data["force_include"][str(archived_project)] = "pyproject.toml"

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if version == "editable":
            return
        if self.target_name == "sdist":
            self._release_metadata.cleanup()
        if self.target_name == "wheel":
            with zipfile.ZipFile(artifact_path) as wheel:
                try:
                    constraints = wheel.read(CONSTRAINTS_PATH.as_posix())
                except KeyError as exc:
                    raise RuntimeError(f"Built wheel is missing {CONSTRAINTS_PATH}") from exc
                if constraints != (Path(self.root) / CONSTRAINTS_PATH).read_bytes():
                    raise RuntimeError(f"Built wheel contains a stale {CONSTRAINTS_PATH}")
                try:
                    content = wheel.read(MANIFEST_WHEEL_PATH)
                except KeyError as exc:
                    raise RuntimeError(f"Built wheel is missing {MANIFEST_WHEEL_PATH}") from exc
                try:
                    router = wheel.read(ROUTER_PATH)
                except KeyError as exc:
                    raise RuntimeError(f"Built wheel is missing {ROUTER_PATH}") from exc
                if router != (Path(self.root) / ROUTER_PATH).read_bytes():
                    raise RuntimeError(f"Built wheel contains a stale {ROUTER_PATH}")
            try:
                validate_manifest_bytes(content)
            except ValueError as exc:
                raise RuntimeError(f"Built wheel contains an invalid {MANIFEST_WHEEL_PATH}: {exc}") from exc
