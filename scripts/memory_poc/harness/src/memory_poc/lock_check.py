from __future__ import annotations

import importlib.metadata as metadata
import re
import site
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.markers import Marker, default_environment


class LockVerificationError(RuntimeError):
    pass


def active_lock_packages(lock_path: Path) -> dict[str, str]:
    """Resolve the current platform's exact `uv.lock` closure, including dev."""
    try:
        payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        raw_packages = payload["package"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise LockVerificationError("lock_invalid") from exc
    if not isinstance(raw_packages, list):
        raise LockVerificationError("lock_invalid")

    packages: dict[str, dict[str, Any]] = {}
    for package in raw_packages:
        if not isinstance(package, dict):
            raise LockVerificationError("lock_invalid")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise LockVerificationError("lock_invalid")
        normalized = _normalize(name)
        if normalized in packages:
            raise LockVerificationError("lock_duplicate_package")
        packages[normalized] = package

    environment = default_environment()
    expected: dict[str, str] = {}
    visited: set[tuple[str, tuple[str, ...]]] = set()

    def include_entry(entry: Any, extras: frozenset[str]) -> None:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise LockVerificationError("lock_dependency_invalid")
        if not _marker_applies(entry.get("marker"), extras, environment):
            return
        entry_extras = entry.get("extra", [])
        if not isinstance(entry_extras, list) or not all(isinstance(value, str) for value in entry_extras):
            raise LockVerificationError("lock_dependency_invalid")
        include(_normalize(entry["name"]), frozenset(entry_extras))

    def include(name: str, extras: frozenset[str] = frozenset()) -> None:
        package = packages.get(name)
        if package is None:
            raise LockVerificationError("lock_dependency_missing")
        expected[name] = package["version"]
        state = (name, tuple(sorted(extras)))
        if state in visited:
            return
        visited.add(state)
        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise LockVerificationError("lock_dependency_invalid")
        for dependency in dependencies:
            include_entry(dependency, extras)
        optional_dependencies = package.get("optional-dependencies", {})
        if not isinstance(optional_dependencies, dict):
            raise LockVerificationError("lock_dependency_invalid")
        for extra in extras:
            optional = optional_dependencies.get(extra)
            if optional is None:
                raise LockVerificationError("lock_extra_missing")
            if not isinstance(optional, list):
                raise LockVerificationError("lock_dependency_invalid")
            for dependency in optional:
                include_entry(dependency, frozenset({extra}))

    root_name = "memory-poc-harness"
    include(root_name)
    root = packages[root_name]
    dev_dependencies = root.get("dev-dependencies", {})
    if not isinstance(dev_dependencies, dict):
        raise LockVerificationError("lock_dependency_invalid")
    dev_group = dev_dependencies.get("dev", [])
    if not isinstance(dev_group, list):
        raise LockVerificationError("lock_dependency_invalid")
    for dependency in dev_group:
        include_entry(dependency, frozenset())
    return expected


def verify_exact_environment(lock_path: Path) -> None:
    if sys.version_info[:2] != (3, 12) or site.ENABLE_USER_SITE:
        raise LockVerificationError("python_environment_invalid")
    expected = active_lock_packages(lock_path)
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if not isinstance(name, str):
            raise LockVerificationError("installed_distribution_invalid")
        normalized = _normalize(name)
        if normalized in installed:
            raise LockVerificationError("installed_distribution_duplicate")
        installed[normalized] = distribution.version
    if installed != expected:
        raise LockVerificationError("locked_environment_package_set_mismatch")


def _marker_applies(marker: Any, extras: frozenset[str], environment: dict[str, str]) -> bool:
    if marker is None:
        return True
    if not isinstance(marker, str):
        raise LockVerificationError("lock_dependency_invalid")
    candidates = extras or frozenset({""})
    return any(Marker(marker).evaluate({**environment, "extra": extra}) for extra in candidates)


def _normalize(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return 1
    try:
        verify_exact_environment(Path(args[0]))
    except LockVerificationError:
        return 1
    print(sys.prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
