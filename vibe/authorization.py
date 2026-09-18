"""Central instance-role authorization for local and remote UI requests."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Any, Iterator, Mapping

from core.inbox_events import (
    DEFINITIONS_UPDATED_EVENT,
    RUNS_UPDATED_EVENT,
    VAULTS_UPDATED_EVENT,
)


INSTANCE_ROLES = frozenset({"owner", "member", "editor", "viewer"})
INSTANCE_ACCESS_SOURCES = frozenset(
    {
        "owner",
        "public_instance",
        "email",
        "email_domain",
        "organization_group",
    }
)
ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
INSTANCE_KINDS = frozenset({"personal", "organization"})
_ROLE_RANK = {"viewer": 1, "editor": 2, "member": 3, "owner": 4}


def recognized_instance_kind(value: object) -> str | None:
    """Return a recognized instance kind, or None for a genuine no-kind snapshot.

    A present-but-unrecognized value (corruption, a future release, a typo)
    is not a no-kind legacy snapshot. Callers that need fail-closed behavior
    must distinguish ``None`` (absent/legacy) from an unrecognized string
    via :func:`instance_kind_is_unsupported`.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned in INSTANCE_KINDS else None


def instance_kind_is_unsupported(value: object) -> bool:
    """True when a kind field is present but not a known Personal/Organization value."""

    if value is None:
        return False
    if not isinstance(value, str):
        return True
    return value.strip() not in {"", *INSTANCE_KINDS}


_RESOURCE_USE_MINIMUM_ROLES = {
    "agent": "editor",
    "skill": "editor",
    "vault_secret": "editor",
}

_VIEWER_WORKBENCH_EVENTS = frozenset(
    {
        "authorization.changed",
        "connected",
        "inbox.session.updated",
        "inbox.unread.changed",
        "message.new",
        "message.updated",
        "projects.changed",
        "session.activity",
        "session.status",
        "show.event",
        "turn.end",
        "turn.start",
        "workbench.events.bridge.status",
    }
)
#: Change notifications whose consumers refetch an instance-scoped endpoint
#: rather than read the frame. Harness and Vault are editor HTTP namespaces —
#: ``/api/harness/*`` applies no resource ACL and ``vault_service.list_requests``
#: / ``list_grants`` give an editor the full runtime read — so a notification
#: admitted at a higher role than the endpoint it announces leaves an editor's
#: Harness and Vault pages on a stream that never wakes them.
#:
#: One set, two owners: this is both the role tier below and the projection rule
#: in ``ui_server._workbench_event_payload_for_context``, which reduces the frame
#: to its bare signal for a recipient without runtime management. Admission is the
#: role; what a frame may CARRY is decided there, because publishers attach
#: identifiers — a run's session, a Vault secret name — that are not uniformly
#: inside a lower role's read scope, and no consumer reads them.
INSTANCE_SCOPED_REFETCH_EVENTS = frozenset(
    {
        DEFINITIONS_UPDATED_EVENT,
        RUNS_UPDATED_EVENT,
        VAULTS_UPDATED_EVENT,
    }
)
_EDITOR_WORKBENCH_EVENTS = frozenset({"queue.updated"}) | INSTANCE_SCOPED_REFETCH_EVENTS
#: Remote-access link quality describes the instance's own connection, which is
#: instance management, and stays member-only.
_PRIVILEGED_RUNTIME_WORKBENCH_EVENTS = frozenset({"remote_access.quality.changed"})

