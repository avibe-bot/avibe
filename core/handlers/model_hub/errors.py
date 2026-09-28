"""Shared Model Hub integration errors."""

import errno
import os


def local_error_detail(error: Exception) -> str | None:
    """Publish the OS reason, never exception text containing paths or secrets."""

    code = error.errno if isinstance(error, OSError) else getattr(error, "os_errno", None)
    if type(code) is not int or code not in errno.errorcode:
        return None
    return f"[Errno {code}] {os.strerror(code)}"


class ModelDiscoveryError(RuntimeError):
    """A source credential or upstream catalog probe could not be validated."""
