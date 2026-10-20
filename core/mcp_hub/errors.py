"""The one error shape every MCP Hub layer raises and serializes."""

from __future__ import annotations

from typing import Any, Optional


class McpHubError(Exception):
    """A categorized failure: ``type`` is the stable ``error.type`` callers branch on."""

    def __init__(
        self,
        type: str,
        message: str,
        *,
        field: Optional[str] = None,
        hint: Optional[str] = None,
        usage: bool = False,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.field = field
        self.hint = hint
        # Usage errors exit 2 at the CLI; everything else exits 1.
        self.usage = usage

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "message": self.message}
        if self.field:
            data["field"] = self.field
        if self.hint:
            data["hint"] = self.hint
        if self.usage:
            data["usage"] = True
        return data

    @classmethod
    def from_payload(cls, data: Any) -> "McpHubError":
        if not isinstance(data, dict):
            return cls("internal", "Unexpected MCP Hub response")
        return cls(
            str(data.get("type") or "internal"),
            str(data.get("message") or ""),
            field=data.get("field"),
            hint=data.get("hint"),
            usage=data.get("usage") is True,
        )
