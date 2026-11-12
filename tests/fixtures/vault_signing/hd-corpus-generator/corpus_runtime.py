"""Interpreter prerequisite for byte-identical public corpus generation."""
from __future__ import annotations

REPRODUCIBLE_PYTHON = (3, 13, 11)


def require_reproducible_python(version: tuple[int, int, int]) -> None:
    # The full interpreter version is embedded in generator.python. Even another
    # 3.13 patch release changes the bytes, regardless of identical derivation.
    if version != REPRODUCIBLE_PYTHON:
        required = ".".join(map(str, REPRODUCIBLE_PYTHON))
        found = ".".join(map(str, version))
        raise SystemExit(
            f"requires Python {required} (found {found}) for byte-identical corpus generation; "
            "create the pinned environment documented in REPORT.md"
        )