@dataclass(frozen=True)
class HttpAuthorizationPolicy:
    minimum_role: str | None


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
    instance_kind: str | None = None

    @property
    def is_instance_owner(self) -> bool:
        return self.instance_role == "owner"

    @property
    def is_personal_instance(self) -> bool:
        return self.instance_kind == "personal"

    @property
    def is_organization_instance(self) -> bool:
        return self.instance_kind == "organization"

    @property
    def is_active_organization_member(self) -> bool:
        return bool(
            self.instance_role in INSTANCE_ROLES
            and self.instance_access_source in INSTANCE_ACCESS_SOURCES
            and self.organization_id
            and self.organization_member_id
            and self.organization_role in ORGANIZATION_ROLES
        )

    def has_role(self, minimum_role: str) -> bool:
        return _ROLE_RANK.get(self.instance_role or "", 0) >= _ROLE_RANK[minimum_role]

    @property
    def can_read_instance(self) -> bool:
        return self.has_role("viewer")

    @property
    def can_chat(self) -> bool:
        return self.has_role("editor")

    @property
    def can_use_cloud_asr(self) -> bool:
        return self.has_role("editor")

    @property
    def can_manage_projects(self) -> bool:
        return self.has_role("member")

    @property
    def can_manage_agents(self) -> bool:
        return self.has_role("member")

    @property
    def can_manage_instance(self) -> bool:
        return self.has_role("member")

    @property
    def can_manage_access_members(self) -> bool:
        return self.has_role("owner")

    def can_use_resource(self, resource_kind: str) -> bool:
        """Return whether the Instance role may use this resource kind.

        Effective resource access is checked by the resource service: managers
        operate instance-wide, while lower roles retain their resource ACLs.
        Unknown kinds fail closed so adding a resource type never makes it
        editor-visible by accident.
        """

        minimum_role = _RESOURCE_USE_MINIMUM_ROLES.get(resource_kind)
        return minimum_role is not None and self.has_role(minimum_role)

    @property
    def can_use_terminal_files(self) -> bool:
        return self.has_role("editor")

    @property
    def can_use_terminal(self) -> bool:
        return self.has_role("editor")

    @property
    def can_use_files(self) -> bool:
        return self.has_role("editor")

    @property
    def can_use_system(self) -> bool:
        return self.has_role("member")

    def capability_projection(self) -> dict[str, bool]:
        return {
            "is_instance_owner": self.is_instance_owner,
            "can_read_instance": self.can_read_instance,
            "can_chat": self.can_chat,
            "can_manage_projects": self.can_manage_projects,
            "can_manage_agents": self.can_manage_agents,
            "can_manage_instance": self.can_manage_instance,
            "can_manage_access_members": self.can_manage_access_members,
            "can_use_agents": self.can_use_resource("agent"),
            "can_use_skills": self.can_use_resource("skill"),
            "can_use_vault_secrets": self.can_use_resource("vault_secret"),
            "can_use_show_pages": self.can_read_instance,
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
    raw_instance_kind = payload.get("vibe_instance_kind")
    if instance_kind_is_unsupported(raw_instance_kind):
        instance_kind = None
    else:
        instance_kind = recognized_instance_kind(raw_instance_kind)
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
        instance_kind=instance_kind,
    )


def instance_owner_context() -> AuthorizationContext:
    """Return the ordinary Owner identity used by standalone local entry points."""

    return AuthorizationContext(instance_role="owner")


_INVOCATION_AUTHORITY: ContextVar["AuthorizationContext | None"] = ContextVar(
    "avibe_invocation_authority", default=None
)


@contextmanager
def invocation_authority(context: "AuthorizationContext | None") -> Iterator[None]:
    """Activate the authority an invocation runs under, for its lifetime only.

    Set explicitly by an entry point that knows who invoked it — today the CLI
    dispatch boundary. It is the default the two role resolvers fall back to when
    a service call passes no context, which is why it is invocation-scoped and
    never global: the token is reset in ``finally``, so ``SystemExit``, an
    exception, and nested or sequential invocations all restore the previous
    authority. Activating ``None`` is meaningful — an explicit local invocation
    nested under a remote one masks it and restores it on exit.

    What bounds the scope is ownership, not the carrier. A ``ContextVar`` is
    copied into whatever this invocation itself starts — ``asyncio.to_thread``
    and a task created inside the block both copy the current context — which is
    what a command that awaits its own work needs, and is not a leak. The scope
    ends where the invocation does: one entry point sets it, around one dispatch,
    and resets it on the way out. A separate process, and a thread started
    outside this block, each begin from their own context, so an independently
    started controller, daemon or supervisor is never running inside a caller's.
    """

    token = _INVOCATION_AUTHORITY.set(context)
    try:
        yield
    finally:
        _INVOCATION_AUTHORITY.reset(token)


def current_invocation_authority() -> "AuthorizationContext | None":
    """Return the active invocation authority, or None outside one."""

    return _INVOCATION_AUTHORITY.get()


