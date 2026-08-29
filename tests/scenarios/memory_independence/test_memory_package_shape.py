"""MEMORY-INDEP-019 forward package-shape and retired-import evidence."""

from __future__ import annotations

import ast
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe.upgrade import (
    installed_memory_package_version,
    memory_package_installed,
)


ROOT = Path(__file__).resolve().parents[3]
SHIPPED_SOURCE_ROOTS = ("main.py", "config", "core", "modules", "storage", "vibe")
FORWARD_PACKAGE_SHAPE_IMPORTS = {
    "CORE_PACKAGE_NAME",
    "LEGACY_CORE_PACKAGE_NAME",
    "MEMORY_PACKAGE_NAME",
    "MEMORY_SPLIT_MIN_VERSION",
}


@pytest.mark.parametrize(
    ("metadata_result", "expected_present", "expected_version"),
    [
        pytest.param(SimpleNamespace(version="3.0.15"), True, "3.0.15", id="installed"),
        pytest.param(PackageNotFoundError(), False, None, id="absent"),
        pytest.param(OSError("unreadable"), True, None, id="unreadable"),
    ],
)
def test_memory_indep_019_forward_memory_package_inspection(
    monkeypatch: pytest.MonkeyPatch,
    metadata_result: object,
    expected_present: bool,
    expected_version: str | None,
) -> None:
    """Forward upgrades preserve installed Memory unless absence is certain."""

    def distribution(_name: str) -> object:
        if isinstance(metadata_result, BaseException):
            raise metadata_result
        return metadata_result

    monkeypatch.setattr("importlib.metadata.distribution", distribution)

    assert memory_package_installed() is expected_present
    assert installed_memory_package_version() == expected_version


def _production_imports() -> tuple[dict[str, set[str]], list[str]]:
    package_shape_imports: dict[str, set[str]] = {}
    reservation_imports: list[str] = []
    for root in SHIPPED_SOURCE_ROOTS:
        target = ROOT / root
        sources = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for source in sources:
            relative = source.relative_to(ROOT).as_posix()
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.endswith("package_shape"):
                        package_shape_imports.setdefault(relative, set()).update(
                            alias.name for alias in node.names
                        )
                    if module.endswith("package_lifecycle_reservation"):
                        reservation_imports.append(relative)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith("package_shape"):
                            package_shape_imports.setdefault(relative, set()).add("*")
                        if alias.name.endswith("package_lifecycle_reservation"):
                            reservation_imports.append(relative)
    return package_shape_imports, reservation_imports


def test_memory_indep_019_production_imports_only_forward_package_shape() -> None:
    package_shape_imports, reservation_imports = _production_imports()

    assert reservation_imports == []
    assert package_shape_imports == {
        "vibe/upgrade.py": FORWARD_PACKAGE_SHAPE_IMPORTS,
    }
