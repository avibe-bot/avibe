"""Process resource limits for long-running Avibe services."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Open files (sockets, pipes, served assets, logs) a service may hold at once.
# macOS starts launchd-spawned processes at a soft limit of 256: one Show Page
# that embeds a few dozen design canvases opens more files than that, and every
# request that needs a file then fails until some close, including the UI
# server's own config reads. 10240 is macOS's OPEN_MAX, which setrlimit accepts
# for the soft limit on every supported macOS and Linux.
OPEN_FILE_SOFT_LIMIT = 10240


def raise_open_file_limit(target: int = OPEN_FILE_SOFT_LIMIT) -> int | None:
    """Raise this process's open-file soft limit toward ``target``.

    Never lowers it and never exceeds the hard limit. Child processes inherit
    the result. Returns the soft limit now in effect, or None where the limit
    cannot be read (Windows).
    """
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            soft = wanted
        except (ValueError, OSError) as exc:
            logger.warning("Could not raise the open-file limit from %s to %s: %s", soft, wanted, exc)
    return soft
