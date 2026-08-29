"""MEMORY-INDEP-024 forward package-shape and retired-import evidence."""

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
def test_memory_indep_024_forward_memory_package_inspection(
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


def _imported_modules(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}

    module = node.module or ""
    imported = {module} if module else set()
    imported.update(
        f"{module}.{alias.name}".strip(".")
        for alias in node.names
        if alias.name != "*"
    )
    return imported


def _package_shape_imported_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
        "package_shape"
    ):
        return {alias.name for alias in node.names}
    if any(module.endswith("package_shape") for module in _imported_modules(node)):
        return {"*"}
    return set()


def _imports_retired_reservation(node: ast.Import | ast.ImportFrom) -> bool:
    return any(
        module.endswith("package_lifecycle_reservation")
        for module in _imported_modules(node)
    )


@pytest.mark.parametrize(
    ("source", "expected_package_shape", "expected_reservation"),
    [
        pytest.param(
            "from vibe.package_shape import CORE_PACKAGE_NAME",
            {"CORE_PACKAGE_NAME"},
            False,
            id="package-shape-symbol",
        ),
        pytest.param(
            "from vibe import package_shape",
            {"*"},
            False,
            id="package-shape-parent",
        ),
        pytest.param(
            "from . import package_shape",
            {"*"},
            False,
            id="package-shape-relative-parent",
        ),
        pytest.param(
            "import vibe.package_lifecycle_reservation",
            set(),
            True,
            id="reservation-module",
        ),
        pytest.param(
            "from vibe.package_lifecycle_reservation import PackageLifecycleReservation",
            set(),
            True,
            id="reservation-symbol",
        ),
        pytest.param(
            "from vibe import package_lifecycle_reservation",
            set(),
            True,
            id="reservation-parent",
        ),
        pytest.param(
            "from . import package_lifecycle_reservation",
            set(),
            True,
            id="reservation-relative-parent",
        ),
    ],
)
def test_memory_indep_024_import_syntax_inventory(
    source: str,
    expected_package_shape: set[str],
    expected_reservation: bool,
) -> None:
    node = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )

    assert _package_shape_imported_names(node) == expected_package_shape
    assert _imports_retired_reservation(node) is expected_reservation


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
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                imported_names = _package_shape_imported_names(node)
                if imported_names:
                    package_shape_imports.setdefault(relative, set()).update(
                        imported_names
                    )
                if _imports_retired_reservation(node):
                    reservation_imports.append(relative)
    return package_shape_imports, reservation_imports


def test_memory_indep_024_production_imports_only_forward_package_shape() -> None:
    package_shape_imports, reservation_imports = _production_imports()

    assert reservation_imports == []
    assert package_shape_imports == {
        "vibe/upgrade.py": FORWARD_PACKAGE_SHAPE_IMPORTS,
    }
