"""Every queue that can discard an event must be able to admit it.

The browser suppresses its reconnect catch-up when the event stream can prove it
lost nothing (see ``ui/src/lib/workbenchEventConnection.ts``). That proof is only
as good as the path's willingness to report a hole, and the path is a chain of
bounded fan-out queues: a full one discards, the publisher's exception has
nowhere to report to, and the subscriber's socket stays open and healthy. Loss
looks exactly like quiet.

Enumerating those queues by hand does not work -- it was asserted complete twice
and was wrong twice, once per review round. So this states the property instead
of the list: wherever a queue drops an event, that drop is counted, and the
module owning it exposes the count. A new bounded fan-out queue added anywhere
under ``core/`` or ``vibe/`` fails this test rather than silently becoming the
next hole in a proof that claims to cover the whole path.

What the count is *for* belongs to each feed's own tests: ``tests/test_sse_broker.py``
and ``tests/test_inbox_events.py`` cover the counters, and
``tests/test_ui_server_fastapi.py`` / ``tests/test_internal_server.py`` cover the
readers ending a subscription whose view has a hole in it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = ("core", "vibe")


def _is_queue_full(node: ast.expr | None) -> bool:
    """Match ``asyncio.QueueFull`` and a bare/aliased ``QueueFull``."""

    if isinstance(node, ast.Attribute):
        return node.attr == "QueueFull"
    if isinstance(node, ast.Name):
        return node.id == "QueueFull"
    if isinstance(node, ast.Tuple):
        return any(_is_queue_full(element) for element in node.elts)
    return False


def _counts_a_drop(handler: ast.ExceptHandler) -> bool:
    """True when the handler increments something named ``dropped``."""

    for node in ast.walk(handler):
        if not isinstance(node, ast.AugAssign):
            continue
        target = node.target
        if isinstance(target, ast.Attribute) and target.attr == "dropped":
            return True
        if isinstance(target, ast.Name) and target.id == "dropped":
            return True
    return False


def _python_sources() -> list[Path]:
    return sorted(
        path
        for root in SEARCH_ROOTS
        for path in (REPO_ROOT / root).rglob("*.py")
    )


def test_every_discarded_event_is_counted():
    sources = _python_sources()
    assert sources, "found no sources to scan; the search roots moved"

    silent: list[str] = []
    uncountable: list[str] = []
    found = 0

    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and _is_queue_full(node.type)
        ]
        if not handlers:
            continue
        found += len(handlers)
        relative = path.relative_to(REPO_ROOT)
        for handler in handlers:
            if not _counts_a_drop(handler):
                silent.append(f"{relative}:{handler.lineno}")
        # Counting a drop nobody can read is the same hole one step later.
        exposes_count = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "dropped_count"
            for node in ast.walk(tree)
        )
        if not exposes_count:
            uncountable.append(str(relative))

    assert not silent, (
        "a full queue discards an event without counting it, so no subscriber "
        f"can ever learn its view has a hole: {silent}"
    )
    assert not uncountable, (
        "a module discards events but exposes no dropped_count, so the count "
        f"cannot reach whoever owns the subscriber's stream: {uncountable}"
    )
    # A guard against the scan silently matching nothing (an import rename, a
    # moved module): the property above is vacuous if it inspected no handlers.
    assert found >= 2, f"expected the known fan-out queues to be scanned, saw {found}"
