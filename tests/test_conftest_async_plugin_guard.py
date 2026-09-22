"""Regression guard: the missing-pytest-asyncio check must stay scoped to the
runs that actually select ``async def`` tests.

The first version of this guard was a ``required_plugins`` entry in the root
``pyproject.toml``. That key is a rootdir-wide prerequisite enforced *before*
collection, so it also aborted the two CI jobs that deliberately install only a
built artifact plus ``pytest`` and select synchronous tests only: the
packaged-test matrix in ``.github/workflows/lint.yml`` and the publish
finalizer in ``.github/workflows/publish.yml`` -- the repository's sole release
finalizer. Both died with "Missing required plugins: pytest-asyncio" before
running a single test.

These tests pin both directions: a synchronous selection with no async plugin
still collects, and an ``async def`` selection with no async plugin fails once,
naming the missing dependency rather than every test that would have failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _async_items_missing_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]

# One of the files the packaged-test and publish jobs select. Kept in sync with
# the workflows on purpose: if it ever grows an async test, those jobs need the
# plugin and this guard should not be what tells us.
SYNC_ONLY_CI_SELECTION = "tests/test_memory_distribution.py"


class _FakeItem:
    def __init__(self, obj, *, anyio: bool = False, raises: bool = False):
        self._obj = obj
        self._anyio = anyio
        self._raises = raises

    @property
    def obj(self):
        if self._raises:
            raise RuntimeError("this collector has no test function")
        return self._obj

    def get_closest_marker(self, name: str):
        return object() if (name == "anyio" and self._anyio) else None


async def _coroutine_test():
    pass


def _sync_test():
    pass


def test_plain_coroutine_items_are_reported() -> None:
    item = _FakeItem(_coroutine_test)
    assert _async_items_missing_plugin([item]) == [item]


def test_anyio_marked_coroutines_are_not_reported() -> None:
    # anyio is a runtime dependency, so its plugin drives these wherever the
    # package installs -- flagging them would fire in environments that are fine.
    assert _async_items_missing_plugin([_FakeItem(_coroutine_test, anyio=True)]) == []


def test_synchronous_items_are_not_reported() -> None:
    assert _async_items_missing_plugin([_FakeItem(_sync_test)]) == []


def test_items_without_a_test_function_are_skipped() -> None:
    # Doctest and custom collectors raise on ``.obj``; the guard must not crash
    # the whole collection over one of them.
    assert _async_items_missing_plugin([_FakeItem(None, raises=True)]) == []


def _collect(selection: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:asyncio",
            "--collect-only",
            "-q",
            selection,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.uses_real_paths
def test_sync_only_ci_selection_still_collects_without_the_async_plugin() -> None:
    """The exact shape of the packaged-test and publish jobs must stay green."""
    result = _collect(SYNC_ONLY_CI_SELECTION)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "pytest-asyncio" not in output


@pytest.mark.uses_real_paths
def test_async_selection_fails_once_naming_the_missing_plugin() -> None:
    result = _collect("tests/test_claude_agent_sessions.py")
    # pytest writes a UsageError raised from a hook to stderr, not stdout.
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "pytest-asyncio is not installed" in output
