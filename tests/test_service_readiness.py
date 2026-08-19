"""Who is entitled to say the service is up.

`vibe status`, the doctor and the restart supervisor all decide whether a release
started by asking the runtime whether the service instance is ready. Publishing
that answer is therefore a claim to have observed the service serving, and the
incident behind this module is what happens when the claim is made by code that
observed no such thing: a release that died inside its own startup was read as an
upgrade that worked, for days, because readiness had been announced by the caller
before the startup it was speaking for was attempted.

The ordering half that `main()` still owns -- lock, then migration, then the
controller -- is asserted in `tests/test_sqlite_state_startup.py`.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import controller as controller_module
from core import internal_server
from core.controller import Controller

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Everything shipped to a user's machine. `tests/` is excluded deliberately:
#: `tests/test_runtime_service_lock.py` calls the primitive to test the primitive,
#: which is not a claim about any running service.
SHIPPED_SOURCE_ROOTS = ("main.py", "config", "core", "modules", "storage", "vibe")


@pytest.fixture
def startup(monkeypatch):
    """A controller stripped to the startup steps `run()` performs itself.

    Nothing in `asyncio` or `threading` is stubbed: `core/controller.py` reaches
    them through the module objects themselves, so replacing an attribute there
    replaces it for the whole process, and a test that stops the loop by owning
    `new_event_loop` would be asserting against a loop of its own rather than
    the one the service runs on. The seams used instead are the two the service
    genuinely has -- the readiness primitive and the dispatch server -- and the
    real loop is stopped from the readiness callback, which is exactly the moment
    under test.
    """

    events: list[str] = []
    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace()
    controller.enabled_platforms = ["slack"]
    controller.show_git_checkpoint_service = SimpleNamespace(start=lambda: events.append("show checkpoints"))
    controller._shutdown_requested = False
    controller._im_run_exception = None
    # Real thread, doing nothing and exiting: the IM runtime is genuinely
    # concurrent with everything after it, so it is left out of `events` rather
    # than given an order it does not have.
    controller._run_im_runtime = Mock()
    controller.cleanup_sync = Mock()

    def publish_readiness():
        events.append("published ready")
        # Recorded from inside the loop rather than after `run_forever()`
        # returns, so "the service is serving" is observed at the instant it
        # becomes true -- which is the thing readiness claims.
        controller._loop.call_soon(lambda: (events.append("serving"), controller._loop.stop()))

    monkeypatch.setattr(controller_module, "mark_service_instance_started", publish_readiness)
    monkeypatch.setattr(internal_server, "start", lambda controller: events.append("internal server"))
    # `run()`'s cleanup unlinks whatever this returns. Left real, it would name
    # -- and on a developer machine delete -- the live local service's socket.
    monkeypatch.setattr(internal_server, "default_socket_path", lambda: REPO_ROOT / "no-such-vibe.sock")

    yield SimpleNamespace(controller=controller, events=events)

    # `run()` installs its loop as the thread's current one and then closes it.
    # Left behind, the next test in this process inherits a closed loop.
    asyncio.set_event_loop(None)


def test_readiness_is_published_by_the_code_that_reaches_the_serving_loop(startup):
    """Announced last, from inside the function that does the starting.

    Not a claim about which line the call sits on, but about what stands behind
    it: every structural step a new release can die in has already succeeded, and
    the only thing left is the loop that is the service serving.
    """

    Controller.run(startup.controller)

    assert startup.events[-2:] == ["published ready", "serving"]
    # And there was a startup for it to be speaking for, rather than the publish
    # being last in an otherwise empty sequence.
    assert startup.events[:-2]


def test_a_release_that_dies_in_its_own_startup_never_publishes_readiness(startup):
    """The incident, reproduced at a step that only the new release can fail.

    A release whose startup breaks structurally does not crash the process:
    `run()` catches, cleans up, and returns, so the service exits through the
    ordinary path. Everything watching therefore has only the published readiness
    to go on -- and nothing must have published it.
    """

    def start_that_the_new_release_cannot_do():
        startup.events.append("show checkpoints")
        raise RuntimeError("checkpoint service is not constructible on this release")

    startup.controller.show_git_checkpoint_service = SimpleNamespace(start=start_that_the_new_release_cannot_do)

    # Returns instead of raising. That is the shape of the bug, not a detail of
    # the test: a non-zero exit would have been noticed years ago.
    Controller.run(startup.controller)

    assert "published ready" not in startup.events
    assert "serving" not in startup.events
    startup.controller.cleanup_sync.assert_called_once()


def _readiness_calls_in(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        # Both spellings: the bare name that `core/controller.py` imports, and
        # the `runtime.` attribute form a new caller is at least as likely to use.
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "mark_service_instance_started"
    )


def test_nothing_else_shipped_publishes_readiness():
    """One announcer, found by looking rather than by remembering.

    The defect was a second announcer: `main()` published readiness and then
    called `run()`, so the gap between the two was a service reported up that had
    not started. Counting over the whole shipped tree closes the class instead of
    the instance -- a third announcer in a platform adapter, a CLI path or a
    future supervisor fails here when it is written, rather than in someone's
    dark instance days later.
    """

    announcers = {}
    for root in SHIPPED_SOURCE_ROOTS:
        target = REPO_ROOT / root
        for source in [target] if target.is_file() else sorted(target.rglob("*.py")):
            calls = _readiness_calls_in(source)
            if calls:
                announcers[source.relative_to(REPO_ROOT).as_posix()] = calls

    assert announcers == {"core/controller.py": 1}
