"""Regression guard: the missing-pytest-asyncio notice must never change a run.

The notice exists so that a suite run in an environment that never installed the
dev group reads as "one missing dependency" instead of a broad product
regression. It is advisory on purpose.

Two earlier revisions aborted collection instead, and each time the abort -- not
the diagnosis -- was the defect:

* as ``required_plugins`` in the root pyproject it was a rootdir-wide
  prerequisite enforced before collection, so it also killed the packaged-test
  matrix in lint.yml and the publish finalizer, which install only a built
  artifact plus pytest;
* as a collection-time abort it still killed legitimate selections, because
  ``unittest.IsolatedAsyncioTestCase`` methods (1087 of them here) run fine
  without the plugin, ``-k`` / ``-m`` deselection runs in the same hook, and
  already-skipped items are never awaited.

So these tests assert outcomes, not just messages: every selection that works
without the plugin must keep working, and the one selection that genuinely
fails must fail exactly as it would have anyway, with the notice added.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import pytest

from tests.conftest import _async_items_missing_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICE = "pytest-asyncio is not installed"

# One of the files the packaged-test and publish jobs select.
SYNC_ONLY_CI_SELECTION = "tests/test_memory_distribution.py"
# IsolatedAsyncioTestCase methods only -- unittest supplies the event loop.
UNITTEST_ASYNC_FILE = "tests/test_feishu_post_messages.py"
# Async tests behind the opt-in e2e_model_hub marker, skipped unless selected.
SKIPPED_ASYNC_FILE = "tests/e2e/test_codex_request_gateway.py"
# Contains one module-level `async def` test that genuinely needs the plugin.
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
    # ``quiet=False`` exists for the ``-p no:terminal`` case: ``-q`` is registered
    # by the terminal plugin, so passing both is a usage error rather than a run.
    argv = [sys.executable, "-m", "pytest", "-p", "no:asyncio"]
    if quiet:
        argv.append("-q")
    result = subprocess.run(
        [*argv, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


@pytest.mark.uses_real_paths
@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("--collect-only", SYNC_ONLY_CI_SELECTION), "the packaged-test/publish shape"),
        ((UNITTEST_ASYNC_FILE,), "unittest-managed async tests need no plugin"),
        ((SKIPPED_ASYNC_FILE,), "opt-in e2e items are skipped, not awaited"),
        (
            ("-k", "test_ambiguous_results_emit_each_answer_in_order", NATIVE_ASYNC_FILE),
            "the notice must run after -k deselection",
        ),
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
    # Not an abort: the run still executed, and failed only on the one test that
    # genuinely needs the plugin -- exactly what it would do without the notice.
    assert returncode != pytest.ExitCode.USAGE_ERROR, output
    assert " passed" in output, output


def test_the_notice_survives_W_error_without_ending_the_run() -> None:
    """``-W error`` must not turn the advisory notice into an INTERNALERROR.

    An earlier revision emitted the notice with ``warnings.warn``. A warning is
    not inert: ``-W error`` promotes it to an exception, and an exception raised
    from a collection hook aborts the whole run before any test executes -- the
    third time this guard changed an outcome it was only meant to describe.

    ``PytestConfigWarning`` is filtered back out because pytest raises its own
    "Unknown config option: asyncio_mode" warning whenever the plugin is
    missing. That one is pytest's, not this guard's, and leaving it promoted
    would mask which of the two the assertion is actually measuring. The
    ``ignore`` entry comes last because Python prepends each ``-W`` filter, so
    the last one wins.
    """
    returncode, output = _run_without_plugin(
        "-W",
        "error::pytest.PytestWarning",
        "-W",
        "ignore::pytest.PytestConfigWarning",
        "--collect-only",
        NATIVE_ASYNC_FILE,
    )
    assert NOTICE in output, output
    assert "INTERNALERROR" not in output, output
    assert returncode != pytest.ExitCode.INTERNAL_ERROR, output


def test_the_notice_still_appears_without_a_terminal_reporter() -> None:
    """``-p no:terminal`` removes the reporter the notice normally writes to.

    Dropping ``warnings.warn`` left ``reporter.write_line`` as the only channel,
    so the stderr fallback is what keeps the notice from disappearing silently
    when pytest runs without a terminal reporter.
    """
    returncode, output = _run_without_plugin(
        "-p", "no:terminal", "--collect-only", NATIVE_ASYNC_FILE, quiet=False
    )
    assert NOTICE in output, output
    assert returncode == 0, output
