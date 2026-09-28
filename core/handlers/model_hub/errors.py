"""Shared Model Hub integration errors."""

import errno
import os


def local_error_detail(error: Exception) -> str | None:
    """Publish the OS reason, never exception text containing paths or secrets."""

    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        code = cause.errno if isinstance(cause, OSError) else getattr(cause, "os_errno", None)
        if type(code) is int and code in errno.errorcode:
            return f"[Errno {code}] {os.strerror(code)}"
        # Explicit wrapping preserves causality; implicit exception context may
        # instead describe an unrelated failure handled during cleanup.
        cause = cause.__cause__
    return None


class ModelDiscoveryError(RuntimeError):
    """A source credential or upstream catalog probe could not be validated."""
