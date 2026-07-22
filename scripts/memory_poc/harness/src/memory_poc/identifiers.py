from __future__ import annotations

import re

from .errors import HarnessError

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_run_id(run_id: str) -> str:
    """Accept only a single, portable POC run-directory component."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise HarnessError("invalid_run_id")
    return run_id
