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
    is_remote: bool = False
    is_trusted_local: bool = False

    @property
    def is_instance_owner(self) -> bool:
        return self.is_trusted_local or self.instance_role == "owner"

    def has_role(self, minimum_role: str) -> bool:
        if self.is_trusted_local:
            return True
        return _ROLE_RANK.get(self.instance_role or "", 0) >= _ROLE_RANK[minimum_role]

    @property
    def can_read_instance(self) -> bool:
        return self.has_role("viewer")

    @property
    def can_chat(self) -> bool:
        return self.has_role("editor")

    @property
    def can_manage_projects(self) -> bool:
        return self.has_role("owner")

    @property
    def can_manage_agents(self) -> bool:
        return self.has_role("owner")

    @property
    def can_manage_instance(self) -> bool:
        return self.has_role("owner")

    @property
    def can_use_terminal_files(self) -> bool:
        return self.has_role("owner")

    @property
    def can_use_terminal(self) -> bool:
        return self.has_role("owner")

    @property
    def can_use_files(self) -> bool:
        return self.has_role("owner")

    @property
    def can_use_system(self) -> bool:
        return self.has_role("owner")

    def capability_projection(self) -> dict[str, bool]:
        return {
            "is_instance_owner": self.is_instance_owner,
            "can_read_instance": self.can_read_instance,
            "can_chat": self.can_chat,
            "can_manage_projects": self.can_manage_projects,
            "can_manage_agents": self.can_manage_agents,
            "can_manage_instance": self.can_manage_instance,
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


def context_from_session_payload(payload: Mapping[str, Any]) -> AuthorizationContext:
    role = _optional_string(payload.get("vibe_instance_role"))
    if role not in INSTANCE_ROLES:
        return AuthorizationContext(is_remote=True)
    access_source = _optional_string(payload.get("vibe_instance_access_source"))
    if access_source not in INSTANCE_ACCESS_SOURCES:
        return AuthorizationContext(is_remote=True)
    raw_groups = payload.get("vibe_group_ids", [])
    group_ids = (
        frozenset(value for item in raw_groups if (value := _optional_string(item)) is not None)
        if isinstance(raw_groups, list)
        else frozenset()
    )
    organization_role = _optional_string(payload.get("vibe_organization_role"))
    if organization_role not in ORGANIZATION_ROLES:
        organization_role = None
    return AuthorizationContext(
        instance_role=role,
        subject=_optional_string(payload.get("sub")),
        email=_optional_string(payload.get("email")),
        instance_id=_optional_string(payload.get("vibe_instance_id", payload.get("instance_id"))),
        instance_access_source=access_source,
        organization_id=_optional_string(payload.get("vibe_organization_id")),
        organization_member_id=_optional_string(payload.get("vibe_organization_member_id")),
        organization_role=organization_role,
        group_ids=group_ids,
        membership_version=_optional_string(payload.get("vibe_membership_version")),
        claims_issued_at=_optional_positive_int(
            payload.get("claims_issued_at", payload.get("iat"))
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
        r"^/api/show-pages$",
        r"^/api/show-pages/[^/]+/icon$",
    )
)

_EDITOR_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/api/agents$"),
        ("GET", r"^/api/agent-backends$"),
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
    )
)


def required_instance_role(method: str, path: str) -> str | None:
    """Return the minimum role for a remote HTTP request.

    Non-API page/static reads are handled by the authenticated shell. Unknown
    API routes deliberately default to owner so a newly added management route
    cannot accidentally become available to editors or viewers.
    """

    normalized_method = method.upper()
    if path.startswith("/show/"):
        return "viewer" if normalized_method in {"GET", "HEAD", "OPTIONS"} else "editor"
    if not path.startswith("/api/"):
        return None
    for rule_method, pattern in _EDITOR_HTTP_RULES:
        if normalized_method == rule_method and pattern.fullmatch(path):
            return "editor"
    if normalized_method in {"GET", "HEAD", "OPTIONS"}:
        if any(pattern.fullmatch(path) for pattern in _VIEWER_HTTP_RULES):
            return "viewer"
    return "owner"
