"""Record where every Python process in one environment imports Avibe from.

Engine qualification drives the Model Hub E2E through a released Avibe wheel.
Installing the wheel proves nothing if the tests, or the service they start,
then import a source checkout. This module is copied into the wheel's own
environment and loaded by a ``.pth`` file, so it runs in every Python process
started from that environment: the test runner, the service, its helpers.

For each import of a product module, submodules included, it appends the
module's name and the file it was loaded from to a per-process log.
``scripts/model_hub_engine_qualification.py`` reads those logs and refuses a
run in which any product module came from outside the environment, or in
which the test runner or the service left no log.

It only observes: the spec it returns is the one the next finder found. It is
configured by a file beside it, not by the process environment, because the
E2E harness starts the service with a rebuilt environment.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PRODUCT_PACKAGES = ("vibe", "core", "config", "modules", "storage")
CONFIG_NAME = "avibe_qualification_origins.json"
MODULE_NAME = "avibe_qualification_origins"


class _Recorder:
    def __init__(self, log: Path) -> None:
        self._log = log
        self._seen: set[str] = set()

    def _write(self, record: dict) -> None:
        with self._log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def start(self) -> None:
        try:
            from importlib import metadata

            version = metadata.version("avibe-os")
        except Exception:  # noqa: BLE001 - a missing distribution is itself the finding
            version = None
        self._write(
            {
                "process": {
                    "pid": os.getpid(),
                    # The interpreter's own command line: ``sys.argv`` is not
                    # final this early, and hides the module behind ``-m``.
                    "command": list(getattr(sys, "orig_argv", [])),
                    "cwd": os.getcwd(),
                    # The environment this interpreter runs in. Its executable
                    # is only a link to a base interpreter somewhere else.
                    "prefix": sys.prefix,
                    "distribution_version": version,
                }
            }
        )

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001 - importlib finder protocol
        if name.partition(".")[0] not in PRODUCT_PACKAGES:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            if finder is self or find is None:
                continue
            spec = find(name, path, target)
            if spec is None:
                continue
            if name not in self._seen:
                self._seen.add(name)
                origin = spec.origin or sorted(str(item) for item in (spec.submodule_search_locations or ()))
                try:
                    self._write({"module": name, "origin": origin})
                except OSError:
                    pass
            return spec
        return None


def _activate() -> None:
    config = Path(__file__).with_name(CONFIG_NAME)
    try:
        directory = Path(json.loads(config.read_text(encoding="utf-8"))["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        recorder = _Recorder(directory / f"{os.getpid()}.jsonl")
        recorder.start()
    except Exception:  # noqa: BLE001 - never break the process; a missing log fails the run later
        return
    sys.meta_path.insert(0, recorder)


_activate()