class InstanceAuthorizationError(PermissionError):
    def __init__(self, minimum_role: str):
        super().__init__(f"Instance role '{minimum_role}' is required")
        self.code = "instance_access_forbidden"
        self.minimum_role = minimum_role


def _in_http_request() -> bool:
    try:
        from vibe.ui_compat import has_request_context

        return bool(has_request_context())
    except Exception:
        return False


def default_authorization_context() -> AuthorizationContext:
    """The authority a service call that passes no context runs under.

    Inside an HTTP request this is the local Owner, exactly as it has always
    been, and the invocation carrier is not consulted: a signed HTTP caller
    authorizes itself by passing its context explicitly, so a call that passes
    none is not silently re-attributed to whoever the request belongs to. That
    boundary is retained deliberately, not repaired here.

    Outside a request, an active invocation authority is the caller; with no
    invocation either, this is a standalone local entry point and keeps Owner
    administration.
    """

    if _in_http_request():
        return instance_owner_context()
    invocation = _INVOCATION_AUTHORITY.get()
    return invocation if invocation is not None else instance_owner_context()


def require_instance_role(
    context: AuthorizationContext | Mapping[str, Any] | None,
    minimum_role: str,
) -> AuthorizationContext:
    """Authorize a service call; omitted context resolves to the invocation authority."""

    resolved = (
        default_authorization_context()
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
    if event_type in _PRIVILEGED_RUNTIME_WORKBENCH_EVENTS:
        return "member"
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
        r"^/api/version(?:/local)?$",
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
        r"^/api/permissions$",
        r"^/api/permissions/resources/[^/]+/[^/]+/access$",
        r"^/api/show-pages$",
        r"^/api/show-pages/[^/]+/access$",
        r"^/api/show-pages/[^/]+/icon$",
    )
)

# Advertised Editor/Viewer surfaces are admitted by namespace so a newly
# added Skills, Vault, Harness, Files, Dock, Terminal, or Web Push route
# inherits the same Instance role as the rest of that capability. Remaining
# unknown APIs fail closed to Owner; see _MEMBER_HTTP_RULES.
_EDITOR_HTTP_NAMESPACES = (
    "/api/skills",
    "/api/vault",
    "/api/files",
    "/api/dock",
    "/api/harness",
    "/api/terminal",
)
_VIEWER_HTTP_NAMESPACES = (
    "/api/memory",
    "/api/web-push",
)
_VIEWER_HTTP_MUTATION_RULES = (
    ("POST", re.compile(r"^/api/sessions/[^/]+/mark-read$")),
    ("DELETE", re.compile(r"^/api/terminal/[^/]+$")),
)

# Pairing identity, member set, and ownership stay Owner-only. Writes under
# /api/remote-access default to Owner. Explicit tunnel operations below preserve
# pairing identity; the settings writer accepts only transport/recovery fields.
_REMOTE_ACCESS_HTTP_NAMESPACE = "/api/remote-access"
_REMOTE_ACCESS_MEMBER_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/api/remote-access/status$"),
        ("GET", r"^/api/remote-access/network-interfaces$"),
        ("POST", r"^/api/remote-access/optimize-route$"),
        ("POST", r"^/api/remote-access/diagnostics$"),
        ("POST", r"^/api/remote-access/(?:start|stop|settings)$"),
    )
)

