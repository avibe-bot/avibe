"""Generate and verify the universal, runtime-only release dependency artifact.

Hashes are retained as provenance. In particular, uv tool install does not
check hashes supplied in a constraints file; release metadata pins versions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CONSTRAINTS_PATH = Path("vibe/dependency_constraints.txt")


def _locked_requirement(item: dict) -> str:
    """Convert uv's stored requirement metadata without losing extras/markers."""
    if set(item) - {"name", "extras", "specifier", "marker"}:
        raise RuntimeError(f"Unsupported non-registry release requirement: {item['name']}")
    extras = f"[{','.join(item['extras'])}]" if item.get("extras") else ""
    marker = f"; {item['marker']}" if item.get("marker") else ""
    return f"{item['name']}{extras}{item.get('specifier', '')}{marker}"


def _check_project_metadata(root: Path, constraints: str) -> None:
    """Check source requirements, allowing only the supplemental sdist pins.

    uv canonicalizes marker logic (including distributing AND over OR and
    rewriting python_version). Let uv compare semantics rather than attempting
    to implement a second marker algebra here. Its check runs on an isolated
    minimal project, never rewrites the source, and cannot access the network.
    """
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.utils import canonicalize_name

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    error = "pyproject.toml runtime requirements do not match uv.lock; run uv lock and regenerate release constraints"
    locked_project = next(
        (item for item in lock["package"]
         if canonicalize_name(item["name"]) == canonicalize_name(project["name"])
         and item.get("source") == {"editable": "."}),
        None,
    )
    if locked_project is None or SpecifierSet(project["requires-python"]) != SpecifierSet(lock["requires-python"]):
        raise RuntimeError(error)
    metadata = locked_project["metadata"]

    def key(requirement: Requirement) -> tuple:
        return canonicalize_name(requirement.name), frozenset(requirement.extras), requirement.specifier, requirement.url

    direct_keys = {key(Requirement(_locked_requirement(item))) for item in metadata["requires-dist"]}
    pins = {Requirement(item) for item in pinned_requirements(constraints)}
    dependencies = []
    for raw in project["dependencies"]:
        requirement = Requirement(raw)
        if key(requirement) in direct_keys:
            dependencies.append(raw)
        elif requirement not in pins:
            raise RuntimeError(error)

    # Only runtime declarations are under test. Preserve the lock's own dev
    # declarations, so an unrelated dev-only edit cannot alter this check.
    document = (
        f"[project]\nname = {json.dumps(project['name'])}\n"
        f"requires-python = {json.dumps(project['requires-python'])}\n"
        f"dependencies = {json.dumps(dependencies)}\n"
        + (f"version = {json.dumps(locked_project['version'])}\n" if "version" in locked_project
           else 'dynamic = ["version"]\n')
        + "[tool.uv]\npackage = true\n[dependency-groups]\n"
    )
    for name, requirements in metadata.get("requires-dev", {}).items():
        document += f"{json.dumps(name)} = {json.dumps([_locked_requirement(item) for item in requirements])}\n"
    with tempfile.TemporaryDirectory(prefix="avibe-release-lock-check-") as directory:
        isolated = Path(directory)
        (isolated / "pyproject.toml").write_text(document, encoding="utf-8")
        (isolated / "uv.lock").write_bytes((root / "uv.lock").read_bytes())
        result = subprocess.run(
            ["uv", "lock", "--check", "--offline", "--no-python-downloads", "--python", sys.executable],
            cwd=isolated,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"{error}\n{result.stderr.strip()}")


def export_constraints(root: Path) -> str:
    """Export the locked runtime graph, without resolving or filtering to this host."""
    # Git may check text files out as CRLF on Windows; line endings are not a
    # dependency change and must not make the same release fail to rebuild.
    lock_digest = hashlib.sha256((root / "uv.lock").read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    result = subprocess.run(
        [
            "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
            "--no-header", "--no-annotate", "--offline", "--no-python-downloads",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    _check_project_metadata(root, result.stdout)
    return (
        "# Generated by scripts/release_dependency_constraints.py; do not edit.\n"
        f"# uv.lock SHA256 (LF): {lock_digest}\n"
        + result.stdout
    )


def check_constraints(root: Path) -> str:
    """Fail closed when a release artifact is absent or differs from its lock."""
    expected = export_constraints(root)
    path = root / CONSTRAINTS_PATH
    if not path.is_file() or path.read_text(encoding="utf-8") != expected:
        raise RuntimeError(
            f"Missing or stale {CONSTRAINTS_PATH}; run "
            "python scripts/release_dependency_constraints.py before building a release"
        )
    return expected


def pinned_requirements(constraints: str) -> list[str]:
    """Read uv's PEP 508 requirement lines, retaining every universal marker fork."""
    from packaging.requirements import Requirement

    requirements = []
    for line in constraints.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        line = line.removesuffix("\\").rstrip()
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if requirement.url or len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise ValueError(f"Release dependency is not an exact registry pin: {line}")
        requirements.append(str(requirement))
    if not requirements:
        raise ValueError("Release dependency constraints are empty")
    return requirements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the generated release constraints are stale")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.check:
        check_constraints(root)
    else:
        content = export_constraints(root)
        pinned_requirements(content)
        (root / CONSTRAINTS_PATH).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
