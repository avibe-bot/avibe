from __future__ import annotations

import os
import posixpath
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from runpy import run_path
from urllib.parse import urlsplit

from hatchling.metadata.plugin.interface import MetadataHookInterface

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
_RELEASE_VERSION = run_path(str(Path(__file__).parent / "scripts" / "release_package_version.py"))[
    "package_version_from_release_tag"
]


def description_ref(root: Path, version: str) -> str:
    """Use the workflow's exact release tag, or an exact local tag, never a guess."""
    explicit = os.environ.get("AVIBE_BUILD_TAG")
    candidates = [explicit] if explicit else []
    if not explicit and (root / ".git").exists():
        result = subprocess.run(
            ["git", "tag", "--points-at", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=10
        )
        candidates = sorted(result.stdout.splitlines())
    for tag in candidates:
        try:
            if _RELEASE_VERSION(tag) == version:
                return tag
        except ValueError:
            pass
    if explicit:
        raise ValueError(f"AVIBE_BUILD_TAG {explicit!r} does not match package version {version}")
    return "master"


def pypi_description(text: str, ref: str) -> str:
    """Make README image and file destinations portable without editing README.md."""
    def absolute(url: str, image: bool = False) -> str:
        if not url or url.startswith(("#", "//")) or urlsplit(url).scheme:
            return url
        path = posixpath.normpath(url.lstrip("/"))
        base = "https://raw.githubusercontent.com/avibe-bot/avibe" if image else "https://github.com/avibe-bot/avibe/blob"
        return f"{base}/{ref}/{path}"

    # Destination matching is independent of link-label nesting, so linked
    # badges rewrite both the inner image and the outer file destination.
    destination = r"(?P<url><[^>\n]+>|(?:\\.|[^\s()\\]|\([^()\n]*\))+)"
    inline = re.compile(r"(?P<prefix>!\[[^\]\n]*\]\(\s*|\]\(\s*)" + destination)
    html_tag = re.compile(r"<[A-Za-z][^>]*>", re.DOTALL)
    attribute = re.compile(r"(?P<prefix>\b(?P<attr>src|href)\s*=\s*)(?P<quote>['\"]?)(?P<url>[^'\"\s>]+)(?P=quote)", re.IGNORECASE)
    image_refs = {
        (match[2] or match[1]).strip().casefold()
        for match in re.finditer(r"!\[([^\]\n]+)\](?:\[([^\]\n]*)\])?(?!\()", text)
    }
    definition = re.compile(r"^(?P<prefix> {0,3}\[(?P<label>[^\]\n]+)\]:\s*)" + destination, re.MULTILINE)

    def replace_inline(match: re.Match) -> str:
        url = match["url"]
        angled = url.startswith("<")
        rewritten = absolute(url[1:-1] if angled else url, match["prefix"].startswith("!"))
        return match["prefix"] + (f"<{rewritten}>" if angled else rewritten)

    def replace_definition(match: re.Match) -> str:
        url = match["url"]
        angled = url.startswith("<")
        rewritten = absolute(url[1:-1] if angled else url, match["label"].strip().casefold() in image_refs)
        return match["prefix"] + (f"<{rewritten}>" if angled else rewritten)

    def replace_attribute(match: re.Match) -> str:
        return match["prefix"] + match["quote"] + absolute(match["url"], match["attr"].lower() == "src") + match["quote"]

    # Do not turn shell/code examples into links. Preserve code spans and
    # fenced blocks byte-for-byte while rewriting the prose around them.
    code = re.compile(r"(^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^ {0,3}(?P=fence)[ \t]*$|`+[^`\n]*`+)", re.MULTILINE | re.DOTALL)
    result = []
    previous = 0
    for match in list(code.finditer(text)) + [None]:
        end = match.start() if match else len(text)
        prose = text[previous:end]
        prose = html_tag.sub(lambda tag: attribute.sub(replace_attribute, tag[0]), prose)
        prose = inline.sub(replace_inline, prose)
        result.append(definition.sub(replace_definition, prose))
        if match:
            result.append(match[0])
            previous = match.end()
    return "".join(result)


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        # An sdist archives the already-resolved description as static metadata.
        # Rebuilding it without Git or release environment must keep that ref.
        if "readme" in metadata:
            return
        root = Path(self.root)
        metadata["readme"] = {
            "text": pypi_description((root / "README.md").read_text(encoding="utf-8"), description_ref(root, metadata["version"])),
            "content-type": "text/markdown",
        }


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
            # Keep README.md unchanged in the sdist, but freeze its portable
            # description for metadata-only frontends and wheel rebuilds.
            project["project"]["dynamic"].remove("readme")
            project["project"]["readme"] = {
                "text": self.metadata.core.readme,
                "content-type": "text/markdown",
            }
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
