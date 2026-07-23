"""Model Hub — EngineAdapter interface. FROZEN CONTRACT v1.2 (2026-07-23 11:12 +08:00).

v1.2 changelog (L1 implementation findings — credential & model lifecycle):
- Secret provisioning path closed: ``provision_credential`` moves an API-key
  secret (transient parameter, never logged, never persisted by L2) into the
  ENGINE-OWNED store and returns the opaque ``credential_ref``;
  ``revoke_credential`` releases it on source deletion. L2's config persists
  refs only. Flow: provision → discover (probe) → persist Source → sync.
- ``SourceBinding.model_ids`` added: the declared supply list (discovered +
  manual custom entries) — required by the engine's generic/API-key config.
- ``discover_models`` re-signed as a PROBE (vendor/protocol/base_url/
  credential_ref) so discovery works BEFORE registration; it no longer takes
  a ``source_id``.

v1.1 changelog (L1 review findings, routed via orchestrator):
- OAuth surface added with DETERMINISTIC source binding: ``start_oauth`` takes
  the pre-created (pending) ``source_id``; a ``success`` ``OAuthFlowState``
  carries the resulting opaque ``credential_ref``. Concurrent same-vendor
  flows can never cross-bind.
- Client binding made enforceable at the engine boundary:
  ``SourceBinding.allowed_origins`` + ``invoke(origin=...)`` +
  ``OriginNotAllowedError`` (adapter-side backstop; L2's resolver filters
  first — defense in depth per README invariant 3).

Canonical text: docs/plans/model-hub-contracts/adapter-interface.py (this file).
Dual-copy rule: BOTH lane L1 and lane L2 copy this file VERBATIM to
``core/handlers/model_hub/adapter.py`` in their branches (byte-identical, so the
merge is a no-op). L2 owns the in-repo copy on master thereafter. Any change
routes through the orchestrator and bumps the version line above.

Seam semantics (from spike S1, binding):
- L1 implements this protocol (engine facade). L1 must NOT classify errors,
  retry, or fall back across sources — per-source invocation only; the
  engine's internal fallback and usage feed stay disabled/bypassed.
- L2 consumes it: picks exactly one source per call (priority resolution),
  classifies ``RawCallOutcome`` into the canonical taxonomy, emits redacted
  resolution events.
- No credential material may appear in any field defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence


class EngineHealth(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    NOT_INSTALLED = "not_installed"


@dataclass(frozen=True)
class EngineStatus:
    health: EngineHealth
    installed_version: str | None
    verified: bool
    listen_host: str  # always "127.0.0.1"
    listen_port: int | None
    last_check_iso: str | None


@dataclass(frozen=True)
class SourceBinding:
    """Engine-side registration of one hub-channel source (projection of config)."""

    source_id: str
    vendor: str
    protocol: str  # "anthropic" | "openai_responses" | "openai_chat" | "openai_compatible"
    base_url: str | None  # None => vendor official default
    credential_ref: str  # opaque handle; never secret material
    allowed_origins: tuple[str, ...]  # agent names allowed to draw on this
    # source. Empty tuple = unrestricted (api_key default). Subscription
    # sources MUST be non-empty (README invariant 3); L2 populates, L1
    # enforces as backstop.
    model_ids: tuple[str, ...]  # declared supply list (discovered + manual
    # custom entries); required by the engine's generic/API-key config. Bare
    # model ids, no provider prefix.


class OriginNotAllowedError(Exception):
    """Raised by the adapter when ``invoke(origin=...)`` violates the source's
    ``allowed_origins``. A programming/policy error — never converted into a
    ``RawCallOutcome`` and never triggers fallback."""


class RawOutcomeKind(str, Enum):
    SUCCESS = "success"
    HTTP_ERROR = "http_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"  # malformed/unparseable upstream response


@dataclass(frozen=True)
class RawCallOutcome:
    """Uninterpreted per-call result. Classification is L2's job, not L1's."""

    kind: RawOutcomeKind
    http_status: int | None
    error_code: str | None  # upstream machine code if parseable, else None
    redacted_message: str | None  # L1 guarantees no credential material
    stream_started: bool
    model_id: str
    source_id: str


@dataclass(frozen=True)
class OAuthFlowState:
    """Engine-held (experimental hub-channel) subscription OAuth flow state.

    Deterministic binding: the flow is created FOR a pre-existing pending
    ``source_id`` and, on ``state == "success"``, carries the opaque
    ``credential_ref`` of the auth record the engine created — L2 binds it to
    that source and never guesses, even with concurrent same-vendor flows.
    Mirrors ``oauth-flow.schema.json`` presentation semantics
    (runtime-declared; UI renders from ``expects``).
    """

    flow_id: str
    source_id: str
    vendor: str
    state: str  # "starting" | "awaiting_action" | "verifying" | "success" | "failed" | "cancelled"
    auth_url: str | None
    device_code: str | None
    expects: str  # "none" | "paste_code" | "paste_callback_url"
    instructions_key: str | None
    error_key: str | None
    expires_at_iso: str | None
    credential_ref: str | None  # set iff state == "success"


class InvokeHandle(Protocol):
    """One in-flight upstream call.

    ``stream`` is None iff the call failed before the first byte (outcome is
    then immediately awaitable). When ``stream`` is not None, the caller must
    consume it; ``outcome()`` resolves after the stream ends and reports
    ``stream_started=True`` — per spec §4.2 no transparent retry then.
    """

    @property
    def stream(self) -> AsyncIterator[bytes] | None: ...

    async def outcome(self) -> RawCallOutcome: ...


class EngineAdapter(Protocol):
    # --- lifecycle -------------------------------------------------------
    async def ensure_installed(self) -> EngineStatus: ...

    async def start(self) -> EngineStatus: ...

    async def stop(self) -> None: ...

    async def status(self) -> EngineStatus: ...

    # --- gateway ---------------------------------------------------------
    async def gateway_token(self) -> str:
        """Local gateway token for backend injection (the ONLY credential
        backends ever receive)."""
        ...

    # --- credential provisioning (engine-owned store) ---------------------
    async def provision_credential(
        self,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
    ) -> str:
        """Store an API-key secret in the ENGINE-OWNED credential store and
        return the opaque ``credential_ref``.

        ``secret`` is transient: the adapter must never log it; L2 must never
        persist it (config stores refs only). OAuth credentials never pass
        through here — they are created engine-side by the OAuth flow and
        surfaced via ``OAuthFlowState.credential_ref``."""
        ...

    async def revoke_credential(self, credential_ref: str) -> None:
        """Release the stored credential (source deletion / key replacement)."""
        ...

    # --- source registry (L2 calls on every config change) ---------------
    async def sync_sources(self, bindings: Sequence[SourceBinding]) -> None: ...

    async def discover_models(
        self,
        vendor: str,
        protocol: str,
        base_url: str | None,
        credential_ref: str,
    ) -> Sequence[str]:
        """PROBE the upstream for supplyable model ids. Works before any
        registration (test-and-add flow: provision → discover → persist →
        sync); does not require or create a source binding."""
        ...

    # --- engine-held subscription OAuth (experimental flag only) ----------
    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState: ...

    async def oauth_status(self, flow_id: str) -> OAuthFlowState: ...

    async def submit_oauth(self, flow_id: str, value: str) -> OAuthFlowState:
        """``value`` per ``expects``: pasted code or callback URL."""
        ...

    async def cancel_oauth(self, flow_id: str) -> None: ...

    # --- invocation primitive (exactly one source; no engine fallback) ---
    async def invoke(
        self,
        source_id: str,
        model_id: str,
        request: Mapping[str, Any],
        stream: bool,
        origin: str,
    ) -> InvokeHandle:
        """``origin`` = requesting agent name ("claude"|"codex"|"opencode"|...).
        Raises ``OriginNotAllowedError`` when the binding's ``allowed_origins``
        excludes it (backstop; L2 must have filtered already)."""
        ...
