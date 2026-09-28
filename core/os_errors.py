"""Safe OS diagnostics shared by runtime results and user-facing failures."""

import errno
import os
import ssl
import urllib.error


def local_os_errno(error: BaseException | None) -> int | None:
    """Read a recognized OS code through explicit wrappers, never error prose."""

    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        # SSL errno values name SSL_ERROR_* categories, not operating-system
        # errors, even though SSLError inherits OSError.
        code = (
            error.errno if isinstance(error, OSError) and not isinstance(error, ssl.SSLError)
            else getattr(error, "os_errno", None)
        )
        if type(code) is int and code in errno.errorcode:
            return code
        cause = error.__cause__
        if cause is None and isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
            cause = error.reason
        error = cause
    return None


def local_error_detail(error: BaseException) -> str | None:
    """Publish the system message without filenames, credentials, or payloads."""

    return format_os_errno(local_os_errno(error))


def format_os_errno(code: int | None) -> str | None:
    """Format only recognized numeric OS codes from internal result contracts."""

    if type(code) is not int or code not in errno.errorcode:
        return None
    return f"[Errno {code}] {os.strerror(code)}"
