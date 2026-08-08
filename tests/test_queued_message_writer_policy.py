"""Keep the durable send-while-busy queue owned by controller/storage code."""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
QUEUED_TYPE_WRITER_ALLOWLIST = {
    "core/internal_server.py",
    "core/session_turns.py",
    "storage/messages_service.py",
    "storage/workbench_sessions_service.py",
}


def _is_queued_type(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and node.value == "queued"
        or isinstance(node, ast.Name)
        and node.id == "QUEUED_TYPE"
        or isinstance(node, ast.Attribute)
        and node.attr == "QUEUED_TYPE"
    )


def _writes_queued_message_type(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg in {"type", "message_type"}
            and _is_queued_type(node.value)
        ):
            return True
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in {"type", "message_type"}
                    and _is_queued_type(value)
                ):
                    return True
    return False


def test_only_controller_and_storage_modules_write_queued_message_type():
    # ``queued`` is executable queue state, not a Web presentation state. Keep
    # its writers confined to the controller and storage ownership boundary.
    writer_modules = {
        path.relative_to(REPO_ROOT).as_posix()
        for root in ("core", "storage", "vibe")
        for path in (REPO_ROOT / root).rglob("*.py")
        if _writes_queued_message_type(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    }

    assert writer_modules <= QUEUED_TYPE_WRITER_ALLOWLIST
