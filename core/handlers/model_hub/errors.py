"""Shared Model Hub integration errors."""

from __future__ import annotations


class ModelDiscoveryError(RuntimeError):
    """A source credential or upstream catalog probe could not be validated.

    ``rejected_status`` identifies a credential refusal (401 or 403).
    ``upstream_status`` and ``upstream_detail`` carry display-only context;
    detail must already be bounded and credential-redacted by the adapter.
    """

    def __init__(
        self, message: str, *, rejected_status: int | None = None,
        upstream_status: int | None = None, upstream_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rejected_status = rejected_status
        self.upstream_status = upstream_status if upstream_status is not None else rejected_status
        self.upstream_detail = upstream_detail
