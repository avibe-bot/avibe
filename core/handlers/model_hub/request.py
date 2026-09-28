"""Request metadata carried through the frozen EngineAdapter surface."""

from __future__ import annotations

from typing import Any, Final, Mapping

# Caller headers that cross from an agent CLI's request to the local engine.
# Protocol capability headers select request features. ``user-agent`` keeps
# the calling client's identity: the engine forwards it upstream, and some
# Anthropic-compatible gateways rewrite the system prompt of any request that
# does not identify as Claude Code. Without it the upstream sees Avibe's HTTP
# library instead of the agent. Credentials and all other headers stay local.
FORWARDED_CALLER_HEADERS: Final = frozenset(
    {
        "anthropic-beta",
        "anthropic-version",
        "openai-beta",
        "user-agent",
    }
)


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
