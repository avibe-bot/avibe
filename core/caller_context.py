"""Avibe caller-context contract for Agent-initiated Harness calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from typing import Any, Mapping, Optional

AVIBE_SESSION_ID_ENV = "AVIBE_SESSION_ID"
AVIBE_RUN_ID_ENV = "AVIBE_RUN_ID"
AVIBE_CALLER_SOURCE_ENV = "AVIBE_CALLER_SOURCE"
AVIBE_CALLER_BACKEND_ENV = "AVIBE_CALLER_BACKEND"
AVIBE_NATIVE_SESSION_ID_ENV = "AVIBE_NATIVE_SESSION_ID"
#: The CREATION ORIGIN half of the contract. Everything below travels the same
#: subprocess hop as the ids above, because an IM-created definition is created by an
#: Agent run executing ``vibe task add`` — the env is the only channel between the
#: conversation that asked for the definition and the row that records who asked.
AVIBE_CALLER_PLATFORM_ENV = "AVIBE_CALLER_PLATFORM"
AVIBE_CALLER_USER_ID_ENV = "AVIBE_CALLER_USER_ID"
AVIBE_CALLER_CHANNEL_ID_ENV = "AVIBE_CALLER_CHANNEL_ID"
AVIBE_CALLER_SESSION_KEY_ENV = "AVIBE_CALLER_SESSION_KEY"
AVIBE_CALLER_MESSAGE_ID_ENV = "AVIBE_CALLER_MESSAGE_ID"
AVIBE_CALLER_WORKSPACE_ID_ENV = "AVIBE_CALLER_WORKSPACE_ID"
AVIBE_CALLER_REMOTE_ENV = "AVIBE_CALLER_REMOTE"
AVIBE_CALLER_RESOURCE_CONTEXT_ENV = "AVIBE_CALLER_RESOURCE_CONTEXT"

CALLER_CONTEXT_ENV_NAMES = frozenset(
    {
        AVIBE_SESSION_ID_ENV,
        AVIBE_RUN_ID_ENV,
        AVIBE_CALLER_SOURCE_ENV,
        AVIBE_CALLER_BACKEND_ENV,
        AVIBE_NATIVE_SESSION_ID_ENV,
        AVIBE_CALLER_PLATFORM_ENV,
        AVIBE_CALLER_USER_ID_ENV,
        AVIBE_CALLER_CHANNEL_ID_ENV,
        AVIBE_CALLER_SESSION_KEY_ENV,
        AVIBE_CALLER_MESSAGE_ID_ENV,
        AVIBE_CALLER_WORKSPACE_ID_ENV,
        AVIBE_CALLER_REMOTE_ENV,
        AVIBE_CALLER_RESOURCE_CONTEXT_ENV,
    }
)

_RESOURCE_USER_CONTEXT_METADATA_KEY = "resource_user_context"


@dataclass(frozen=True)
class CallerContext:
    """Caller identity resolved from Avibe-owned execution context.

    The first five fields answer "which Avibe session/run made this call". The
    ORIGIN fields below answer "where in the user's world was it made", which is a
    different question and has to be captured separately: a failure notice is
    context-free by construction (it may be delivered to an owner DM or to the
    workspace inbox hours later), so it can only name the channel and thread the
    definition came from if the creating turn wrote them down.

    ``session_key`` and ``channel_id`` are BOTH kept, and are not redundant:

    * ``session_key`` is the ADDRESS — the
      ``<platform>::<channel|user>::<id>[::thread::<ts>]`` grammar
      ``parse_session_key`` speaks, and the one a notice rung can be delivered to;
    * ``channel_id`` is the LOCATION — what a message permalink needs. For a DM the
      session key's scope id is the USER id, and a permalink built from it would be
      fabricated (Slack DM permalinks are keyed by the ``D…`` conversation).

    ``workspace_id`` is one generic field rather than one per platform: it holds a
    Slack team id or a Discord guild id, both of which are the enclosing tenant a
    deep link must name.
    """

    session_id: str
    run_id: Optional[str] = None
    source: Optional[str] = None
    backend: Optional[str] = None
    native_session_id: Optional[str] = None
    platform: Optional[str] = None
    user_id: Optional[str] = None
    channel_id: Optional[str] = None
    session_key: Optional[str] = None
    message_id: Optional[str] = None
    workspace_id: Optional[str] = None
    is_remote: bool = False
    resource_user_context: Optional[Mapping[str, Any]] = None

    def session_stable(self) -> "CallerContext":
        """This context minus every origin field that changes from turn to turn.

        For a backend whose caller env is written ONCE — the Claude SDK client is spawned
        per session and its environment cannot be refreshed afterwards — the per-message
        fields are not merely useless, they are dangerous in two different ways:

        * ``message_id`` changes on every message, so keeping it in the comparison that
          decides whether a cached client is still valid would tear down and respawn the
          Agent on EVERY turn;
        * ``user_id`` changes with the author, and a shared channel session is
          deliberately shared across participants (pinned by
          ``test_session_handler_reuses_cached_claude_client_when_system_prompt_is_unchanged``,
          which also asserts the per-user id never reaches the system prompt). A baked-in
          author id would attribute a definition created by one participant to whoever
          happened to speak first — and the owner-DM rung would then notify the wrong
          person, which is worse than not notifying anybody.

        What survives is what the SESSION owns: platform, channel, session key,
        workspace. A trusted remote Workbench authorization snapshot also survives:
        unlike an IM author id, it is required to authorize any CLI resource write.
        When that snapshot changes the Claude cache comparison deliberately recreates
        the client, so one remote user's ACL can never be reused for another. Thus a
        Claude-created definition still names its conversation and remote authority,
        while ordinary IM sessions get no owner-DM rung or deep link. Codex and
        OpenCode rewrite their caller env per turn and keep the full origin.

        A DM is the case where nothing is lost: its session key is
        ``<platform>::user::<id>``, so the person is carried by the scope itself.
        """

        if not self.user_id and not self.message_id:
            return self
        return replace(self, user_id=None, message_id=None)

    def to_env(self) -> dict[str, str]:
        env = {AVIBE_SESSION_ID_ENV: self.session_id}
        if self.run_id:
            env[AVIBE_RUN_ID_ENV] = self.run_id
        if self.source:
            env[AVIBE_CALLER_SOURCE_ENV] = self.source
        if self.backend:
            env[AVIBE_CALLER_BACKEND_ENV] = self.backend
        if self.native_session_id:
            env[AVIBE_NATIVE_SESSION_ID_ENV] = self.native_session_id
        if self.platform:
            env[AVIBE_CALLER_PLATFORM_ENV] = self.platform
        if self.user_id:
            env[AVIBE_CALLER_USER_ID_ENV] = self.user_id
        if self.channel_id:
            env[AVIBE_CALLER_CHANNEL_ID_ENV] = self.channel_id
        if self.session_key:
            env[AVIBE_CALLER_SESSION_KEY_ENV] = self.session_key
        if self.message_id:
            env[AVIBE_CALLER_MESSAGE_ID_ENV] = self.message_id
        if self.workspace_id:
            env[AVIBE_CALLER_WORKSPACE_ID_ENV] = self.workspace_id
        if self.is_remote:
            env[AVIBE_CALLER_REMOTE_ENV] = "1"
            if self.resource_user_context is not None:
                env[AVIBE_CALLER_RESOURCE_CONTEXT_ENV] = json.dumps(
                    dict(self.resource_user_context),
                    separators=(",", ":"),
                    sort_keys=True,
                )
        return env

    def to_metadata(self) -> dict[str, str]:
        """The ``created_by.caller`` shape persisted with a definition.

        The origin key NAMES are load-bearing: ``core/scheduled_tasks.py``'s failure
        ladder has always read ``caller["session_key"]`` / ``caller["scope_id"]``
        (rung 3) and ``caller["platform"]`` / ``caller["user_id"]`` (rung 4, the owner
        DM) — fields nothing had ever written, so both rungs were dormant. Writing
        them under any other name would leave them dormant.
        """

        metadata = {"session_id": self.session_id}
        if self.run_id:
            metadata["run_id"] = self.run_id
        if self.source:
            metadata["source"] = self.source
        if self.backend:
            metadata["backend"] = self.backend
        if self.native_session_id:
            metadata["native_session_id"] = self.native_session_id
        if self.platform:
            metadata["platform"] = self.platform
        if self.user_id:
            metadata["user_id"] = self.user_id
        if self.channel_id:
            metadata["channel_id"] = self.channel_id
        if self.session_key:
            metadata["session_key"] = self.session_key
            # ``scope_id`` only when it says something the session key does not: rung 3
            # reads ``session_key or scope_id``, so a copy of the same value would be
            # dead weight. A THREAD key's parent scope is the exception — it is the
            # coarser address a consumer that cannot deliver into a thread still wants,
            # and ``parse_scope_id`` cannot express the five-part form.
            scope_id = _scope_id_from_session_key(self.session_key)
            if scope_id:
                metadata["scope_id"] = scope_id
        if self.message_id:
            metadata["message_id"] = self.message_id
        if self.workspace_id:
            metadata["workspace_id"] = self.workspace_id
        return metadata


def _clean(value: object) -> str:
    return str(value or "").strip()


def _resource_context_from_env(source: Mapping[str, str]) -> Optional[dict[str, Any]]:
    raw = _clean(source.get(AVIBE_CALLER_RESOURCE_CONTEXT_ENV))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def caller_resource_user_context(context: Optional[CallerContext]) -> Optional[Mapping[str, Any]]:
    """Return explicit remote ACL context for a CLI write.

    ``None`` is reserved for a genuinely local caller. A remote Agent invocation
    with missing or malformed authorization provenance returns an empty mapping,
    which resource services parse as an anonymous remote context and reject.
    """

    if context is None or not context.is_remote:
        return None
    return dict(context.resource_user_context or {})


def validated_caller_env_snapshot(source: object) -> dict[str, str]:
    """Return only caller-context fields from a persisted internal snapshot."""

    if not isinstance(source, Mapping):
        return {}
    return {
        str(key): value
        for key, value in source.items()
        if key in CALLER_CONTEXT_ENV_NAMES and isinstance(value, str) and value
    }


def _scope_id_from_session_key(session_key: str) -> Optional[str]:
    parts = _clean(session_key).split("::")
    if len(parts) != 5:
        return None
    return "::".join(parts[:3])


def _origin_thread_id(
    context_platform: str,
    payload: Mapping[str, object] | None,
    thread_id: object,
    *,
    is_dm: bool,
) -> Optional[str]:
    """The thread segment of the origin session key.

    Mirrors ``build_session_key_for_context(..., include_thread=True)`` —
    ``resolve_context_thread_id(context) or context.thread_id`` — which is pinned by
    ``test_the_captured_origin_session_key_matches_the_canonical_builder`` rather than
    imported, so this module keeps no dependency on ``modules.im`` and stays cheap
    enough for the CLI to import.

    *context_platform* is deliberately the platform WITHOUT the caller's fallback, which
    is the one asymmetry in that mirror: ``build_session_key_for_context`` passes its
    ``fallback_platform`` to ``resolve_context_platform`` for the key's prefix but calls
    ``resolve_context_thread_id`` with no fallback at all. Applying the fallback here
    would canonicalise a Telegram forum thread the canonical builder would have left
    off, i.e. address rung (3) at a conversation the rest of the system does not believe
    in. The asymmetry is invisible in production — the Telegram adapter always sets
    ``context.platform``, and only Slack leaves it unset — but it is the exact case
    ``test_the_captured_origin_session_key_matches_the_canonical_builder`` caught.
    """

    canonical: Optional[str] = None
    if context_platform == "telegram" and not is_dm:
        data = payload or {}
        if _clean(thread_id):
            canonical = _clean(thread_id)
        elif data.get("is_forum") or data.get("is_topic_message"):
            canonical = "1"
    return canonical or (_clean(thread_id) or None)


def _origin_session_key(
    platform: str,
    payload: Mapping[str, object] | None,
    *,
    context_platform: str,
    user_id: str,
    channel_id: str,
    thread_id: object,
) -> Optional[str]:
    if not platform:
        return None
    is_dm = bool((payload or {}).get("is_dm", False))
    scope_type = "user" if is_dm else "channel"
    scope_id = user_id if is_dm else channel_id
    if not scope_id:
        return None
    base = f"{platform}::{scope_type}::{scope_id}"
    thread = _origin_thread_id(context_platform, payload, thread_id, is_dm=is_dm)
    return f"{base}::thread::{thread}" if thread else base


def _origin_workspace_id(platform: str, payload: Mapping[str, object] | None) -> Optional[str]:
    """The enclosing tenant, read from whatever the adapter already put on the payload.

    Defensive by design, and deliberately read-only: no ``modules/im`` adapter is
    changed to feed this. Slack event payloads carry ``team_id``; a Discord payload
    carries either the raw ``discord.Message`` or ``discord.Interaction``; both expose
    ``guild``, which is ``None`` in a DM. Every other platform has no tenant id here,
    which is a MISSING id, not an empty one — the deep-link builder must refuse rather
    than invent one.
    """

    if not payload:
        return None
    if platform == "slack":
        return _clean(payload.get("team_id")) or None
    if platform == "discord":
        origin = payload.get("message")
        if origin is None:
            origin = payload.get("interaction")
        guild = getattr(origin, "guild", None)
        return _clean(getattr(guild, "id", None)) or None
    return None


def caller_context_from_env(env: Mapping[str, str] | None = None) -> Optional[CallerContext]:
    """Resolve caller context from process env.

    The raw session id is authoritative only when Avibe injected it into an
    Agent subprocess. If it is absent, callers should fail or require explicit
    flags instead of guessing from native backend ids.
    """

    source = env if env is not None else os.environ
    session_id = _clean(source.get(AVIBE_SESSION_ID_ENV))
    if not session_id:
        return None
    return CallerContext(
        session_id=session_id,
        run_id=_clean(source.get(AVIBE_RUN_ID_ENV)) or None,
        source=_clean(source.get(AVIBE_CALLER_SOURCE_ENV)) or None,
        backend=_clean(source.get(AVIBE_CALLER_BACKEND_ENV)) or None,
        native_session_id=_clean(source.get(AVIBE_NATIVE_SESSION_ID_ENV)) or None,
        platform=_clean(source.get(AVIBE_CALLER_PLATFORM_ENV)) or None,
        user_id=_clean(source.get(AVIBE_CALLER_USER_ID_ENV)) or None,
        channel_id=_clean(source.get(AVIBE_CALLER_CHANNEL_ID_ENV)) or None,
        session_key=_clean(source.get(AVIBE_CALLER_SESSION_KEY_ENV)) or None,
        message_id=_clean(source.get(AVIBE_CALLER_MESSAGE_ID_ENV)) or None,
        workspace_id=_clean(source.get(AVIBE_CALLER_WORKSPACE_ID_ENV)) or None,
        is_remote=_clean(source.get(AVIBE_CALLER_REMOTE_ENV)).lower() in {"1", "true"},
        resource_user_context=_resource_context_from_env(source),
    )


def caller_context_from_platform_payload(
    payload: Mapping[str, object] | None,
    *,
    message: object | None = None,
    fallback_platform: object | None = None,
) -> Optional[CallerContext]:
    """Resolve caller context from an Avibe message/turn payload.

    *message* is the turn's typed ``MessageContext`` (duck-typed here so this module
    stays importable by the CLI without pulling in ``modules.im``). Without it the
    context carries identity only and NO origin — which is exactly right for a caller
    that has no conversation behind it, and is why every existing caller keeps its
    previous env and metadata byte for byte.
    """

    if not payload and message is None:
        return None
    payload = payload or {}
    target = payload.get("agent_session_target")
    session_id = ""
    backend = ""
    native_session_id = ""
    if isinstance(target, Mapping):
        session_id = _clean(target.get("id"))
        backend = _clean(target.get("agent_backend") or target.get("backend"))
        native_session_id = _clean(target.get("native_session_id"))
    session_id = session_id or _clean(payload.get("agent_session_id"))
    if not session_id:
        return None
    run_id = _clean(payload.get("task_execution_id"))
    source_kind = _clean(payload.get("source_kind"))
    trigger_kind = _clean(payload.get("task_trigger_kind"))
    source = source_kind if source_kind == "callback" else trigger_kind or source_kind or "agent_turn"
    backend = backend or _clean(payload.get("vibe_agent_backend"))

    platform = ""
    user_id = ""
    channel_id = ""
    message_id = ""
    session_key: Optional[str] = None
    workspace_id: Optional[str] = None
    if message is not None:
        context_platform = _clean(getattr(message, "platform", None)) or _clean(payload.get("platform"))
        platform = context_platform or _clean(fallback_platform)
        user_id = _clean(getattr(message, "user_id", None))
        channel_id = _clean(getattr(message, "channel_id", None))
        message_id = _clean(getattr(message, "message_id", None))
        session_key = _origin_session_key(
            platform,
            payload,
            context_platform=context_platform,
            user_id=user_id,
            channel_id=channel_id,
            thread_id=getattr(message, "thread_id", None),
        )
        workspace_id = _origin_workspace_id(platform, payload)

    is_remote = platform == "avibe" and user_id.startswith("remote:")
    resource_user_context: Optional[dict[str, Any]] = None
    message_metadata = payload.get("message_metadata")
    if is_remote and isinstance(message_metadata, Mapping):
        raw_resource_context = message_metadata.get(_RESOURCE_USER_CONTEXT_METADATA_KEY)
        if isinstance(raw_resource_context, Mapping):
            subject = _clean(raw_resource_context.get("sub"))
            if subject and user_id == f"remote:{subject}":
                resource_user_context = dict(raw_resource_context)

    return CallerContext(
        session_id=session_id,
        run_id=run_id or None,
        source=source or None,
        backend=backend or None,
        native_session_id=native_session_id or None,
        platform=platform or None,
        user_id=user_id or None,
        channel_id=channel_id or None,
        session_key=session_key,
        message_id=message_id or None,
        workspace_id=workspace_id,
        is_remote=is_remote,
        resource_user_context=resource_user_context,
    )


def caller_env_for_platform_payload(
    payload: Mapping[str, object] | None,
    *,
    message: object | None = None,
    fallback_platform: object | None = None,
    session_stable_only: bool = False,
) -> dict[str, str]:
    """The caller env for an Agent subprocess.

    Set *session_stable_only* when the env is written once for the whole session and
    cannot be refreshed per turn — see ``CallerContext.session_stable`` for what that
    drops and why keeping it would be worse than losing it.
    """

    context = caller_context_from_platform_payload(
        payload,
        message=message,
        fallback_platform=fallback_platform,
    )
    if context is None:
        return {}
    if session_stable_only:
        context = context.session_stable()
    return context.to_env()
