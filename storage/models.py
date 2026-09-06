from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DDL,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)

from storage.delivery_states import DELIVERY_STATES
from vibe.message_types import build_partial_index_predicate

metadata = MetaData()


def _sql_string_set(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


state_meta = Table(
    "state_meta",
    metadata,
    Column("key", String, primary_key=True),
    Column("value_json", Text, nullable=False),
    Column("updated_at", String, nullable=False),
)

agents = Table(
    "agents",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("normalized_name", String, nullable=False),
    Column("description", Text, nullable=True),
    Column("backend", String, nullable=False),
    Column("model", String, nullable=True),
    Column("reasoning_effort", String, nullable=True),
    Column("system_prompt", Text, nullable=True),
    Column("enabled", Integer, nullable=False),
    Column("source", String, nullable=False),
    Column("source_ref", Text, nullable=True),
    Column("metadata_json", Text, nullable=False),
    Column("archived_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("normalized_name", name="uq_agents_normalized_name"),
    Index("ix_agents_backend", "backend"),
    Index("ix_agents_updated", "updated_at"),
)

scopes = Table(
    "scopes",
    metadata,
    Column("id", String, primary_key=True),
    Column("platform", String, nullable=False),
    Column("scope_type", String, nullable=False),
    Column("native_id", String, nullable=False),
    Column("parent_scope_id", String, ForeignKey("scopes.id", ondelete="SET NULL"), nullable=True),
    Column("display_name", Text, nullable=True),
    Column("native_type", String, nullable=True),
    Column("is_private", Integer, nullable=False),
    Column("supports_threads", Integer, nullable=False),
    Column("metadata_json", Text, nullable=False),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("platform", "scope_type", "native_id", name="uq_scopes_platform_type_native"),
    Index("ix_scopes_platform_type", "platform", "scope_type"),
    Index("ix_scopes_parent", "parent_scope_id"),
)

scope_settings = Table(
    "scope_settings",
    metadata,
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), primary_key=True),
    Column("enabled", Integer, nullable=False),
    Column("role", String, nullable=True),
    Column("workdir", Text, nullable=True),
    Column("agent_name", String, nullable=True),
    Column("agent_backend", String, nullable=True),
    Column("agent_variant", String, nullable=True),
    Column("model", String, nullable=True),
    Column("reasoning_effort", String, nullable=True),
    Column("require_mention", Integer, nullable=True),
    Column("settings_version", Integer, nullable=False),
    Column("settings_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_scope_settings_role", "role"),
    Index("ix_scope_settings_workdir", "workdir"),
    Index("ix_scope_settings_backend_model", "agent_backend", "model"),
)

