"""Keep interpreter mismatches distinct from public corpus derivation failures."""
from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest

FIXTURES = Path(__file__).parent / "fixtures/vault_signing"
RUNTIME = runpy.run_path(str(FIXTURES / "hd-corpus-generator/corpus_runtime.py"))


def test_generator_accepts_exact_recorded_interpreter():
    corpus = json.loads((FIXTURES / "hd-seed-corpus.json").read_text())
    recorded = tuple(map(int, corpus["generator"]["python"].split(".")))
    assert recorded == RUNTIME["REPRODUCIBLE_PYTHON"]
    assert RUNTIME["require_reproducible_python"](recorded) is None


@pytest.mark.parametrize("version", [(3, 10, 0), (3, 12, 3), (3, 13, 10), (3, 13, 12), (3, 14, 0)])
def test_generator_rejects_other_interpreters_with_actionable_diagnostic(version):
    with pytest.raises(SystemExit) as error:
        RUNTIME["require_reproducible_python"](version)
    found = ".".join(map(str, version))
    assert str(error.value) == (
        f"requires Python 3.13.11 (found {found}) for byte-identical corpus generation; "
        "create the pinned environment documented in REPORT.md"
    )
