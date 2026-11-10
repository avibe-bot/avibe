"""Record where every Python process in one environment imports Avibe from.

Engine qualification drives the Model Hub E2E through a released Avibe wheel.
Installing the wheel proves nothing if the tests, or the service they start,
then import a source checkout. This module is copied into the wheel's own
environment and loaded by a ``.pth`` file, so it runs in every Python process
started from that environment: the test runner, the service, its helpers.

Each process writes its own log. The first record says how the process was
started. After it, each product module the import system resolves, submodules
included, gets one record per file it was found in.
``scripts/model_hub_engine_qualification.py`` reads those logs and refuses a
run in which any product module came from outside the environment, or in
which the test runner or the service left no log.

It observes, and these rules keep it from changing what it observes:

- It is one finder on ``sys.meta_path``. For a product module it asks the
  finders after it, in order, and returns the first spec they give, unchanged.
  An error one of them raises passes through. It never asks a finder ahead of
  it: the import system, or that finder, has already done so.
- Other finders delegate as well. The test suite's own (``tests/conftest.py``)
  asks every finder but itself, this one included. When a finder this one is
  asking asks back for the same module on the same thread, this one declines,
  and that finder goes on to the rest of the path by itself. Each finder then
  handles the import once. Without this the two call each other until the
  stack runs out.
- It loads nothing into the process but ``json``. The installed version is
  read from the distribution's metadata file, because the standard metadata
  library would first import some fifty modules into every process.
- Recording never raises into an import. A record that cannot be written is
  kept and written with the next one.
- It is configured by a file beside it, not by the process environment,
  because the E2E harness starts the service with a rebuilt environment.

What it does not see: a module that a finder ahead of it answers without
asking the rest of the path, a module loaded straight from a file without the
import system, a process started with ``-S``, and a Python that is not this
environment's. A forked child writes to its parent's log.
"""

# No ``from __future__ import``: this file loads nothing its job does not need.
import json
import os
import sys
from _thread import get_ident

PRODUCT_PACKAGES = ("vibe", "core", "config", "modules", "storage")
CONFIG_NAME = "avibe_qualification_origins.json"
MODULE_NAME = "avibe_qualification_origins"
_DISTRIBUTION = "avibe_os"
_METADATA_FILES = {".dist-info": "METADATA", ".egg-info": "PKG-INFO"}


def _distribution_version() -> str | None:
    """The version of the first Avibe distribution on ``sys.path``; ``None`` if it has none or it cannot be read."""

    for entry in sys.path:
        directory = entry or os.curdir
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            stem, suffix = os.path.splitext(name)
            if suffix not in _METADATA_FILES or stem.lower().partition("-")[0] != _DISTRIBUTION:
                continue
            try:
                with open(os.path.join(directory, name, _METADATA_FILES[suffix]), encoding="utf-8") as stream:
                    for line in stream:
                        if line.startswith("Version:"):
                            return line.partition(":")[2].strip()
                        if not line.strip():
                            break
            except (OSError, ValueError):
                pass
            return None
    return None


class _Recorder:
    def __init__(self, log: str) -> None:
        self._log = log
        self._recorded: set[str] = set()
        self._unwritten: list[str] = []
        #: (thread, module) pairs this finder is asking the finders after it about.
        self._asking: set[tuple[int, str]] = set()

    def _append(self, lines: list[str]) -> None:
        with open(self._log, "a", encoding="utf-8") as stream:
            stream.write("".join(lines))

    def start(self) -> None:
        """Write the process record, or raise: a process that cannot log is not recorded at all."""

        self._append(
            [
                json.dumps(
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
                            "distribution_version": _distribution_version(),
                        }
                    },
                    sort_keys=True,
                )
                + "\n"
            ]
        )

    def _record(self, name: str, spec) -> None:  # noqa: ANN001 - whatever a finder returned
        try:
            # A spec with neither a file nor search locations is recorded as coming from nowhere.
            found = getattr(spec, "origin", None)
            origin = str(found) if found else sorted(
                str(item) for item in (getattr(spec, "submodule_search_locations", None) or ())
            )
            line = json.dumps({"module": name, "origin": origin}, sort_keys=True) + "\n"
            # The same module found in another file later, after a test changed
            # the path, is another record.
            if line in self._recorded:
                return
            self._recorded.add(line)
            lines = [line]
            try:
                while True:
                    lines.append(self._unwritten.pop())
            except IndexError:
                pass
            try:
                self._append(lines)
            except OSError:
                self._unwritten.extend(lines)
        except Exception:  # noqa: BLE001 - recording must never fail an import
            pass

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001 - importlib finder protocol
        if name.partition(".")[0] not in PRODUCT_PACKAGES:
            return None
        asking = (get_ident(), name)
        if asking in self._asking:
            return None
        self._asking.add(asking)
        try:
            spec, after = None, False
            for finder in tuple(sys.meta_path):
                if finder is self:
                    after = True
                    continue
                find = getattr(finder, "find_spec", None) if after else None
                if find is None:
                    continue
                spec = find(name, path, target)
                if spec is not None:
                    break
        finally:
            self._asking.discard(asking)
        if spec is not None:
            self._record(name, spec)
        return spec


def _activate() -> None:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME), encoding="utf-8") as stream:
            directory = json.load(stream)["directory"]
        os.makedirs(directory, exist_ok=True)
        # One log per process, not per process id: an id can be reused, or kept across an exec.
        recorder = _Recorder(os.path.join(directory, f"{os.getpid()}-{os.urandom(4).hex()}.jsonl"))
        recorder.start()
    except Exception:  # noqa: BLE001 - never break the process; a missing log fails the run later
        return
    sys.meta_path.insert(0, recorder)


_activate()
