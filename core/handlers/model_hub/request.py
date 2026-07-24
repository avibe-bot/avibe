"""Request metadata carried through the frozen EngineAdapter mapping surface."""

from __future__ import annotations

from typing import Any, Mapping


class ModelHubRequest(dict[str, Any]):
    """Raw request body plus the protocol spoken by the local caller."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(payload)
        self.protocol = protocol
        self.headers = dict(headers or {})
