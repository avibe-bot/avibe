"""Use the Runtime's generated router and upgrade only known stock scaffolds."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path

logger = logging.getLogger(__name__)

# Exact Python History-router output shipped in 3.0.14. Never
# infer ownership from a missing export: routers are freely editable by agents.
_LEGACY_ROUTER_SHA256 = {
    "1154739b3e21e2f1c7f45e3d0b7454dc5541fdf15e2c79bbc2f96f766338706e",  # LF
    "ed7cbd0aa11a491ac8b7621b8a7ea64d7c83c0b53ec46e950cca95b7f4d0079e",  # Windows CRLF
}


def default_show_router() -> str:
    return files("vibe").joinpath("show_router.tsx").read_text(encoding="utf-8")


def upgrade_default_show_router(page_dir: Path) -> bool:
    """Upgrade the exact old scaffold on access, preserving authored content."""
    router = page_dir / "src" / "router.tsx"
    if page_dir.is_symlink() or router.parent.is_symlink():
        return False
    try:
        before = router.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            return False
        original = router.read_bytes()
    except FileNotFoundError:
        return False
    if hashlib.sha256(original).hexdigest() not in _LEGACY_ROUTER_SHA256:
        return False

    # This is a compare-and-replace of an editable source file, not an owned
    # state document: retain its mode and abort if an editor changes it while
    # the replacement is prepared.
    try:
        descriptor, name = tempfile.mkstemp(prefix=".router-", suffix=".tmp", dir=router.parent)
    except PermissionError:
        # A read-only workspace can still serve its existing HTML/root Markdown.
        # Migration is optional on reads; the Runtime explains unsupported
        # subroutes without making those existing representations unavailable.
        logger.warning("Cannot upgrade stock Show Page router in read-only workspace: %s", page_dir.name)
        return False
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(default_show_router().encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary.chmod(stat.S_IMODE(before.st_mode))
        try:
            current = router.lstat()
            if (
                (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns, current.st_size)
                != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size)
                or router.read_bytes() != original
                or page_dir.is_symlink()
                or router.parent.is_symlink()
            ):
                return False
        except FileNotFoundError:
            return False
        os.replace(temporary, router)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info("Upgraded stock Show Page router for SSR Markdown: %s", page_dir.name)
    return True
