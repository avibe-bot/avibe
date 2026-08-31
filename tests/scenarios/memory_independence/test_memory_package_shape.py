"""MEMORY-INDEP-024 forward package-shape and retired-import evidence."""

from __future__ import annotations

import ast
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from vibe.upgrade import memory_package_installed


ROOT = Path(__file__).resolve().parents[3]
SHIPPED_SOURCE_ROOTS = ("main.py", "config", "core", "modules", "storage", "vibe")
FORWARD_PACKAGE_SHAPE_IMPORTS = {
    "CORE_PACKAGE_NAME",
    "LEGACY_CORE_PACKAGE_NAME",
    "MEMORY_PACKAGE_NAME",
}


@pytest.mark.parametrize(
    ("metadata_result", "expected_present"),
    [
        pytest.param(object(), True, id="installed"),
        pytest.param(PackageNotFoundError(), False, id="absent"),
        pytest.param(OSError("unreadable"), True, id="unreadable"),
    ],
)
def test_memory_indep_024_forward_package_shape_and_import_contract(
    monkeypatch: pytest.MonkeyPatch,
    metadata_result: object,
    expected_present: bool,
) -> None:
    """Forward upgrades preserve installed Memory unless absence is certain."""

    def distribution(_name: str) -> object:
        if isinstance(metadata_result, BaseException):
            raise metadata_result
        return metadata_result

    monkeypatch.setattr("importlib.metadata.distribution", distribution)

    assert memory_package_installed() is expected_present

    package_shape_imports, reservation_imports = _production_imports()

    assert reservation_imports == []
    assert package_shape_imports == {
        "vibe/upgrade.py": FORWARD_PACKAGE_SHAPE_IMPORTS,
    }


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