# Instance management follows Member capabilities, using an explicit method/path
# inventory. Unknown APIs remain Owner-only. Access-member/role administration,
# pairing identity, Project/Resource ACL changes, WeChat's combined login/binding,
# and bulk Agent ownership migration deliberately remain outside this table.
# Mixed config/scope writers also preserve access policy at their locked commit.
_MEMBER_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        # Agent CRUD and instance-wide default selection.
        ("POST", r"^/api/agents$"),
        ("PATCH", r"^/api/agents/[^/]+$"),
        ("DELETE", r"^/api/agents/[^/]+$"),
        ("POST", r"^/api/agents/import$"),
        ("POST", r"^/api/agents/default$"),
        # Model Hub management. Keep this method/path allow-list aligned with
        # its registered API contract; unknown siblings remain Owner-only.
        # GET /agents/<backend>/models stays in the Editor model-picker rules.
        ("GET", r"^/api/models/sources$"),
        ("POST", r"^/api/models/sources$"),
        ("POST", r"^/api/models/sources/observe$"),
        ("PATCH", r"^/api/models/sources/[^/]+$"),
        ("DELETE", r"^/api/models/sources/[^/]+$"),
        ("PUT", r"^/api/models/sources/[^/]+/credential$"),
        ("POST", r"^/api/models/sources/[^/]+/(?:reauth|refresh|probe)$"),
        ("POST", r"^/api/models/sources/[^/]+/models$"),
        # Model IDs are path parameters and may contain vendor/model slashes.
        ("PATCH", r"^/api/models/sources/[^/]+/models/.+$"),
        ("DELETE", r"^/api/models/sources/[^/]+/models/.+$"),
        ("GET", r"^/api/models/agents$"),
        ("GET", r"^/api/models/agents/[^/]+/chains$"),
        ("GET", r"^/api/models/agents/[^/]+/chain$"),
        ("PUT", r"^/api/models/agents/[^/]+/chain$"),
        ("DELETE", r"^/api/models/agents/[^/]+/chain$"),
        ("POST", r"^/api/models/agents/[^/]+/chain/preview$"),
        ("GET", r"^/api/models/agents/[^/]+/sources$"),
        ("PUT", r"^/api/models/agents/[^/]+/sources$"),
        ("GET", r"^/api/models/agents/[^/]+/models/candidates$"),
        ("PUT", r"^/api/models/agents/[^/]+/models$"),
        ("GET", r"^/api/models/catalog/models-dev$"),
        ("PATCH", r"^/api/models/agents/[^/]+/mode$"),
        ("POST", r"^/api/models/agents/[^/]+/chains/reorder$"),
        ("POST", r"^/api/models/agents/[^/]+/probe$"),
        ("GET", r"^/api/models/agents/[^/]+/provenance$"),
        ("GET", r"^/api/models/(?:events|usage)$"),
        ("POST", r"^/api/models/oauth/(?:start|submit|cancel)$"),
        ("GET", r"^/api/models/oauth/status/[^/]+$"),
        ("POST", r"^/api/models/migration/(?:scan|apply)$"),
        ("GET", r"^/api/models/turns/[^/]+/provenance$"),
        ("GET", r"^/api/models/runtime/status$"),
        ("POST", r"^/api/models/runtime/(?:install|start|stop)$"),
        # can_manage_agents: instance-wide prompt text, not a credential.
        ("GET", r"^/api/global-prompts$"),
        ("PUT", r"^/api/global-prompts$"),
        # can_manage_projects. Project ACL lives under /api/permissions and is
        # Owner; agents-md is project content.
        ("POST", r"^/api/projects$"),
        ("PUT", r"^/api/projects/order$"),
        ("PATCH", r"^/api/projects/[^/]+$"),
        ("DELETE", r"^/api/projects/[^/]+$"),
        ("GET", r"^/api/projects/[^/]+/agents-md$"),
        ("PUT", r"^/api/projects/[^/]+/agents-md$"),
        # General instance settings; mixed writes retain access-administration guards.
        ("GET", r"^/api/doctor$"),
        ("PUT", r"^/api/workbench/prefs$"),
        ("GET", r"^/api/cli/detect$"),
        ("GET", r"^/api/slack/manifest$"),
        ("POST", r"^/api/control$"),
        ("POST", r"^/api/ui/reload$"),
        ("POST", r"^/api/settings$"),
        ("POST", r"^/api/settings/thread$"),
        ("DELETE", r"^/api/settings/thread$"),
        ("POST", r"^/api/slack/auth_test$"),
        ("POST", r"^/api/slack/channels$"),
        ("POST", r"^/api/discord/auth_test$"),
        ("POST", r"^/api/discord/guilds$"),
        ("POST", r"^/api/discord/channels$"),
        ("POST", r"^/api/channels/delete$"),
        ("POST", r"^/api/telegram/auth_test$"),
        ("POST", r"^/api/telegram/chats$"),
        ("POST", r"^/api/lark/auth_test$"),
        ("POST", r"^/api/lark/chats$"),
        ("POST", r"^/api/lark/temp_ws/start$"),
        ("POST", r"^/api/lark/temp_ws/stop$"),
        ("POST", r"^/api/doctor$"),
        ("POST", r"^/api/logs$"),
        ("POST", r"^/api/opencode/options$"),
        ("POST", r"^/api/upgrade$"),
        ("POST", r"^/api/opencode/setup-permission$"),
        ("GET", r"^/api/opencode/permission-status$"),
        ("GET", r"^/api/claude/agents$"),
        ("GET", r"^/api/codex/agents$"),
        ("POST", r"^/api/agent/[^/]+/install$"),
        ("GET", r"^/api/agent/[^/]+/install/[^/]+$"),
        ("GET", r"^/api/backend/[^/]+/runtime$"),
        ("POST", r"^/api/backend/[^/]+/restart$"),
        ("GET", r"^/api/dependencies$"),
        ("POST", r"^/api/dependencies/[^/]+/install$"),
        ("GET", r"^/api/dependencies/[^/]+/install/[^/]+$"),
        ("GET", r"^/api/backend/codex/auth$"),
        ("POST", r"^/api/backend/codex/auth$"),
        ("GET", r"^/api/backend/claude/auth$"),
        ("POST", r"^/api/backend/claude/auth$"),
        ("POST", r"^/api/backend/[^/]+/auth/oauth/start$"),
        ("GET", r"^/api/backend/[^/]+/auth/oauth/status/[^/]+$"),
        ("POST", r"^/api/backend/[^/]+/auth/oauth/submit-code$"),
        ("POST", r"^/api/backend/[^/]+/auth/oauth/cancel$"),
        ("POST", r"^/api/backend/[^/]+/auth/oauth/remove$"),
        ("POST", r"^/api/backend/claude/auth/oauth/credentials/remove$"),
        ("POST", r"^/api/backend/[^/]+/auth/api-key/remove$"),
        ("POST", r"^/api/backend/[^/]+/auth/test$"),
        ("GET", r"^/api/backend/opencode/providers$"),
        ("POST", r"^/api/backend/opencode/custom-provider$"),
        ("DELETE", r"^/api/backend/opencode/custom-provider/[^/]+$"),
        ("POST", r"^/api/backend/opencode/provider/[^/]+/auth/oauth/start$"),
        ("POST", r"^/api/backend/opencode/provider/[^/]+/auth$"),
        ("DELETE", r"^/api/backend/opencode/provider/[^/]+/auth$"),
        ("POST", r"^/api/backend/opencode/provider/[^/]+/test$"),
        ("POST", r"^/api/backend/opencode/default-provider$"),
        ("POST", r"^/api/backend/opencode/provider/[^/]+/models$"),
        ("DELETE", r"^/api/backend/opencode/provider/[^/]+/models/.+$"),
        ("POST", r"^/api/browse$"),
        ("POST", r"^/api/browse/mkdir$"),
        ("POST", r"^/api/users$"),
        # Read-only instance state.
        ("GET", r"^/api/settings$"),
        ("GET", r"^/api/users$"),
    )
)

