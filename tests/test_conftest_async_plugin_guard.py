"""Regression guard for advisory handling when pytest-asyncio is absent."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import pytest

from tests.conftest import _async_items_missing_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICE = "pytest-asyncio is not installed"

# The packaged-test and publish jobs select this synchronous contract suite.
SYNC_ONLY_CI_SELECTION = "tests/test_release_verification.py"
UNITTEST_ASYNC_FILE = "tests/test_feishu_post_messages.py"
SKIPPED_ASYNC_FILE = "tests/e2e/test_codex_request_gateway.py"
NATIVE_ASYNC_FILE = "tests/test_claude_agent_sessions.py"


class _FakeItem:
    def __init__(self, obj, *, marker: str | None = None, cls=None, raises=False):
        self._obj = obj
        self._marker = marker
        self.cls = cls
        self._raises = raises

    @property
    def obj(self):
        if self._raises:
            raise RuntimeError("this collector has no test function")
        return self._obj

    def get_closest_marker(self, name: str):
        return object() if name == self._marker else None


class _AsyncCase(unittest.IsolatedAsyncioTestCase):
    pass


async def _coroutine_test():
    pass


def _sync_test():
    pass


def test_plain_coroutine_items_are_reported() -> None:
    item = _FakeItem(_coroutine_test)
    assert _async_items_missing_plugin([item]) == [item]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"cls": _AsyncCase}, "unittest brings its own event loop"),
        ({"marker": "anyio"}, "anyio is a runtime dependency"),
        ({"marker": "skip"}, "a skipped item is never awaited"),
    ],
)
def test_items_pytest_handles_without_the_plugin_are_not_reported(kwargs, why) -> None:
    assert _async_items_missing_plugin([_FakeItem(_coroutine_test, **kwargs)]) == [], why


def test_synchronous_items_are_not_reported() -> None:
    assert _async_items_missing_plugin([_FakeItem(_sync_test)]) == []


def test_items_without_a_test_function_are_skipped() -> None:
    assert _async_items_missing_plugin([_FakeItem(None, raises=True)]) == []


def _run_without_plugin(*args: str, quiet: bool = True) -> tuple[int, str]:
    argv = [sys.executable, "-m", "pytest", "-p", "no:asyncio"]
    if quiet:
        argv.append("-q")
    result = subprocess.run([*argv, *args], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


@pytest.mark.uses_real_paths
@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("--collect-only", SYNC_ONLY_CI_SELECTION), "the packaged-test/publish shape"),
        ((UNITTEST_ASYNC_FILE,), "unittest-managed async tests need no plugin"),
        ((SKIPPED_ASYNC_FILE,), "opt-in e2e items are skipped, not awaited"),
    ],
)
def test_selections_that_work_without_the_plugin_stay_untouched(args, why) -> None:
    returncode, output = _run_without_plugin(*args)
    assert returncode == 0, f"{why}: {output}"
    assert NOTICE not in output, f"{why}: {output}"


@pytest.mark.uses_real_paths
def test_a_genuine_async_selection_is_explained_but_not_aborted() -> None:
    returncode, output = _run_without_plugin(NATIVE_ASYNC_FILE)
    assert NOTICE in output, output
    assert returncode != pytest.ExitCode.USAGE_ERROR, output
    assert " passed" in output, output


def test_the_notice_survives_W_error_without_ending_the_run() -> None:
    returncode, output = _run_without_plugin(
        "-W", "error::pytest.PytestWarning", "-W", "ignore::pytest.PytestConfigWarning",
        "--collect-only", NATIVE_ASYNC_FILE,
    )
    assert NOTICE in output, output
    assert "INTERNALERROR" not in output, output
    assert returncode != pytest.ExitCode.INTERNAL_ERROR, output


def test_the_notice_still_appears_without_a_terminal_reporter() -> None:
    returncode, output = _run_without_plugin(
        "-p", "no:terminal", "--collect-only", NATIVE_ASYNC_FILE, quiet=False
    )
    assert NOTICE in output, output
    assert returncode == 0, output
