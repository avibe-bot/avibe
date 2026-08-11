"""Central instance-role authorization for local and remote UI requests."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from core.inbox_events import (
    DEFINITIONS_UPDATED_EVENT,
    RUNS_UPDATED_EVENT,
    VAULTS_UPDATED_EVENT,
)


INSTANCE_ROLES = frozenset({"owner", "editor", "viewer"})
INSTANCE_ACCESS_SOURCES = frozenset(
    {
        "owner",
        "public_instance",
        "email",
        "email_domain",
        "organization_group",
        "show_page_email",
    }
)
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
_RESOURCE_USE_MINIMUM_ROLES = {
    "agent": "editor",
    "skill": "editor",
    "vault_secret": "editor",
    "show_page": "viewer",
}

_VIEWER_WORKBENCH_EVENTS = frozenset(
    {
        "authorization.changed",
        "connected",
        "inbox.session.updated",
        "inbox.unread.changed",
        "message.new",
        "session.activity",
        "session.status",
        "turn.end",
        "turn.start",
        "workbench.events.bridge.status",
    }
)
_EDITOR_WORKBENCH_EVENTS = frozenset({"queue.updated", "show.event"})
_PRIVILEGED_RUNTIME_WORKBENCH_EVENTS = frozenset(
    {
        DEFINITIONS_UPDATED_EVENT,
        RUNS_UPDATED_EVENT,
        VAULTS_UPDATED_EVENT,
    }
)

REMOTE_HTTP_ALLOWED = "allowed"
REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER = "active_organization_member"
REMOTE_HTTP_LOCAL_ONLY = "local_only"
REMOTE_HTTP_PAYLOAD_FILTERED = "payload_filtered"


@dataclass(frozen=True)
class HttpAuthorizationPolicy:
    minimum_role: str | None
    remote_access: str = REMOTE_HTTP_ALLOWED


@dataclass(frozen=True)
class AuthorizationContext:
    instance_role: str | None = None
    subject: str | None = None
    email: str | None = None
    instance_id: str | None = None
    instance_access_source: str | None = None
    organization_id: str | None = None
    organization_member_id: str | None = None
    organization_role: str | None = None
    group_ids: frozenset[str] = frozenset()
    membership_version: str | None = None
    claims_issued_at: int | None = None
    authorization_revision: int | None = None
    show_page_id: str | None = None
    is_remote: bool = False
    is_trusted_local: bool = False

    @property
    def is_instance_owner(self) -> bool:
        if self.is_trusted_local:
            return True
        return self._has_admitted_remote_identity() and self.instance_role == "owner"

    @property
    def is_active_organization_member(self) -> bool:
        return bool(
            self.instance_role in INSTANCE_ROLES
            and self.instance_access_source in INSTANCE_ACCESS_SOURCES
            and self.organization_id
            and self.organization_member_id
            and self.organization_role in ORGANIZATION_ROLES
        )

    def _has_admitted_remote_identity(self) -> bool:
        """Return whether this context carries an identity accepted for runtime use.

        HTTP admission validates the signed session before a request reaches a
        service. Services are also callable directly, so they must not infer
        authority from a caller-constructed ``owner`` role alone. Show Page
        email grants are the one non-Organization remote identity and retain
        their signed viewer/page scope.
        """

        if not self.is_remote:
            return True
        if self.instance_access_source == "show_page_email":
            return self.instance_role == "viewer" and bool(self.show_page_id)
        return self.is_active_organization_member

    def has_role(self, minimum_role: str) -> bool:
        if self.is_trusted_local:
            return True
        if not self._has_admitted_remote_identity():
            return False
        return _ROLE_RANK.get(self.instance_role or "", 0) >= _ROLE_RANK[minimum_role]

    @property
    def can_read_instance(self) -> bool:
        return self.has_role("viewer")

    @property
    def can_chat(self) -> bool:
        return self.has_role("editor")

    @property
    def can_use_cloud_asr(self) -> bool:
        """Allow the explicit remote Cloud ASR capability, not local execution."""

        return self.is_remote and self.has_role("editor")

    @property
    def can_manage_projects(self) -> bool:
        return self.has_role("owner")

    @property
    def can_manage_agents(self) -> bool:
        return self.has_role("owner")

    @property
    def can_manage_instance(self) -> bool:
        return self.has_role("owner")

    def can_use_resource(self, resource_kind: str) -> bool:
        """Return whether the Instance role may use this resource kind.

        Effective Resource ACL is a separate, mandatory check performed by the
        resource service. Unknown kinds fail closed so adding a resource type
        never makes it editor-visible by accident.
        """

        minimum_role = _RESOURCE_USE_MINIMUM_ROLES.get(resource_kind)
        return minimum_role is not None and self.has_role(minimum_role)

    def can_use_show_page(self, show_page_id: str) -> bool:
        """Return whether the signed session carries this exact page entitlement."""

        return bool(self.show_page_id and self.show_page_id == show_page_id)

    @property
    def can_use_terminal_files(self) -> bool:
        """Legacy trusted-local control capability, not an Apps visibility gate."""

        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_terminal(self) -> bool:
        """Legacy trusted-local control capability, not Terminal App access."""

        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_files(self) -> bool:
        """Legacy local-project filesystem capability, not Files App access."""

        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_system(self) -> bool:
        """Return access to trusted-local control-plane surfaces.

        Apps are not system administration and must not use this capability as
        an availability gate.
        """

        return not self.is_remote and self.has_role("owner")

    def capability_projection(self) -> dict[str, bool]:
        return {
            "is_instance_owner": self.is_instance_owner,
            "can_read_instance": self.can_read_instance,
            "can_chat": self.can_chat,
            "can_manage_projects": self.can_manage_projects,
            "can_manage_agents": self.can_manage_agents,
            "can_manage_instance": self.can_manage_instance,
            "can_use_agents": self.can_use_resource("agent"),
            "can_use_skills": self.can_use_resource("skill"),
            "can_use_vault_secrets": self.can_use_resource("vault_secret"),
            "can_use_show_pages": self.can_use_resource("show_page"),
            "can_use_terminal_files": self.can_use_terminal_files,
            "can_use_terminal": self.can_use_terminal,
            "can_use_files": self.can_use_files,
            "can_use_system": self.can_use_system,
        }


def has_temporary_unrestricted_org_access(
    context: AuthorizationContext | Mapping[str, Any] | None,
) -> bool:
    """Return whether the temporary unrestricted Organization policy applies.

    This is an HTTP/product rollout rule, not a projected capability. Until the
    per-surface authorization model tracked in avibe#1313 ships, every
    authenticated active Organization member may use the explicitly opened
    runtime surfaces. Exact Show Page email grants remain confined to their
    signed page subtree.
    """

    resolved = (
        context
        if isinstance(context, AuthorizationContext)
        else context_from_session_payload(context)
        if context is not None
        else None
    )
    return bool(
        resolved is not None
        and resolved.is_remote
        and resolved.instance_access_source != "show_page_email"
        and resolved.is_active_organization_member
    )


def has_temporary_unrestricted_org_app_access(
    context: AuthorizationContext | Mapping[str, Any] | None,
) -> bool:
    """Backward-compatible alias for the renamed unrestricted policy signal."""

    return has_temporary_unrestricted_org_access(context)


def has_temporary_unrestricted_runtime_access(
    context: AuthorizationContext | Mapping[str, Any] | None,
) -> bool:
    """Return the temporary runtime admission signal without changing identity.

    Callers must use this predicate for the explicitly opened runtime surfaces
    instead of treating an Organization member as a trusted-local principal or
    projecting synthetic owner capabilities to the browser.
    """

    return has_temporary_unrestricted_org_access(context)


def _optional_string(value: Any, *, limit: int = 320) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return None
    return cleaned


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def context_from_session_payload(payload: Mapping[str, Any]) -> AuthorizationContext:
    role = _optional_string(payload.get("vibe_instance_role", payload.get("instance_role")))
    if role not in INSTANCE_ROLES:
        return AuthorizationContext(is_remote=True)
    access_source = _optional_string(
        payload.get("vibe_instance_access_source", payload.get("instance_access_source"))
    )
    if access_source not in INSTANCE_ACCESS_SOURCES:
        return AuthorizationContext(is_remote=True)
    show_page_id = _optional_string(payload.get("vibe_show_page_id"), limit=200)
    if access_source == "show_page_email" and show_page_id is None:
        return AuthorizationContext(is_remote=True)
    if access_source == "show_page_email" and role != "viewer":
        return AuthorizationContext(is_remote=True)
    raw_groups = payload.get("vibe_group_ids", payload.get("group_ids", []))
    group_ids = (
        frozenset(value for item in raw_groups if (value := _optional_string(item)) is not None)
        if isinstance(raw_groups, list)
        else frozenset()
    )
    organization_role = _optional_string(
        payload.get("vibe_organization_role", payload.get("organization_role"))
    )
    if organization_role not in ORGANIZATION_ROLES:
        organization_role = None
    return AuthorizationContext(
        instance_role=role,
        subject=_optional_string(payload.get("sub")),
        email=_optional_string(payload.get("email")),
        instance_id=_optional_string(payload.get("vibe_instance_id", payload.get("instance_id"))),
        instance_access_source=access_source,
        organization_id=_optional_string(
            payload.get("vibe_organization_id", payload.get("organization_id"))
        ),
        organization_member_id=_optional_string(
            payload.get("vibe_organization_member_id", payload.get("organization_member_id"))
        ),
        organization_role=organization_role,
        group_ids=group_ids,
        membership_version=_optional_string(
            payload.get("vibe_membership_version", payload.get("membership_version"))
        ),
        claims_issued_at=_optional_positive_int(
            payload.get("claims_issued_at", payload.get("iat"))
        ),
        authorization_revision=_optional_nonnegative_int(
            payload.get(
                "vibe_instance_authorization_revision",
                payload.get("authorization_revision"),
            )
        ),
        show_page_id=show_page_id,
        is_remote=True,
    )


def trusted_local_context() -> AuthorizationContext:
    return AuthorizationContext(instance_role="owner", is_trusted_local=True)


class InstanceAuthorizationError(PermissionError):
    def __init__(self, minimum_role: str):
        super().__init__(f"Instance role '{minimum_role}' is required")
        self.code = "instance_access_forbidden"
        self.minimum_role = minimum_role


def require_instance_role(
    context: AuthorizationContext | Mapping[str, Any] | None,
    minimum_role: str,
) -> AuthorizationContext:
    """Authorize a service call; omitted context denotes a trusted local caller."""

    resolved = (
        trusted_local_context()
        if context is None
        else context
        if isinstance(context, AuthorizationContext)
        else context_from_session_payload(context)
    )
    # The temporary rollout intentionally has no per-surface role split for an
    # active Organization member, but it does not turn an arbitrary remote
    # Instance owner into an Organization member. Keep exact Show Page email
    # grants on their signed viewer role; the HTTP/page boundary scopes them to
    # the one page separately.
    if resolved.is_remote and resolved.instance_access_source != "show_page_email":
        if not has_temporary_unrestricted_org_access(resolved):
            raise InstanceAuthorizationError(minimum_role)
        return resolved
    if not resolved.has_role(minimum_role):
        raise InstanceAuthorizationError(minimum_role)
    return resolved


def required_workbench_event_role(event_type: str) -> str:
    """Return the minimum role for a browser workbench event.

    Unknown events default to owner so newly published management events are
    not exposed to remote editors or viewers before their policy is defined.
    """

    if event_type in _VIEWER_WORKBENCH_EVENTS:
        return "viewer"
    if event_type in _EDITOR_WORKBENCH_EVENTS:
        return "editor"
    return "owner"


def can_receive_workbench_event(
    context: AuthorizationContext | Mapping[str, Any] | None,
    event_type: str,
) -> bool:
    """Return whether an SSE subscriber may receive a workbench event."""

    known_event = event_type in (
        _VIEWER_WORKBENCH_EVENTS
        | _EDITOR_WORKBENCH_EVENTS
        | _PRIVILEGED_RUNTIME_WORKBENCH_EVENTS
    )
    if has_temporary_unrestricted_org_access(context) and known_event:
        return True
    # A signed Show Page email grant is still a viewer session, but its exact
    # page subtree is enforced by the payload/resource visibility filters below.
    if (
        event_type == "show.event"
        and isinstance(context, AuthorizationContext)
        and context.instance_access_source == "show_page_email"
        and context.can_use_show_page(context.show_page_id or "")
    ):
        return True
    # Unknown event names remain owner-only. The temporary rollout is an
    # explicit allowlist for known runtime events, not a blanket event-bus
    # capability that would expose a future control-plane event. Existing
    # remote owners still retain their established owner-level behavior.
    if not known_event and has_temporary_unrestricted_org_access(context):
        return False
    try:
        require_instance_role(context, required_workbench_event_role(event_type))
    except InstanceAuthorizationError:
        return False
    if getattr(context, "is_remote", False) and event_type in _PRIVILEGED_RUNTIME_WORKBENCH_EVENTS:
        return False
    return True


_VIEWER_HTTP_RULES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/api/session$",
        r"^/api/csrf-token$",
        r"^/api/config$",
        r"^/api/version$",
        r"^/api/platforms$",
        r"^/api/projects(?:/[^/]+)?$",
        r"^/api/workbench/prefs$",
        r"^/api/workbench/projects-bootstrap$",
        r"^/api/sessions$",
        r"^/api/sessions/[^/]+$",
        r"^/api/sessions/[^/]+/(?:bootstrap|messages|activity|turn-state)$",
        r"^/api/search/messages$",
        r"^/api/events$",
        r"^/api/inbox$",
        r"^/api/media/[^/]+(?:/meta)?$",
        r"^/api/org/(?:context|groups)$",
        r"^/api/resource-policies$",
        r"^/api/show-pages$",
        r"^/api/show-pages/[^/]+/access$",
        r"^/api/show-pages/[^/]+/authorized-emails$",
        r"^/api/show-pages/[^/]+/icon$",
    )
)

_VIEWER_HTTP_MUTATION_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("POST", r"^/api/show-pages/[^/]+/ensure$"),
        ("POST", r"^/api/show-pages/[^/]+/visibility$"),
        ("POST", r"^/api/show-pages/[^/]+/rotate-share$"),
        ("POST", r"^/api/show-pages/[^/]+/share-id$"),
        ("PUT", r"^/api/show-pages/[^/]+/authorized-emails$"),
    )
)

_EDITOR_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/api/agents$"),
        ("GET", r"^/api/agent-backends$"),
        ("GET", r"^/api/skills$"),
        ("GET", r"^/api/vault/(?:secrets|tags)$"),
        ("GET", r"^/api/cloud/token$"),
        ("GET", r"^/api/asr/status$"),
        ("GET", r"^/api/sessions/[^/]+/(?:archive-preview|queue|draft)$"),
        ("POST", r"^/api/sessions$"),
        ("POST", r"^/api/sessions/[^/]+/fork$"),
        ("PATCH", r"^/api/sessions/[^/]+$"),
        ("DELETE", r"^/api/sessions/[^/]+$"),
        ("POST", r"^/api/sessions/[^/]+/(?:messages|attachments|cancel|mark-read)$"),
        ("DELETE", r"^/api/sessions/[^/]+/queue/[^/]+$"),
        ("POST", r"^/api/sessions/[^/]+/queue/[^/]+/send-now$"),
        ("PUT", r"^/api/sessions/[^/]+/draft$"),
        ("POST", r"^/api/asr/transcribe$"),
        ("POST", r"^/api/show/sessions/[^/]+/events$"),
        ("POST", r"^/api/show/sessions/[^/]+/prewarm$"),
        ("POST", r"^/api/vault/requests/(?:access|sign)$"),
    )
)

_REMOTE_LOCAL_ONLY_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("DELETE", r"^/api/sessions/[^/]+(?:/queue/[^/]+)?$"),
        ("POST", r"^/api/sessions/[^/]+/fork$"),
        ("GET", r"^/api/sessions/[^/]+/draft$"),
        ("PUT", r"^/api/sessions/[^/]+/draft$"),
        ("POST", r"^/api/sessions/[^/]+/mark-read$"),
        (
            "POST",
            r"^/api/sessions/[^/]+/(?:attachments|cancel|queue/[^/]+/send-now)$",
        ),
        ("POST", r"^/api/asr/transcribe$"),
        ("POST", r"^/api/show/sessions/[^/]+/(?:events|prewarm)$"),
        ("GET", r"^/api/settings$"),
        ("HEAD", r"^/api/settings$"),
        ("POST", r"^/api/settings$"),
        ("POST", r"^/api/settings/thread$"),
        ("DELETE", r"^/api/settings/thread$"),
        ("GET", r"^/api/bind-codes$"),
        ("DELETE", r"^/api/projects/[^/]+$"),
        ("GET", r"^/api/harness/(?:runs|bootstrap|runs/[^/]+)$"),
        ("GET", r"^/api/vault/(?:pubkey|agent/pubkey|sandbox/root-metadata|vmk)$"),
        ("GET", r"^/api/global-prompts$"),
        # Baseline for remote identities outside the temporary active-member
        # rollout. The explicit matrix below admits the same Model Hub metadata
        # and mutations for active Organization members.
        ("GET", r"^/api/models/sources$"),
        ("HEAD", r"^/api/models/sources$"),
        ("GET", r"^/api/skills/(?:check|find)$"),
        ("HEAD", r"^/api/skills/(?:check|find)$"),
        ("POST", r"^/api/vault/requests/(?:access|sign)$"),
        ("GET", r"^/api/users$"),
        ("HEAD", r"^/api/users$"),
    )
)

_REMOTE_PAYLOAD_FILTERED_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        (
            "POST",
            r"^/api/(?:config|projects|sessions)$",
        ),
        ("PATCH", r"^/api/(?:projects|sessions)/[^/]+$"),
    )
)

# Temporary unrestricted runtime surface for authenticated active Organization
# members. This is deliberately an explicit route matrix: Organization auth,
# membership, tunnel pairing, cloud-management, and unknown future API routes
# still use their existing policy. The per-surface authorization model is
# tracked in avibe#1313.
_UNRESTRICTED_ORG_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_TEMPORARY_UNRESTRICTED_ORG_HTTP_RULES = tuple(
    (methods, re.compile(pattern))
    for methods, pattern in (
        # Settings, configuration, workbench preferences, and service control.
        # Config responses use the local runtime projection with only the
        # pairing/tunnel block removed.
        (frozenset({"POST"}), r"^/api/config$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:settings|settings/thread|workbench/prefs|ui/reload)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:control|doctor|logs|upgrade)$"),
        # The shell polls the process status and the setup pages use these
        # local discovery/configuration helpers. They do not alter Organization
        # identity, pairing, or tunnel state.
        (_UNRESTRICTED_ORG_METHODS, r"^/status$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:cli/detect|slack/manifest)$"),
        # Harness, Agent definitions, global prompts, and runtime diagnostics.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/harness/(?:counts|tasks|tasks/[^/]+|watches|watches/[^/]+|runs|runs/[^/]+|bootstrap)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/agents(?:/(?:default|import|[^/]+))?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:agent-backends|agents-graph|agent-onboarding|running-agents(?:/end)?)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/agent/[^/]+/install(?:/[^/]+)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/global-prompts$"),
        # Skills and their installed runtime dependencies.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/skills(?:/(?:preview|find|check|update|upload|[^/]+))?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/dependencies(?:/[^/]+(?:/install(?:/[^/]+)?)?)?$"),
        # Vault inventory, key material, secret CRUD, grants, approvals, and audit.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/vault/(?:secrets(?:/[^/]+(?:/reveal-context)?)?|tags|pubkey|agent/pubkey|sandbox/root-metadata|agent-bindings:batch|agent-binding|settings|vmk|authz/factors/webauthn(?:/options)?|signing-addresses|requests(?:/[^/]+(?:/(?:deny|fulfill-access))?)?|provision-requests(?:/(?:by-id/)?[^/]+)?|grants(?:/[^/]+)?|sign|pubkey-pin|audit)$"),
        # Model Hub source/mapping/OAuth/runtime management.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/sources(?:/[^/]+(?:/(?:credential|reauth|refresh|models(?:/.*)?)?)?)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/(?:agents|events)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/agents/(?:opencode/menu|[^/]+/(?:sources|mode|mappings|chain|probe))$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/turns/[^/]+/provenance$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/oauth/(?:start|status/[^/]+|submit|cancel)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/migration/(?:scan|apply)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/runtime/(?:status|start)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/models/sources/[^/]+/models(?:/.*)?$"),
        # Memory administration.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/memory/(?:settings|status|failures|profile|log|log/entry|search|runtime/restart|clear)$"),
        # Projects, sessions, chat execution, and ASR.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/projects(?:/[^/]+(?:/agents-md)?)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/workbench/projects-bootstrap$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/sessions(?:/[^/]+(?:/(?:fork|bootstrap|archive-preview|messages|activity|cli-activity|cancel|mark-read|turn-state|queue(?:/[^/]+(?:/send-now)?)?|draft|attachments))?)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:search/messages|events|inbox)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/asr/(?:transcribe|telemetry|status)$"),
        # Apps: Dock, Files, browse favorites, Terminal teardown, and Show Pages.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/dock(?:/pins(?:/[^/]+)?|/order)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/files/(?:list|meta|content|write|upload|mkdir|rename|move|copy|delete|delete/undo|search|search_names|search/replace|search/undo)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/media/[^/]+(?:/meta)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/browse(?:/favorites|/mkdir)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/terminal/[^/]+$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/show-pages(?:/[^/]+(?:/(?:visibility|ensure|access|authorized-emails|rotate-share|share-id|icon))?)?$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/show/sessions/[^/]+/(?:events|prewarm)$"),
        # Agent backends, provider setup, platform settings, and channel tools.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/backend/(?:codex|claude|opencode)/(?:runtime|restart)$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/backend/(?:codex|claude)/auth$"),
        (
            _UNRESTRICTED_ORG_METHODS,
            r"^/api/backend/(?:codex|claude|opencode)/auth/(?:oauth/(?:start|status/[^/]+|submit-code|cancel|remove)|api-key/remove|test)$",
        ),
        (
            _UNRESTRICTED_ORG_METHODS,
            r"^/api/backend/claude/auth/oauth/credentials/remove$",
        ),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/backend/opencode/(?:providers|custom-provider(?:/[^/]+)?|default-provider|provider/[^/]+/(?:auth(?:/.*)?|test|models(?:/.*)?))$"),
        (
            _UNRESTRICTED_ORG_METHODS,
            r"^/api/(?:opencode/(?:options|permission-status|setup-permission)|claude/(?:agents|models)|codex/(?:agents|models))$",
        ),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/(?:platforms|slack(?:/(?:auth_test|channels))?|discord/(?:auth_test|channels|guilds)|telegram/(?:auth_test|chats)|lark/(?:auth_test|chats|temp_ws/(?:start|stop))|wechat/qr_login/(?:start|poll)|channels/delete)$"),
        # Push registration is a runtime notification surface.
        (_UNRESTRICTED_ORG_METHODS, r"^/api/web-push/(?:status|vapid-public-key|subscriptions|test)$"),
        # Local IM users and bind codes are part of the temporarily unrestricted
        # runtime administration surface. This does not include Organization
        # membership, Cloud pairing, or tunnel control routes.
        (frozenset({"GET", "HEAD", "POST"}), r"^/api/users$"),
        (frozenset({"POST"}), r"^/api/users/[^/]+/admin$"),
        (frozenset({"DELETE"}), r"^/api/users/[^/]+$"),
        (frozenset({"GET", "HEAD", "POST"}), r"^/api/bind-codes$"),
        (frozenset({"DELETE"}), r"^/api/bind-codes/[^/]+$"),
        (frozenset({"GET", "HEAD"}), r"^/api/setup/first-bind-code$"),
        (_UNRESTRICTED_ORG_METHODS, r"^/api/resource-policies(?:/[^/]+/[^/]+)?$"),
    )
)

# Compatibility name retained for callers/tests from the previous Apps-only
# rollout. New code should use the unrestricted Organization matrix above.
_TEMPORARY_ORGANIZATION_APP_HTTP_RULES = _TEMPORARY_UNRESTRICTED_ORG_HTTP_RULES

# Compatibility allowlist for remote identities outside the temporary active
# Organization rollout. The unrestricted active-member matrix is evaluated
# first; unknown routes still use the fail-closed local-only default below.
_REMOTE_OWNER_ALLOWED_HTTP_RULES = tuple(
    (methods, re.compile(pattern))
    for methods, pattern in (
        (frozenset({"GET", "HEAD", "POST"}), r"^/api/agent-onboarding$"),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/agents/[^/]+$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/vault/(?:settings|requests|requests/[^/]+|provision-requests/[^/]+|provision-requests/by-id/[^/]+|grants|audit)$",
        ),
        (
            frozenset({"POST"}),
            r"^/api/show-pages/[^/]+/ensure$",
        ),
        (
            frozenset({"GET", "HEAD", "PUT"}),
            r"^/api/show-pages/[^/]+/authorized-emails$",
        ),
        # Dock reads and writes are admitted only by the temporary Organization
        # Apps matrix above; Workbench preference writes remain local-only.
        # Reading push status stays remote, and so does the `POST` form of it:
        # it reports this principal's own subscription count and can only
        # re-attach a device id to an endpoint that is already stored and
        # enabled for that same principal, so it can neither introduce an
        # endpoint nor send to one.
        #
        # Registering and testing a subscription are different: the endpoint is
        # caller-supplied data that `send_web_push()` later fetches from the
        # Avibe host, and nothing between the payload and the request restricts
        # it to a real push service. A remote caller could register an HTTPS
        # endpoint aimed at loopback, a private LAN host or a rebinding name and
        # then have this host issue the request from inside the network, so the
        # write and the test stay local until the endpoint is checked against
        # its resolved addresses.
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/web-push/(?:status|vapid-public-key)$",
        ),
        (frozenset({"POST"}), r"^/api/web-push/status$"),
        (
            frozenset({"PUT"}),
            r"^/api/resource-policies/[^/]+/[^/]+$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/(?:agents|events)$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/agents/[^/]+/(?:sources|chain)$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/turns/[^/]+/provenance$",
        ),
        # OAuth polling follows its flow: `oauth/start` and the Settings
        # `auth/oauth/start` are local-only, so only the local owner can open a
        # flow, while a status poll returns that flow's authorization URL and
        # device code and - once the Model Hub flow succeeds - the same
        # credential and account payload `/api/models/sources` deliberately
        # keeps local. A remote caller holding a flow id would read the local
        # owner's in-flight login, so both status routes stay local too.
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/remote-access/status$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/harness/counts$",
        ),
        # Memory reads that describe this principal's own view stay remote, and
        # search is scoped to the caller. Shared provider settings and the
        # process-global failure log do not: both expose data across principals,
        # so they stay trusted-local with the admin log and other sidecar-wide
        # operations. Memory administration is absent for the same reason: a
        # settings PATCH repoints the shared provider endpoints, and
        # `runtime/restart` / `clear` act on the whole local sidecar rather than
        # one principal.
        (frozenset({"GET", "HEAD"}), r"^/api/memory/(?:status|profile)$"),
        (frozenset({"POST"}), r"^/api/memory/search$"),
    )
)


def _http_rule_matches(
    method: str,
    path: str,
    rules: tuple[tuple[str, re.Pattern[str]], ...],
) -> bool:
    return any(rule_method == method and pattern.fullmatch(path) for rule_method, pattern in rules)


def _owner_http_rule_matches(method: str, path: str) -> bool:
    return any(
        method in methods and pattern.fullmatch(path)
        for methods, pattern in _REMOTE_OWNER_ALLOWED_HTTP_RULES
    )


def _temporary_unrestricted_org_rule_matches(method: str, path: str) -> bool:
    return any(
        method in methods and pattern.fullmatch(path)
        for methods, pattern in _TEMPORARY_UNRESTRICTED_ORG_HTTP_RULES
    )


def _temporary_organization_app_rule_matches(method: str, path: str) -> bool:
    """Compatibility alias for the previous Apps-only policy matcher."""

    return _temporary_unrestricted_org_rule_matches(method, path)


def http_authorization_policy(
    method: str,
    path: str,
    *,
    temporary_org_access: bool = False,
) -> HttpAuthorizationPolicy:
    """Return role and remote-exposure policy for one HTTP request.

    Explicit viewer/editor and owner-management routes keep their approved remote
    behavior. Unknown API routes fail closed to trusted-local callers so adding a
    new owner-only endpoint cannot silently expose local machine capabilities.
    """

    normalized_method = method.upper()
    # Organization management is an explicit Cloud proxy namespace. Cloud user
    # identity and object authorization are re-evaluated by that boundary.
    if path.startswith("/api/cloud-management/"):
        return HttpAuthorizationPolicy("viewer")
    if path.startswith("/show/"):
        is_read = normalized_method in {"GET", "HEAD", "OPTIONS"}
        # The server-owned event endpoint does not proxy into Show Runtime. Its
        # handler rejects any remote request that would dispatch an Agent turn
        # before the event store can reserve a delivery.
        is_safe_human_event = normalized_method == "POST" and re.fullmatch(
            r"^/show/[^/]+/(?:__show/events|__events)$",
            path,
        )
        minimum_role = "viewer"
        remote_access = (
            REMOTE_HTTP_ALLOWED
            if is_read or is_safe_human_event
            else REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER
        )
        return HttpAuthorizationPolicy(minimum_role, remote_access)
    if path == "/status":
        return HttpAuthorizationPolicy(None, REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER)
    if not path.startswith("/api/"):
        return HttpAuthorizationPolicy(None)
    if _temporary_unrestricted_org_rule_matches(normalized_method, path):
        # The matrix is an identity-independent route classification. The
        # request boundary below admits it only after verifying the signed
        # active-Organization claim; ordinary remote callers still receive
        # ``remote_execution_disabled`` there. The optional keyword is retained
        # for callers from the previous Apps-only rollout, but classification is
        # deliberately context-independent.
        minimum_role = (
            "owner"
            if re.fullmatch(r"^/api/projects/[^/]+/agents-md$", path)
            else "viewer"
        )
        return HttpAuthorizationPolicy(minimum_role, REMOTE_HTTP_ACTIVE_ORGANIZATION_MEMBER)

    minimum_role = "owner"
    if _http_rule_matches(normalized_method, path, _VIEWER_HTTP_MUTATION_RULES):
        # These mutations enforce Show Page ownership and Organization authority
        # in the resource service. Admit a viewer to that final authorization gate.
        minimum_role = "viewer"
    else:
        for rule_method, pattern in _EDITOR_HTTP_RULES:
            if normalized_method == rule_method and pattern.fullmatch(path):
                minimum_role = "editor"
                break
        else:
            if normalized_method in {"GET", "HEAD", "OPTIONS"} and any(
                pattern.fullmatch(path) for pattern in _VIEWER_HTTP_RULES
            ):
                minimum_role = "viewer"

    if _http_rule_matches(normalized_method, path, _REMOTE_LOCAL_ONLY_HTTP_RULES):
        remote_access = REMOTE_HTTP_LOCAL_ONLY
    elif _http_rule_matches(
        normalized_method,
        path,
        _REMOTE_PAYLOAD_FILTERED_HTTP_RULES,
    ):
        remote_access = REMOTE_HTTP_PAYLOAD_FILTERED
    elif minimum_role in {"viewer", "editor"} or _owner_http_rule_matches(
        normalized_method,
        path,
    ):
        remote_access = REMOTE_HTTP_ALLOWED
    else:
        remote_access = REMOTE_HTTP_LOCAL_ONLY
    return HttpAuthorizationPolicy(minimum_role, remote_access)


def required_instance_role(method: str, path: str) -> str | None:
    """Return the minimum role for a remote HTTP request.

    Non-API page/static reads are handled by the authenticated shell. Unknown
    API routes deliberately default to owner so a newly added management route
    cannot accidentally become available to editors or viewers.
    """

    return http_authorization_policy(method, path).minimum_role
