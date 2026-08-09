"""Central instance-role authorization for local and remote UI requests."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


INSTANCE_ROLES = frozenset({"owner", "editor", "viewer"})
INSTANCE_ACCESS_SOURCES = frozenset(
    {"owner", "public_instance", "email", "email_domain", "organization_group"}
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

REMOTE_HTTP_ALLOWED = "allowed"
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
    is_remote: bool = False
    is_trusted_local: bool = False

    @property
    def is_instance_owner(self) -> bool:
        return self.is_trusted_local or self.instance_role == "owner"

    @property
    def is_active_organization_member(self) -> bool:
        return bool(
            self.organization_id
            and self.organization_member_id
            and self.organization_role in ORGANIZATION_ROLES
        )

    def has_role(self, minimum_role: str) -> bool:
        if self.is_trusted_local:
            return True
        return _ROLE_RANK.get(self.instance_role or "", 0) >= _ROLE_RANK[minimum_role]

    @property
    def can_read_instance(self) -> bool:
        return self.has_role("viewer")

    @property
    def can_chat(self) -> bool:
        return not self.is_remote and self.has_role("editor")

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

    @property
    def can_use_terminal_files(self) -> bool:
        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_terminal(self) -> bool:
        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_files(self) -> bool:
        return not self.is_remote and self.has_role("owner")

    @property
    def can_use_system(self) -> bool:
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

    try:
        require_instance_role(context, required_workbench_event_role(event_type))
    except InstanceAuthorizationError:
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
        r"^/api/show-pages/[^/]+/icon$",
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
        (
            "POST",
            r"^/api/sessions/[^/]+/(?:messages|attachments|cancel|queue/[^/]+/send-now)$",
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
        ("POST", r"^/api/show-pages/[^/]+/visibility$"),
        ("POST", r"^/api/show-pages/[^/]+/(?:rotate-share|share-id)$"),
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

# Positive allowlist for remote-safe owner surfaces. Any route that mutates
# local Agent, IM, Vault, or other execution state must stay absent here and
# therefore use the fail-closed local-only default below.
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
        (frozenset({"GET", "HEAD"}), r"^/api/dock$"),
        (frozenset({"POST"}), r"^/api/dock/pins$"),
        (frozenset({"DELETE"}), r"^/api/dock/pins/[^/]+$"),
        (frozenset({"PUT"}), r"^/api/dock/order$"),
        (frozenset({"PUT"}), r"^/api/workbench/prefs$"),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/web-push/(?:status|vapid-public-key)$",
        ),
        (
            frozenset({"POST"}),
            r"^/api/web-push/(?:status|subscriptions|test)$",
        ),
        (frozenset({"DELETE"}), r"^/api/web-push/subscriptions$"),
        (
            frozenset({"PUT"}),
            r"^/api/resource-policies/[^/]+/[^/]+$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/projects/[^/]+/agents-md$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/(?:sources|agents|events|runtime/status)$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/agents/[^/]+/(?:sources|chain)$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/models/(?:turns/[^/]+/provenance|oauth/status/[^/]+)$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/backend/[^/]+/auth/oauth/status/[^/]+$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/remote-access/status$",
        ),
        (
            frozenset({"GET", "HEAD"}),
            r"^/api/harness/counts$",
        ),
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


def http_authorization_policy(method: str, path: str) -> HttpAuthorizationPolicy:
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
        minimum_role = "viewer" if is_read else "editor"
        remote_access = (
            REMOTE_HTTP_ALLOWED
            if is_read or is_safe_human_event
            else REMOTE_HTTP_LOCAL_ONLY
        )
        return HttpAuthorizationPolicy(minimum_role, remote_access)
    if not path.startswith("/api/"):
        return HttpAuthorizationPolicy(None)

    minimum_role = "owner"
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