_EDITOR_HTTP_RULES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"^/api/agents$"),
        ("GET", r"^/api/agents/[^/]+$"),
        ("GET", r"^/api/agents-graph$"),
        ("GET", r"^/api/agent-backends$"),
        # Read-only model catalogs. Chat's route picker is an editor surface and
        # the Agents detail panel is a member one, so the catalog they share is
        # editor-tier and member inherits it. Both routes are snapshot reads of a
        # shared model catalog: no provider configuration, no backend contact.
        # Listed one route at a time rather than as a namespace, so neither
        # `/api/claude/*` nor `/api/codex/*` grows an editor-visible mutation by
        # accident.
        #
        # OpenCode's native live catalog has no editor-visible endpoint. The only
        # live endpoint is `/api/backend/opencode/providers`, a Settings surface --
        # base URLs, masked API keys, active auth type, tool-call permission
        # state -- and reaching it runs `ensure_running()`, which can install a
        # plugin, restart, or launch the daemon. This stays Member; the Editor model
        # picker treats the refusal as "no catalog" instead.
        ("GET", r"^/api/models/agents/[^/]+/models$"),
        ("GET", r"^/api/claude/models$"),
        ("GET", r"^/api/codex/models$"),
        ("GET", r"^/api/running-agents$"),
        ("POST", r"^/api/running-agents/end$"),
        # Files favorites live under /api/browse, not /api/files. Admit only this
        # shared dependency — POST /api/browse and /mkdir are Member project-picker
        # routes.
        ("GET", r"^/api/browse/favorites$"),
        ("GET", r"^/api/cloud/token$"),
        ("GET", r"^/api/asr/status$"),
        ("GET", r"^/api/sessions/[^/]+/(?:archive-preview|queue|draft)$"),
        ("POST", r"^/api/sessions$"),
        ("POST", r"^/api/sessions/[^/]+/cli-activity$"),
        ("POST", r"^/api/sessions/[^/]+/fork$"),
        ("PATCH", r"^/api/sessions/[^/]+$"),
        ("DELETE", r"^/api/sessions/[^/]+$"),
        ("POST", r"^/api/sessions/[^/]+/(?:messages|attachments|cancel)$"),
        ("DELETE", r"^/api/sessions/[^/]+/queue/[^/]+$"),
        ("POST", r"^/api/sessions/[^/]+/queue/[^/]+/send-now$"),
        ("PUT", r"^/api/sessions/[^/]+/draft$"),
        ("POST", r"^/api/asr/transcribe$"),
        ("POST", r"^/api/asr/telemetry$"),
        ("POST", r"^/api/config$"),
        ("POST", r"^/api/show/sessions/[^/]+/events$"),
        ("POST", r"^/api/show/sessions/[^/]+/prewarm$"),
        ("GET", r"^/api/show-pages/[^/]+$"),
        ("POST", r"^/api/show-pages/[^/]+/icon$"),
        ("POST", r"^/api/show-pages/[^/]+/(?:ensure|availability)$"),
        ("POST", r"^/api/show-pages/[^/]+/access-settings/(?:read|apply)$"),
    )
)

