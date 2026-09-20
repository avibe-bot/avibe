"""Pytest plugin proving core scenarios do not touch the Memory implementation."""

from __future__ import annotations

import importlib.abc
import os
import sys

import pytest


class _AvibeMemoryImportBlocker(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.attempted: list[str] = []

    def find_spec(self, fullname, path, target=None):
        del path, target
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
            self.attempted.append(fullname)
            raise ModuleNotFoundError(
                f"blocked optional Memory implementation: {fullname}"
            )
        return None


_BLOCKER = _AvibeMemoryImportBlocker()


def pytest_configure(config: pytest.Config) -> None:
    del config
    os.environ["AVIBE_TEST_BLOCK_MEMORY_IMPORTS"] = "1"
    loaded = [
        name
        for name in sys.modules
        if name == "avibe_memory" or name.startswith("avibe_memory.")
    ]
    if loaded:
        raise pytest.UsageError(
            f"avibe_memory loaded before import tracking started: {loaded}"
        )
    sys.meta_path.insert(0, _BLOCKER)


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int,
) -> None:
    del exitstatus
    loaded = sorted(
        name
        for name in sys.modules
        if name == "avibe_memory" or name.startswith("avibe_memory.")
    )
    attempted = sorted(set(_BLOCKER.attempted))
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "MEMORY-INDEP-017 import tracking: "
            f"attempted={attempted} loaded={loaded}"
        )
    if attempted or loaded:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
