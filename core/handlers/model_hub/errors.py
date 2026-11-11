"""Shared Model Hub integration errors."""

from __future__ import annotations


class ModelDiscoveryError(RuntimeError):
    """A source credential or upstream catalog probe could not be validated.

    ``rejected_status`` is the upstream HTTP status when the provider refused the
    credential itself (401 or 403), and ``None`` for every other failure.
    """

    def __init__(self, message: str, *, rejected_status: int | None = None) -> None:
        super().__init__(message)
        self.rejected_status = rejected_status