def _http_rule_matches(
    method: str,
    path: str,
    rules: tuple[tuple[str, re.Pattern[str]], ...],
) -> bool:
    return any(rule_method == method and pattern.fullmatch(path) for rule_method, pattern in rules)


def _path_in_namespaces(path: str, namespaces: tuple[str, ...]) -> bool:
    return any(path == namespace or path.startswith(f"{namespace}/") for namespace in namespaces)


def http_authorization_policy(
    method: str,
    path: str,
) -> HttpAuthorizationPolicy:
    """Return the minimum Instance role for one HTTP request."""

    normalized_method = method.upper()
    if path.startswith("/show/"):
        return HttpAuthorizationPolicy("viewer")
    if path == "/status":
        return HttpAuthorizationPolicy(None)
    if not path.startswith("/api/"):
        return HttpAuthorizationPolicy(None)
    if _path_in_namespaces(path, _VIEWER_HTTP_NAMESPACES):
        return HttpAuthorizationPolicy("viewer")
    if any(
        rule_method == normalized_method and pattern.fullmatch(path)
        for rule_method, pattern in _VIEWER_HTTP_MUTATION_RULES
    ):
        return HttpAuthorizationPolicy("viewer")
    if _path_in_namespaces(path, _EDITOR_HTTP_NAMESPACES):
        return HttpAuthorizationPolicy("editor")
    if _path_in_namespaces(path, (_REMOTE_ACCESS_HTTP_NAMESPACE,)):
        if _http_rule_matches(normalized_method, path, _REMOTE_ACCESS_MEMBER_HTTP_RULES):
            return HttpAuthorizationPolicy("member")
        return HttpAuthorizationPolicy("owner")
    if _http_rule_matches(normalized_method, path, _MEMBER_HTTP_RULES):
        return HttpAuthorizationPolicy("member")

    # Default deny: an unclassified /api route is Owner-only, so adding the
    # member rank cannot widen a route nobody listed. See _MEMBER_HTTP_RULES.
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

    return HttpAuthorizationPolicy(minimum_role)


def required_instance_role(method: str, path: str) -> str | None:
    """Return the minimum role for a remote HTTP request.

    Non-API page/static reads are handled by the authenticated shell. Unknown
    API routes deliberately default to owner, so a newly added management route
    is never reachable by a role that predates it. The member rank widens only
    the routes listed in ``_MEMBER_HTTP_RULES`` plus the explicit tunnel operations under
    ``/api/remote-access``. Access administration, ACL writes, pairing, WeChat
    login/binding and bulk Agent ownership migration remain Owner by default.
    """

    return http_authorization_policy(method, path).minimum_role
