from __future__ import annotations

import ast
from pathlib import Path


MEMORY_ROOT = Path(__file__).resolve().parents[1] / "core" / "memory"


def test_memory_modules_do_not_import_python_311_datetime_utc() -> None:
    offenders: list[str] = []
    for path in MEMORY_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                if any(alias.name == "UTC" for alias in node.names):
                    offenders.append(path.name)

    assert offenders == []


def test_memory_process_has_a_python_310_tomli_fallback() -> None:
    path = MEMORY_ROOT / "process.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    fallback = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Try)
            and any(
                isinstance(stmt, ast.Import) and any(alias.name == "tomllib" for alias in stmt.names)
                for stmt in node.body
            )
        ),
        None,
    )

    assert fallback is not None
    assert any(
        isinstance(handler.type, ast.Name)
        and handler.type.id == "ModuleNotFoundError"
        and any(
            isinstance(stmt, ast.Import)
            and any(alias.name == "tomli" and alias.asname == "tomllib" for alias in stmt.names)
            for stmt in handler.body
        )
        for handler in fallback.handlers
    )
