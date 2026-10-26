"""What pytest itself collects for a set of arguments, without running anything.

Two scenario-catalog gates ask pytest rather than reading source: the citation
check (``tests/scenarios/test_catalog_citations.py``) asks which cases a cited
file holds, and the evidence gate (``scripts/ci_scenario_evidence.py``) asks
which cases a workflow's pytest command would run. Both get pytest's own answer,
selection and deselection included, from one collection-only run in a child
process with the conftest files that apply where it runs: a nested
``pytest.main`` would share the caller's modules and plugins.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# One collection of every file the catalogs cite takes about a minute in CI; the bound only stops a hang.
COLLECTION_TIMEOUT_SECONDS = 300


class _Recorder:
    """A pytest plugin that writes what a collection-only run collected, as JSON."""

    def __init__(self, out: str) -> None:
        self.out = out

    def pytest_collection_finish(self, session) -> None:
        cases = []
        for item in session.items:
            function = getattr(item, "function", None)
            cases.append(
                {
                    "nodeid": item.nodeid,
                    "doc": (function.__doc__ if function is not None else None) or "",
                    "skipped": _declares_skip(item),
                }
            )
        Path(self.out).write_text(json.dumps(cases), encoding="utf-8")


def _declares_skip(item) -> bool:
    """Whether the case, its class, its module or its parameter set declares a skip.

    Only a declared ``skip``: one a hook adds while collecting, such as the
    e2e suites' opt-in, depends on the run's options as ``skipif`` depends on
    the environment.
    """

    function, owner, module = (getattr(item, name, None) for name in ("function", "cls", "module"))
    marks = list(getattr(getattr(item, "callspec", None), "marks", None) or [])
    for target in (function, owner, module):
        declared = getattr(target, "pytestmark", None) or []
        marks.extend(declared if isinstance(declared, (list, tuple)) else [declared])
    return any(getattr(mark, "name", None) == "skip" for mark in marks) or any(
        getattr(target, "__unittest_skip__", False) for target in (function, owner)
    )


def collect(args: list[str], cwd: Path = REPO_ROOT) -> list[dict]:
    """Each case ``pytest --collect-only <args>`` collects from ``cwd``: its node id, docstring and declared skip."""

    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "collected.json"
        result = subprocess.run(
            [sys.executable, "-c", "from scripts.pytest_collection import _main\n_main()", str(out), *args],
            cwd=cwd,
            # Without the caller's PYTEST_ADDOPTS, whose selection would drop cases,
            # or a CI junit directory this run has nothing to report to.
            env={
                **{name: value for name, value in os.environ.items() if name not in ("PYTEST_ADDOPTS", "AVIBE_CI_JUNIT_DIR")},
                "PYTHONPATH": os.pathsep.join(filter(None, [str(cwd), str(REPO_ROOT), os.environ.get("PYTHONPATH")])),
            },
            capture_output=True,
            text=True,
            timeout=COLLECTION_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"pytest could not collect {' '.join(args)} (exit {result.returncode}):\n"
                f"{result.stdout[-4000:]}{result.stderr[-4000:]}"
            )
        return json.loads(out.read_text(encoding="utf-8"))


def _main() -> None:
    import pytest

    out, *args = sys.argv[1:]
    sys.exit(int(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args], plugins=[_Recorder(out)])))
