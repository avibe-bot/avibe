"""Pytest plugin: record where a qualification run imported Avibe from.

Engine qualification drives the Model Hub E2E through a released Avibe wheel.
Installing that wheel proves nothing if the tests then import a source
checkout, so this plugin writes down, from inside the test process, which
files the product packages were loaded from and which release they report.
``scripts/model_hub_engine_qualification.py`` refuses the run unless every one
of them came from the wheel's own environment.

It is loaded with ``-p`` from a directory that holds nothing else, so loading
it cannot put a checkout on the import path.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

ORIGINS_ENV = "AVIBE_QUALIFICATION_ORIGINS"
PRODUCT_PACKAGES = ("vibe", "core", "config", "modules", "storage")


def pytest_sessionstart(session) -> None:  # noqa: ANN001 - pytest hook
    target = os.environ.get(ORIGINS_ENV)
    if not target:
        raise RuntimeError(f"{ORIGINS_ENV} must name where to record import origins")
    modules = {}
    for name in PRODUCT_PACKAGES:
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None) or next(iter(getattr(module, "__path__", [])), None)
        modules[name] = str(origin)
    record = {
        "modules": modules,
        "distribution_version": importlib.metadata.version("avibe-os"),
        "vibe_version": importlib.import_module("vibe").__version__,
        "prefix": sys.prefix,
    }
    Path(target).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