project_access_policies = Table(
    "project_access_policies",
    metadata,
    Column("project_id", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False),
    Column("organization_id", String, nullable=True),
    Column("mode", String, nullable=False, server_default="inherit"),
    Column("policy_revision", Integer, nullable=False, server_default="0"),
    Column("last_applied_control_plane_revision", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("scope_id", name="uq_project_access_policies_scope"),
    CheckConstraint("mode in ('inherit', 'restricted')", name="ck_project_access_policies_mode"),
    CheckConstraint("policy_revision >= 0", name="ck_project_access_policies_revision"),
    CheckConstraint(
        "last_applied_control_plane_revision >= 0",
        name="ck_project_access_policies_control_revision",
    ),
    Index("ix_project_access_policies_organization", "organization_id"),
)

project_access_bindings = Table(
    "project_access_bindings",
    metadata,
    Column(
        "project_id",
        String,
        ForeignKey("project_access_policies.project_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("principal_kind", String, primary_key=True),
    Column("principal_value", String, primary_key=True),
    Column("access_role", String, nullable=False),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "principal_kind in ('email', 'email_domain', 'organization_group')",
        name="ck_project_access_bindings_kind",
    ),
    CheckConstraint("access_role in ('editor', 'viewer')", name="ck_project_access_bindings_role"),
    Index(
        "ix_project_access_bindings_principal",
        "principal_kind",
        "principal_value",
    ),
)

remote_access_authorizations = Table(
    "remote_access_authorizations",
    metadata,
    Column("id", String, primary_key=True),
    Column("instance_id", String, nullable=False),
    Column("subject", String, nullable=False),
    Column("email", String, nullable=True),
    Column("scope_kind", String, nullable=True),
    Column("scope_ref", String, nullable=True),
    Column("authorization_state", String, nullable=True),
    Column("claims_json", Text, nullable=False),
    Column("expires_at", Integer, nullable=True),
    Column("created_at", Integer, nullable=False),
    Column("last_checked_at", Integer, nullable=True),
    Column("updated_at", Integer, nullable=True),
    Index("ix_remote_access_authorizations_expires", "expires_at"),
    Index(
        "ux_remote_access_authorizations_scope",
        "instance_id",
        "subject",
        "scope_kind",
        "scope_ref",
        unique=True,
        sqlite_where=text("scope_kind is not null and scope_ref is not null"),
    ),
)

auth_codes = Table(
    "auth_codes",
    metadata,
    Column("code", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("is_active", Integer, nullable=False),
    Column("expires_at", String, nullable=True),
    Column("used_by_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

agent_sessions = Table(
    "agent_sessions",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="SET NULL"), nullable=True),
    Column("agent_id", String, nullable=True),
    Column("agent_name", String, nullable=True),
    Column("agent_backend", String, nullable=False),
    Column("agent_variant", String, nullable=False),
    Column("model", String, nullable=True),
    Column("reasoning_effort", String, nullable=True),
    Column("session_anchor", String, nullable=False),
    Column("workdir", Text, nullable=True),
    Column("native_session_id", Text, nullable=False),
    Column("title", Text, nullable=True),
    Column("status", String, nullable=False),
    Column("visibility", String, nullable=False, server_default="foreground"),
    Column("pinned", Integer, nullable=False, server_default="0"),
    # Live agent-runtime status, distinct from the lifecycle ``status``
    # (active/archived). One of ``idle`` / ``running`` / ``failed`` —
    # ``running`` while a turn is in flight, ``failed`` when the most recent
    # turn errored, ``idle`` otherwise. Drives the workbench sidebar status dot.
    Column("agent_status", String, nullable=False, server_default="idle"),
    # Composer state is one value per Session, not a communication record.
    Column("composer_draft_text", Text, nullable=True),
    Column("composer_draft_updated_at", String, nullable=True),
    Column("metadata_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("last_active_at", String, nullable=True),
    # A thread is ONE session per (scope, anchor). The invariant shipped in the
    # Alembic revision only (20260601_0011), while ``SQLiteSessionsService.__init__``
    # calls ``metadata.create_all`` — so any DB born from models-only, including
    # tests, silently lacked it and could not reproduce the production collision.
    # Index name matches the revision's so the two agree on one object.
    Index("uq_agent_sessions_scope_anchor", "scope_id", "session_anchor", unique=True),
    Index("ix_agent_sessions_scope_anchor_workdir", "scope_id", "session_anchor", "workdir"),
    Index("ix_agent_sessions_backend_variant", "agent_backend", "agent_variant"),
    Index("ix_agent_sessions_status_activity", "status", "last_active_at"),
    Index("ix_agent_sessions_visibility", "visibility"),
    Index("ix_agent_sessions_scope_status_activity", "scope_id", "status", "last_active_at", "created_at", "id"),
    Index(
        "ix_agent_sessions_scope_status_pinned_activity",
        "scope_id",
        "status",
        "pinned",
        "last_active_at",
        "created_at",
        "id",
    ),
    Index("ix_agent_sessions_native_session", "native_session_id"),
)

runtime_records = Table(
    "runtime_records",
    metadata,
    Column("id", String, primary_key=True),
    Column("record_type", String, nullable=False),
    Column("record_key", String, nullable=False),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="SET NULL"), nullable=True),
    Column("session_anchor", String, nullable=True),
    Column("workdir", Text, nullable=True),
    Column("payload_json", Text, nullable=False),
    Column("expires_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("record_type", "record_key", name="uq_runtime_records_type_key"),
    Index("ix_runtime_records_type_scope_expiry", "record_type", "scope_id", "expires_at"),
    Index("ix_runtime_records_scope_anchor", "scope_id", "session_anchor"),
    Index("ix_runtime_records_workdir", "workdir"),
)

run_definitions = Table(
    "run_definitions",
    metadata,
    Column("id", String, primary_key=True),
    Column("definition_type", String, nullable=False),
    Column("name", Text, nullable=True),
    Column("agent_name", String, nullable=True),
    Column("session_policy", String, nullable=True),
    Column("session_id", String, nullable=True),
    Column("legacy_session_key", Text, nullable=True),
    Column("prompt", Text, nullable=True),
    Column("message", Text, nullable=True),
    Column("message_payload_json", Text, nullable=True),
    Column("schedule_type", String, nullable=True),
    Column("cron", Text, nullable=True),
    Column("run_at", String, nullable=True),
    Column("timezone", String, nullable=True),
    Column("command_json", Text, nullable=True),
    Column("shell_command", Text, nullable=True),
    Column("prefix", Text, nullable=True),
    Column("cwd", Text, nullable=True),
    Column("mode", String, nullable=True),
    Column("timeout_seconds", Float, nullable=True),
    Column("lifetime_timeout_seconds", Float, nullable=True),
    Column("retry_exit_codes_json", Text, nullable=True),
    Column("retry_delay_seconds", Float, nullable=True),
    Column("post_to", String, nullable=True),
    Column("deliver_key", Text, nullable=True),
    Column("enabled", Integer, nullable=False),
    Column("deleted_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("last_started_at", String, nullable=True),
    Column("last_finished_at", String, nullable=True),
    Column("retired_at", String, nullable=True),
    Column("retirement_reason", String, nullable=True),
    Column("last_event_at", String, nullable=True),
    Column("last_run_at", String, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("last_exit_code", Integer, nullable=True),
    Column("last_run_id", String, nullable=True),
    Column("metadata_json", Text, nullable=False),
    Index("ix_run_definitions_type_enabled", "definition_type", "enabled"),
    Index("ix_run_definitions_session", "session_id"),
    Index("ix_run_definitions_agent", "agent_name"),
    Index("ix_run_definitions_updated", "updated_at"),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("definition_id", String, nullable=True),
    Column("run_type", String, nullable=False),
    Column("status", String, nullable=False),
    Column("source_kind", String, nullable=True),
    Column("source_actor", Text, nullable=True),
    Column("parent_run_id", String, nullable=True),
    Column("agent_name", String, nullable=True),
    Column("agent_id", String, nullable=True),
    Column("agent_backend", String, nullable=True),
    Column("model", String, nullable=True),
    Column("reasoning_effort", String, nullable=True),
    Column("session_policy", String, nullable=True),
    Column("session_id", String, nullable=True),
    Column("legacy_session_key", Text, nullable=True),
    Column("post_to", String, nullable=True),
    Column("deliver_key", Text, nullable=True),
    Column("prompt", Text, nullable=True),
    Column("message", Text, nullable=True),
    Column("message_payload_json", Text, nullable=True),
    Column("result_text", Text, nullable=True),
    Column("result_payload_json", Text, nullable=True),
    Column("message_ids_json", Text, nullable=True),
    Column("delivery_id", String, nullable=True),
    Column("callback_session_id", String, nullable=True),
    Column("callback_status", String, nullable=True),
    Column("callback_error", Text, nullable=True),
    Column("callback_run_id", String, nullable=True),
    Column("callback_completed_at", String, nullable=True),
    Column("callback_terminal_turn_id", String, nullable=True),
    Column("cancel_requested", Integer, nullable=False, default=0),
    Column("cancel_requested_at", String, nullable=True),
    Column("pid", Integer, nullable=True),
    Column("exit_code", Integer, nullable=True),
    Column("error", Text, nullable=True),
    Column("stdout", Text, nullable=True),
    Column("stderr", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("started_at", String, nullable=True),
    Column("completed_at", String, nullable=True),
    Column("updated_at", String, nullable=False),
    Column("metadata_json", Text, nullable=False),
    Index("ix_agent_runs_definition_created", "definition_id", "created_at"),
    Index("ix_agent_runs_status_created", "status", "created_at"),
    Index("ix_agent_runs_type_status_created", "run_type", "status", "created_at"),
    Index("ix_agent_runs_session_created", "session_id", "created_at"),
    Index(
        "uq_agent_runs_delivery",
        "delivery_id",
        unique=True,
        sqlite_where=text("delivery_id is not null"),
    ),
    Index("ix_agent_runs_agent_created", "agent_name", "created_at"),
    Index("ix_agent_runs_callback_status", "callback_status", "completed_at"),
    Index(
        "uq_agent_runs_callback_terminal_turn_session",
        "callback_terminal_turn_id",
        "session_id",
        unique=True,
        sqlite_where=text(
            "run_type = 'agent_run' and source_kind = 'callback' "
            "and callback_terminal_turn_id is not null and session_id is not null"
        ),
    ),
    # Leading-timestamp index for the run-graph window scan: updated_at bumps on
    # every state change, so it is the single column that scan filters on.
    Index("ix_agent_runs_updated", "updated_at"),
)

# Backwards-compatible Python aliases for legacy callers. The physical table
# names are the new domain names.
background_tasks = run_definitions
background_runs = agent_runs

show_pages = Table(
    "show_pages",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("access_mode", String, nullable=False, server_default="private"),
    Column("access_revision", Integer, nullable=False, server_default="0"),
    Column("share_id", String, nullable=True),
    Column("offline_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("share_id", name="uq_show_pages_share_id"),
    CheckConstraint(
        "access_mode in ('private', 'limited', 'public')",
        name="ck_show_pages_access_mode",
    ),
    CheckConstraint("access_revision >= 0", name="ck_show_pages_access_revision"),
    Index("ix_show_pages_share_id", "share_id"),
    Index("ix_show_pages_access_mode", "access_mode"),
)

# The Limited audience of a Show Page: one heterogeneous set of read-only
# grants, OR-ed at admission. ``email`` is instance-independent; ``group`` and
# ``organization`` only mean something relative to the organization that owns
# the page, so they carry that organization and cannot exist on a Personal
# instance. This table replaces the email-only ``show_page_authorized_emails``.
show_page_access_entries = Table(
    "show_page_access_entries",
    metadata,
    Column(
        "page_id",
        String,
        ForeignKey("show_pages.session_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("kind", String, primary_key=True),
    Column("value", String, primary_key=True),
    Column("organization_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "kind in ('email', 'group', 'organization')",
        name="ck_show_page_access_entries_kind",
    ),
    CheckConstraint(
        "length(value) between 1 and 320",
        name="ck_show_page_access_entries_value_length",
    ),
    CheckConstraint(
        "(kind = 'email' and organization_id is null) "
        "or (kind in ('group', 'organization') and organization_id is not null)",
        name="ck_show_page_access_entries_organization",
    ),
    # An organization entry IS the organization, so its value cannot name a
    # different one than the entry is scoped to.
    CheckConstraint(
        "kind <> 'organization' or value = organization_id",
        name="ck_show_page_access_entries_organization_value",
    ),
    # Admission resolves a visitor assertion to entries by (kind, value).
    Index("ix_show_page_access_entries_lookup", "kind", "value"),
    # "This organization may read" is one switch, not a list: at most one such
    # entry per page. The composite primary key cannot say that on its own,
    # because two organization rows would differ in ``value``.
    Index(
        "uq_show_page_access_entries_organization",
        "page_id",
        unique=True,
        sqlite_where=text("kind = 'organization'"),
    ),
)

show_session_events = Table(
    "show_session_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("scope", String, nullable=False),
    Column("anchor_json", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("transcript_text", Text, nullable=True),
    Column("message_id", String, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
    Column("delivery_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Index("ix_show_session_events_session_created", "session_id", "created_at"),
    Index("ix_show_session_events_type_created", "event_type", "created_at"),
)

# Append-only agent trace log. Rows here are backend/process events, not chat
# messages. They can be inspected later without polluting the transcript,
# unread counters, or Inbox activity.
agent_events = Table(
    "agent_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=True),
    Column("session_id", String, ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True),
    Column("turn_id", String, nullable=True),
    Column("run_id", String, nullable=True),
    Column("platform", String, nullable=False),
    Column("agent_name", String, nullable=True),
    Column("backend", String, nullable=True),
    Column("event_type", String, nullable=False),
    Column("visibility", String, nullable=False, server_default="trace"),
    Column("sequence", Integer, nullable=True),
    Column("content_text", Text, nullable=True),
    Column("content_json", Text, nullable=False),
    Column("metadata_json", Text, nullable=False),
    Column("source", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_agent_events_session_created_id", "session_id", "created_at", "id"),
    Index("ix_agent_events_session_type_created_id", "session_id", "event_type", "created_at", "id"),
    Index("ix_agent_events_scope_created_id", "scope_id", "created_at", "id"),
    Index("ix_agent_events_turn_sequence_id", "turn_id", "sequence", "id"),
    # Retention scan index: bounded age scan over the only rows the retention
    # service may ever delete (storage/agent_events_retention.py owns the
    # matching predicate; keep the two in sync).
    Index(
        "ix_agent_events_trace_retention",
        "created_at",
        sqlite_where=text(
            "event_type = 'tool_call' and visibility = 'trace' "
            "and datetime(created_at) is not null"
        ),
    ),
)

# Platform-agnostic chat message store. Every IM adapter (Slack, Discord,
# Telegram, Lark, WeChat, Avibe/Web UI) writes user+agent turns here so the
# workbench Inbox and per-session history can read from a single ORM
# surface instead of round-tripping the platform's own API. Native message
# identity is scoped to its conversation because some platforms reuse message
# ids across chats. ``read_at`` drives unread counts for the
# Inbox; legacy IM platforms ignore it.
messages = Table(
    "messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=True),
    Column(
        "session_id",
        String,
        ForeignKey(
            "agent_sessions.id",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
            name="fk_messages_session_id_agent_sessions",
        ),
        nullable=True,
    ),
    Column("platform", String, nullable=False),
    Column("author", String, nullable=False),
    # Fine-grained accepted communication kind, distinct from coarse ``author``.
    # Operational queue/trace/draft concepts are forbidden from this ledger.
    Column("type", String, nullable=False, server_default="assistant"),
    Column("author_id", String, nullable=True),
    Column("author_name", Text, nullable=True),
    # Origin of the message (user / agent / harness), distinct from authorship.
    # Harness-triggered prompts use author/type/source='harness' so no automated
    # input can be represented as human-authored. ``author_name`` holds the
    # display name (username / agent_name / task|watch), ``author_id`` the precise
    # id.
    Column("source", String, nullable=True),
    Column("native_message_id", String, nullable=True),
    Column("parent_native_message_id", String, nullable=True),
    Column("content_text", Text, nullable=True),
    Column("content_json", Text, nullable=False),
    Column("metadata_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("delivered_at", String, nullable=True),
    Column("read_at", String, nullable=True),
    Index(
        "uq_messages_platform_scope_native",
        "platform",
        "scope_id",
        "native_message_id",
        unique=True,
        sqlite_where=text("scope_id is not null and native_message_id is not null"),
    ),
    Index(
        "uq_messages_platform_native_unscoped",
        "platform",
        "native_message_id",
        unique=True,
        sqlite_where=text("scope_id is null and native_message_id is not null"),
    ),
    Index("ix_messages_session_created", "session_id", "created_at"),
    Index("ix_messages_session_created_id", "session_id", "created_at", "id"),
    Index("ix_messages_session_type_created_id", "session_id", "type", "created_at", "id"),
    Index("ix_messages_platform_session_created_id", "platform", "session_id", "created_at", "id"),
    Index("ix_messages_unread_session", "platform", "type", "author", "read_at", "session_id"),
    Index(
        "ix_messages_mark_read",
        "session_id",
        "author",
        "read_at",
        text("coalesce(delivered_at, created_at)"),
        "id",
    ),
    Index(
        "ix_messages_inbox_activity",
        "platform",
        "session_id",
        text("coalesce(delivered_at, created_at) desc"),
        text("id desc"),
        sqlite_where=text(build_partial_index_predicate("ix_messages_inbox_activity")),
    ),
    Index(
        "ix_messages_inbox_agent_reply",
        "platform",
        "session_id",
        text("coalesce(delivered_at, created_at) desc"),
        text("id desc"),
        sqlite_where=text(build_partial_index_predicate("ix_messages_inbox_agent_reply")),
    ),
    Index(
        "ix_messages_inbox_user_send",
        "platform",
        "session_id",
        text("coalesce(delivered_at, created_at) desc"),
        text("id desc"),
        sqlite_where=text(build_partial_index_predicate("ix_messages_inbox_user_send")),
    ),
    Index("ix_messages_scope_created", "scope_id", "created_at"),
    Index("ix_messages_scope_unread", "scope_id", "read_at"),
    Index("ix_messages_author_created", "author", "created_at"),
    CheckConstraint(
        "type not in ('queued', 'pending', 'draft', 'harness_dedupe', 'silent', 'tool_call')",
        name="ck_messages_communication_type",
    ),
)

# Durable execution ownership for one logical Agent Turn per Session. The Turn
# references its initial Delivery even before native acceptance has materialized
# a Message. Accepted steer participants reference the Turn from their Delivery.
session_turns = Table(
    "session_turns",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "session_id",
        String,
        ForeignKey(
            "agent_sessions.id",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    ),
    Column("initial_delivery_id", String, nullable=False),
    Column("state", String, nullable=False),
    Column("backend", String, nullable=False),
    Column("runtime_key", Text, nullable=True),
    Column("runtime_turn_id", Text, nullable=True),
    Column("native_turn_id", Text, nullable=True),
    Column("start_attempt_id", String, nullable=True),
    Column("start_receipt_outcome", String, nullable=True),
    Column("start_receipt_json", Text, nullable=False, server_default="{}"),
    Column("dispatch_text", Text, nullable=True),
    Column("dispatch_sha256", String, nullable=True),
    Column("terminal_outcome", String, nullable=True),
    Column("settled_by", String, nullable=True),
    Column("terminal_evidence_kind", String, nullable=True),
    Column("terminal_evidence_json", Text, nullable=False, server_default="{}"),
    # One coalesced control slot owns empty/content P0 against this exact Turn.
    Column("control_state", String, nullable=True),
    Column("control_mode", String, nullable=True),
    Column("control_attempt_id", String, nullable=True),
    Column("control_expected_native_turn_id", Text, nullable=True),
    Column("control_receipt_outcome", String, nullable=True),
    Column("control_receipt_json", Text, nullable=False, server_default="{}"),
    Column("control_successor_delivery_id", String, nullable=True),
    Column("control_successor_turn_id", String, nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("started_at", String, nullable=True),
    Column("terminal_at", String, nullable=True),
    ForeignKeyConstraint(
        ["initial_delivery_id"],
        ["message_deliveries.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_session_turns_initial_delivery",
    ),
    ForeignKeyConstraint(
        ["control_successor_delivery_id"],
        ["message_deliveries.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_session_turns_control_successor_delivery",
    ),
    ForeignKeyConstraint(
        ["control_successor_turn_id"],
        ["session_turns.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_session_turns_control_successor_turn",
    ),
    CheckConstraint(
        "state in ('waiting', 'starting', 'active', 'terminal')",
        name="ck_session_turns_state",
    ),
    CheckConstraint(
        "terminal_outcome is null or terminal_outcome in "
        "('completed', 'failed', 'canceled', 'not_written')",
        name="ck_session_turns_terminal_outcome",
    ),
    CheckConstraint(
        "start_receipt_outcome is null or start_receipt_outcome in "
        "('accepted', 'not_written', 'unknown')",
        name="ck_session_turns_start_receipt_outcome",
    ),
    CheckConstraint(
        "(state = 'waiting' and start_attempt_id is null and dispatch_text is null "
        "and dispatch_sha256 is null and start_receipt_outcome is null) "
        "or (state = 'starting' and start_attempt_id is not null "
        "and dispatch_text is not null and dispatch_sha256 is not null "
        "and (start_receipt_outcome is null or start_receipt_outcome = 'unknown')) "
        "or (state = 'active' and start_attempt_id is not null "
        "and dispatch_text is not null and dispatch_sha256 is not null "
        "and start_receipt_outcome = 'accepted') "
        "or (state = 'terminal' and (((terminal_outcome <> 'not_written' "
        "and start_attempt_id is not null and dispatch_text is not null "
        "and dispatch_sha256 is not null and start_receipt_outcome = 'accepted') "
        "or (terminal_outcome = 'failed' and start_attempt_id is not null "
        "and dispatch_text is not null and dispatch_sha256 is not null "
        "and start_receipt_outcome = 'unknown')) "
        "or (terminal_outcome = 'not_written' and start_attempt_id is not null "
        "and dispatch_text is not null and dispatch_sha256 is not null "
        "and start_receipt_outcome = 'not_written') "
        "or (terminal_outcome = 'not_written' and start_attempt_id is null "
        "and dispatch_text is null and dispatch_sha256 is null)))",
        name="ck_session_turns_start_shape",
    ),
    CheckConstraint(
        "control_state is null or control_state in "
        "('pending', 'interrupting', 'waiting_terminal', 'reconciling', 'refused', 'settled')",
        name="ck_session_turns_control_state",
    ),
    CheckConstraint(
        "control_mode is null or control_mode in ('stop_only', 'replace')",
        name="ck_session_turns_control_mode",
    ),
    CheckConstraint(
        "(state = 'terminal' and terminal_outcome is not null and terminal_at is not null) "
        "or (state <> 'terminal' and terminal_outcome is null and terminal_at is null)",
        name="ck_session_turns_terminal_shape",
    ),
    CheckConstraint(
        "(control_mode = 'replace' and control_successor_delivery_id is not null "
        "and control_successor_turn_id is not null) "
        "or (control_mode = 'stop_only' and control_successor_delivery_id is null "
        "and control_successor_turn_id is null) "
        "or (control_mode is null and control_successor_delivery_id is null "
        "and control_successor_turn_id is null)",
        name="ck_session_turns_control_shape",
    ),
    Index("ix_session_turns_session_created", "session_id", "created_at", "id"),
    Index(
        "uq_session_turns_live_session",
        "session_id",
        unique=True,
        sqlite_where=text("state in ('starting', 'active')"),
    ),
    Index(
        "uq_session_turns_message_written_attempt",
        "initial_delivery_id",
        unique=True,
        sqlite_where=text("state <> 'terminal' or start_receipt_outcome = 'accepted'"),
    ),
    Index(
        "uq_session_turns_waiting_successor",
        "session_id",
        unique=True,
        sqlite_where=text("state = 'waiting'"),
    ),
    Index(
        "uq_session_turns_control_attempt",
        "control_attempt_id",
        unique=True,
        sqlite_where=text("control_attempt_id is not null"),
    ),
    Index(
        "uq_session_turns_start_attempt",
        "start_attempt_id",
        unique=True,
        sqlite_where=text("start_attempt_id is not null"),
    ),
)

# One durable operational owner per submitted content-bearing input. Before
# positive native acceptance the complete immutable Message candidate lives only
# here. Acceptance moves one Delivery snapshot, or one ordered merged batch,
# into ``messages`` and links every participating Delivery to that record.
message_deliveries = Table(
    "message_deliveries",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "session_id",
        String,
        ForeignKey(
            "agent_sessions.id",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    ),
    Column("message_id", String, nullable=True),
    Column("priority", String, nullable=False),
    Column("state", String, nullable=False),
    Column("snapshot_json", Text, nullable=True),
    Column("snapshot_sha256", String, nullable=False),
    # Durable dispatch content is independent of Message display content.
    # Execution metadata is rendered on a request copy at the native write.
    Column("dispatch_text", Text, nullable=True),
    Column("dispatch_sha256", String, nullable=False),
    Column("dedupe_key", Text, nullable=True),
    Column("turn_id", String, nullable=True),
    Column("turn_role", String, nullable=True),
    Column("turn_position", Integer, nullable=True),
    Column("current_attempt_id", String, nullable=True),
    Column("current_attempt_kind", String, nullable=True),
    Column("current_target_turn_id", String, nullable=True),
    Column("current_expected_native_turn_id", Text, nullable=True),
    Column("current_receipt_outcome", String, nullable=True),
    Column("current_receipt_json", Text, nullable=False, server_default="{}"),
    Column("current_attempt_opened_at", String, nullable=True),
    Column(
        "delivery_history_json",
        Text,
        nullable=False,
        # Declared as an expression rather than a JSON literal on purpose: a literal
        # default containing ``:1`` is re-read as a bind parameter every time a table
        # rebuild reflects and recompiles it, which is how 20260811_0050 turned this
        # default into invalid JSON. json_object() has no colon to lose. See
        # 20260819_0057.
        server_default=text("(json_object('version', 1, 'events', json_array()))"),
    ),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("submitted_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("materialized_at", String, nullable=True),
    Column("retired_at", String, nullable=True),
    ForeignKeyConstraint(
        ["message_id"],
        ["messages.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_message_deliveries_message",
    ),
    ForeignKeyConstraint(
        ["turn_id"],
        ["session_turns.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_message_deliveries_turn",
    ),
    ForeignKeyConstraint(
        ["current_target_turn_id"],
        ["session_turns.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_message_deliveries_current_target_turn",
    ),
    UniqueConstraint("dedupe_key", name="uq_message_deliveries_dedupe"),
    CheckConstraint("priority in ('p0', 'p1', 'p3')", name="ck_message_deliveries_priority"),
    CheckConstraint(
        f"state in ({_sql_string_set(DELIVERY_STATES)})",
        name="ck_message_deliveries_state",
    ),
    CheckConstraint(
        "current_attempt_kind is null or current_attempt_kind = 'steer'",
        name="ck_message_deliveries_current_attempt_kind",
    ),
    CheckConstraint(
        "json_valid(delivery_history_json) = 1 "
        "and json_extract(delivery_history_json, '$.version') = 1 "
        "and json_type(delivery_history_json, '$.events') = 'array'",
        name="ck_message_deliveries_history_json",
    ),
    CheckConstraint(
        "(state in ('steering', 'reconciling_steer') "
        "and current_attempt_id is not null and current_attempt_kind = 'steer' "
        "and current_target_turn_id is not null "
        "and current_expected_native_turn_id is not null) "
        "or (state = 'pending_steer' and current_attempt_id is not null "
        "and current_attempt_kind = 'steer' and current_target_turn_id is not null "
        "and current_expected_native_turn_id is null) "
        "or (state not in ('steering', 'reconciling_steer', 'pending_steer') "
        "and current_attempt_id is null "
        "and current_attempt_kind is null and current_target_turn_id is null "
        "and current_expected_native_turn_id is null)",
        name="ck_message_deliveries_current_attempt_shape",
    ),
    CheckConstraint(
        "(state = 'reconciling_steer' "
        "and current_receipt_outcome in ('accepted', 'unknown')) "
        "or (state <> 'reconciling_steer' "
        "and current_receipt_outcome is null)",
        name="ck_message_deliveries_current_receipt",
    ),
    CheckConstraint(
        "(state = 'accepted' and message_id is not null and turn_id is not null "
        "and turn_role in ('initial', 'steer') and turn_position is not null "
        "and materialized_at is not null and snapshot_json is null and dispatch_text is null "
        "and current_attempt_id is null and current_attempt_kind is null "
        "and current_target_turn_id is null and current_expected_native_turn_id is null "
        "and current_receipt_outcome is null and current_attempt_opened_at is null) "
        "or (state <> 'accepted' and message_id is null and materialized_at is null)",
        name="ck_message_deliveries_materialization",
    ),
    CheckConstraint(
        "(state in ('claimed', 'interrupt_waiting') and turn_id is not null "
        "and turn_role = 'initial' and turn_position is not null) "
        "or (state = 'accepted' and turn_id is not null and turn_role is not null "
        "and turn_position is not null) "
        "or (state not in ('claimed', 'interrupt_waiting', 'accepted') "
        "and turn_id is null and turn_role is null and turn_position is null)",
        name="ck_message_deliveries_turn_membership",
    ),
    Index(
        "ix_message_deliveries_session_order",
        "session_id",
        "submitted_at",
        "id",
        sqlite_where=text(
            "state in ('reserved', 'queued', 'claimed', 'pending_steer', 'steering', "
            "'reconciling_steer')"
        ),
    ),
    Index("ix_message_deliveries_session_state", "session_id", "state", "submitted_at", "id"),
    Index("ix_message_deliveries_turn", "turn_id", "turn_position"),
    Index(
        "uq_message_deliveries_turn_position",
        "turn_id",
        "turn_position",
        unique=True,
        sqlite_where=text("turn_id is not null"),
    ),
    Index("ix_message_deliveries_current_target_turn", "current_target_turn_id"),
    Index("ix_message_deliveries_current_attempt", "current_attempt_id"),
)

agent_runs.append_constraint(
    ForeignKeyConstraint(
        ["delivery_id"],
        ["message_deliveries.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_agent_runs_delivery",
    )
)

# The Show event can exist before its input is accepted, so its durable anchor is
# the Delivery. The Message link is filled only on acceptance for history reads.
show_session_events.append_constraint(
    ForeignKeyConstraint(
        ["delivery_id"],
        ["message_deliveries.id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
        name="fk_show_session_events_delivery",
    )
)

# Opaque-token proxy for chat media. The workbench browser can't load
# ``file://`` and we deliberately neuter arbitrary remote images, so a local
# file referenced by an agent reply (or uploaded by the user) is registered
# here and served back over ``/api/media/<token>``. The URL carries only the
# opaque ``token`` — never a filesystem path, never a session — so it is stable
# within the referencing session and the browser can cache it. ``content_type`` /
# ``file_ext`` are stored so the response and the UI file card don't have to
# re-derive them; ``kind`` (image|file) selects inline-image vs download-card
# rendering; ``source`` distinguishes agent output from user uploads so one
# table serves both. ``size_bytes`` + ``mtime_ns`` are the content fingerprint:
# :func:`storage.media_service.register` reuses an existing token for the same
# session and (local_path, size_bytes, mtime_ns), while a different session or
# changed file mints a fresh token.
media_objects = Table(
    "media_objects",
    metadata,
    Column("token", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=True),
    Column("session_id", String, ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True),
    Column("message_id", String, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
    Column("kind", String, nullable=False),
    Column("source", String, nullable=False),
    Column("local_path", Text, nullable=False),
    Column("file_name", Text, nullable=True),
    Column("content_type", String, nullable=True),
    Column("file_ext", String, nullable=True),
    Column("size_bytes", Integer, nullable=True),
    Column("mtime_ns", Integer, nullable=True),
    # Image pixel dimensions, read at registration when the file is a decodable
    # image (NULL for non-images / unknown). The UI uses them to reserve an
    # image's box before it loads so the transcript never shifts on scroll.
    Column("width_px", Integer, nullable=True),
    Column("height_px", Integer, nullable=True),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=True),
    Column("revoked_at", String, nullable=True),
    Index("ix_media_objects_session", "session_id"),
    Index("ix_media_objects_scope_created", "scope_id", "created_at"),
    # Backs the fingerprint prefix of register()'s session-scoped dedup lookup.
    Index("ix_media_objects_dedup", "local_path", "size_bytes", "mtime_ns"),
)

# A legacy media token could be reused across multiple sessions before token
# dedup became session-scoped. Keep every trusted referencing session so those
# historical attachments remain readable without treating the opaque token as
# authorization for an arbitrary session.
media_object_references = Table(
    "media_object_references",
    metadata,
    Column("token", String, ForeignKey("media_objects.token", ondelete="CASCADE"), primary_key=True),
    Column(
        "session_id",
        String,
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("created_at", String, nullable=False),
    Index("ix_media_object_references_session", "session_id"),
)

# Per-install browser Push API subscriptions for PWA Web Push. These are
# runtime/device endpoints, not user-authored config: one user may install the
# app on multiple devices, and endpoints can rotate or expire independently.
web_push_subscriptions = Table(
    "web_push_subscriptions",
    metadata,
    Column("id", String, primary_key=True),
    Column("user_key", String, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("p256dh", Text, nullable=False),
    Column("auth", Text, nullable=False),
    Column("device_id", String, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("device_label", Text, nullable=True),
    Column("enabled", Integer, nullable=False),
    Column("last_success_at", String, nullable=True),
    Column("last_failure_at", String, nullable=True),
    Column("failure_count", Integer, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("endpoint", name="uq_web_push_subscriptions_endpoint"),
    Index("ix_web_push_subscriptions_user_enabled", "user_key", "enabled"),
    Index("ix_web_push_subscriptions_user_device", "user_key", "device_id"),
)

# Resource ACLs are local enforcement state. The hosted control plane receives
# only safe metadata and desired revisions; it never stores local resource
# content or secret values.
resource_access_policies = Table(
    "resource_access_policies",
    metadata,
    Column("resource_kind", String, primary_key=True),
    Column("resource_id", String, primary_key=True),
    Column("organization_id", String, nullable=True),
    Column("owner_user_id", String, nullable=True),
    Column("owner_email", String, nullable=True),
    Column("access_level", String, nullable=False, server_default=text("'private'")),
    Column("created_by_user_id", String, nullable=True),
    Column("updated_by_user_id", String, nullable=True),
    Column("policy_revision", Integer, nullable=False, server_default=text("0")),
    Column("last_applied_control_plane_revision", Integer, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint(
        "resource_kind in ('agent', 'vault_secret', 'skill', 'show_page')",
        name="ck_resource_access_policies_kind",
    ),
    CheckConstraint(
        "access_level in ('public', 'scope', 'private')",
        name="ck_resource_access_policies_access_level",
    ),
    Index(
        "ix_resource_access_policies_org_level",
        "organization_id",
        "access_level",
        "resource_kind",
    ),
    Index("ix_resource_access_policies_owner", "owner_user_id", "resource_kind"),
)

resource_access_groups = Table(
    "resource_access_groups",
    metadata,
    Column("resource_kind", String, primary_key=True),
    Column("resource_id", String, primary_key=True),
    Column("group_id", String, primary_key=True),
    Column("organization_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    ForeignKeyConstraint(
        ["resource_kind", "resource_id"],
        ["resource_access_policies.resource_kind", "resource_access_policies.resource_id"],
        ondelete="CASCADE",
    ),
    Index("ix_resource_access_groups_group", "organization_id", "group_id", "resource_kind"),
)

# Vaults — secret management for agents.
# A named secret is referenced by a globally-unique env-style ``name``. Tags are
# the only grouping selector; skill association is stored as reserved
# ``skill:<name>`` tags. Values are envelope-encrypted at rest (storage/vault_crypto.py):
# standard tier under a machine key, protected tier under a password/passkey-derived key.
# ``ciphertext``/``nonce`` are base64 text and ``wrap_meta`` is a JSON blob of the
# wrapped DEK + scheme — there is deliberately no plaintext column. The
# ``vault_secrets`` table is denylisted in ``vibe data query``.
vault_secrets = Table(
    "vault_secrets",
    metadata,
    Column("id", String, primary_key=True),
    # Globally unique, case-preserving shell name ``^[A-Za-z_][A-Za-z0-9_]*$``.
    # The exact UNIQUE(name) keeps existing exact lookup semantics, while the
    # unique lower(name) expression index below atomically rejects case-only
    # duplicates so exact lookup stays unambiguous under concurrent creates.
    Column("name", String, nullable=False),
    Column("tags", Text, nullable=True),  # JSON array
    Column("kind", String, nullable=False, server_default="static"),  # static | keypair (P2)
    Column("protection", String, nullable=False, server_default="standard"),  # standard (P0) | protected (P1)
    Column("signer_kind", String, nullable=True),  # local | external | mpc:<provider> (P2, keypair only)
    Column("source", String, nullable=False, server_default="manual"),  # manual | imported:1password | op-reference
    # Envelope: AES-256-GCM ciphertext/nonce (base64 text); ``wrap_meta`` JSON holds the
    # wrapped DEK + scheme. All null for external/mpc/op-reference (no local key/value).
    Column("ciphertext", Text, nullable=True),
    Column("nonce", Text, nullable=True),
    Column("wrap_meta", Text, nullable=True),
    Column("public_meta", Text, nullable=True),  # JSON: desc / pubkey / address / op:// uri
    Column("policy", Text, nullable=True),  # JSON: allowed modes, allowed hosts, always_ask
    Column("last_used_at", String, nullable=True),
    Column("use_count", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("name", name="uq_vault_secrets_name"),
    Index("ix_vault_secrets_name_kind", "name", "kind"),
)
Index("uq_vault_secrets_name_folded", func.lower(vault_secrets.c.name), unique=True)

# One queue for everything that needs a human: P0 uses ``provision`` (dynamic ask via
# ``$<NAME>``); ``access``/``sign``/``proxy``/``keygen`` are P1+.
vault_requests = Table(
    "vault_requests",
    metadata,
    Column("id", String, primary_key=True),
    Column("request_type", String, nullable=False),  # provision | access | sign | proxy | keygen
    Column("secret_name", String, nullable=True),
    Column("requester", Text, nullable=True),  # JSON: session_id / agent / run
    Column("delivery", Text, nullable=True),  # JSON
    Column("status", String, nullable=False, server_default="pending"),
    Column("message_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("decided_at", String, nullable=True),
    Column("expires_at", String, nullable=True),
    # Auto-resume callback bookkeeping: set to "pending" when a request transitions to a
    # terminal state (approved/denied/fulfilled/failed/expired) so the daemon sweep enqueues
    # exactly one callback turn to the requesting session, then "sent"/"skipped". NULL = no
    # callback owed (e.g. rows created before this feature, or born-fulfilled).
    Column("callback_status", String, nullable=True),
    Index("ix_vault_requests_status_created", "status", "created_at"),
    Index("ix_vault_requests_callback_status", "callback_status", "decided_at"),
)
event.listen(
    vault_requests,
    "after_create",
    DDL(
        """
        create trigger if not exists trg_vault_requests_pending_provision_name_case_insert
        before insert on vault_requests
        when new.request_type = 'provision'
          and new.status = 'pending'
          and new.secret_name is not null
          and exists (
            select 1
            from vault_requests
            where request_type = 'provision'
              and status = 'pending'
              and secret_name is not null
              and lower(secret_name) = lower(new.secret_name)
              and secret_name <> new.secret_name
          )
        begin
          select raise(abort, 'vault pending provision name case conflict');
        end
        """
    ),
)
event.listen(
    vault_requests,
    "after_create",
    DDL(
        """
        create trigger if not exists trg_vault_requests_pending_provision_name_case_update
        before update of request_type, status, secret_name on vault_requests
        when new.request_type = 'provision'
          and new.status = 'pending'
          and new.secret_name is not null
          and exists (
            select 1
            from vault_requests
            where id <> new.id
              and request_type = 'provision'
              and status = 'pending'
              and secret_name is not null
              and lower(secret_name) = lower(new.secret_name)
              and secret_name <> new.secret_name
          )
        begin
          select raise(abort, 'vault pending provision name case conflict');
        end
        """
    ),
)

# Metadata + audit of active unlock grants. Key material is NEVER stored here;
# resident avault agent delivery material wires in later as process-local opaque
# state. The member set is frozen at grant time and keyed by the first-class grant id.
vault_grants = Table(
    "vault_grants",
    metadata,
    Column("id", String, primary_key=True),
    Column("member_snapshot", Text, nullable=False),  # JSON: frozen protected secret-name set
    Column("source_selector", Text, nullable=True),  # JSON: env/tag/skill source that produced the grant
    Column("request_id", String, nullable=True),
    Column("session_id", String, nullable=True),
    Column("purpose", String, nullable=False, server_default="run"),  # run | fetch | inject
    Column("status", String, nullable=False, server_default="active"),  # active | reserved | expired | revoked
    Column("one_shot", Integer, nullable=False, server_default=text("0")),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("revoked_at", String, nullable=True),
    Column("agent_ready", Integer, nullable=False, server_default=text("0")),
    Column("agent_ready_at", String, nullable=True),
    Index("ix_vault_grants_status_expires", "status", "expires_at"),
    Index("ix_vault_grants_request", "request_id"),
    Index("ix_vault_grants_session_purpose", "session_id", "purpose"),
)

vault_auth_factors = Table(
    "vault_auth_factors",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("label", Text, nullable=True),
    Column("rp_id", String, nullable=False),
    Column("credential_id", Text, nullable=False),
    Column("public_key", Text, nullable=False),
    Column("alg", Integer, nullable=False),
    Column("sign_count", Integer, nullable=False, server_default=text("0")),
    Column("transports", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("last_used_at", String, nullable=True),
    Column("disabled_at", String, nullable=True),
    UniqueConstraint("credential_id", name="uq_vault_auth_factors_credential_id"),
    Index("ix_vault_auth_factors_kind_rp", "kind", "rp_id", "disabled_at"),
)

vault_operation_challenges = Table(
    "vault_operation_challenges",
    metadata,
    Column("id", String, primary_key=True),
    Column("operation", String, nullable=False),
    Column("secret_name", String, nullable=True),
    Column("secret_id", String, nullable=True),
    Column("secret_updated_at", String, nullable=True),
    Column("challenge_hash", String, nullable=False),
    Column("rp_id", String, nullable=False),
    Column("origin", Text, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("consumed_at", String, nullable=True),
    Column("factor_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Index("ix_vault_operation_challenges_lookup", "operation", "secret_name", "expires_at"),
    Index("ix_vault_operation_challenges_consumed", "consumed_at", "expires_at"),
)

# Append-only audit log. Secret VALUES never appear here — only names, requesters,
# and delivery summaries.
vault_audit = Table(
    "vault_audit",
    metadata,
    Column("id", String, primary_key=True),
    Column("ts", String, nullable=False),
    Column("event", String, nullable=False),  # created/updated/deleted/delivered/denied/granted/...
    Column("secret_name", String, nullable=True),
    Column("requester", Text, nullable=True),
    Column("delivery", Text, nullable=True),
    Column("request_id", String, nullable=True),
    Column("grant_id", String, nullable=True),
    Index("ix_vault_audit_ts", "ts"),
    Index("ix_vault_audit_secret_ts", "secret_name", "ts"),
)

imported_state_tables = [
    show_pages,
    background_runs,
    background_tasks,
    scope_settings,
    auth_codes,
    agent_sessions,
    runtime_records,
    scopes,
    messages,
    agent_events,
    show_session_events,
    web_push_subscriptions,
]
