from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

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
    # Live agent-runtime status, distinct from the lifecycle ``status``
    # (active/archived). One of ``idle`` / ``running`` / ``failed`` —
    # ``running`` while a turn is in flight, ``failed`` when the most recent
    # turn errored, ``idle`` otherwise. Drives the workbench sidebar status dot.
    Column("agent_status", String, nullable=False, server_default="idle"),
    Column("metadata_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("last_active_at", String, nullable=True),
    Index("ix_agent_sessions_scope_anchor_workdir", "scope_id", "session_anchor", "workdir"),
    Index("ix_agent_sessions_backend_variant", "agent_backend", "agent_variant"),
    Index("ix_agent_sessions_status_activity", "status", "last_active_at"),
    Index("ix_agent_sessions_scope_status_activity", "scope_id", "status", "last_active_at", "created_at", "id"),
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
    Column("callback_session_id", String, nullable=True),
    Column("callback_status", String, nullable=True),
    Column("callback_error", Text, nullable=True),
    Column("callback_run_id", String, nullable=True),
    Column("callback_completed_at", String, nullable=True),
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
    Index("ix_agent_runs_agent_created", "agent_name", "created_at"),
    Index("ix_agent_runs_callback_status", "callback_status", "completed_at"),
)

# Backwards-compatible Python aliases for legacy callers. The physical table
# names are the new domain names.
background_tasks = run_definitions
background_runs = agent_runs

show_pages = Table(
    "show_pages",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("visibility", String, nullable=False),
    Column("share_id", String, nullable=True),
    Column("offline_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("share_id", name="uq_show_pages_share_id"),
    Index("ix_show_pages_share_id", "share_id"),
    Index("ix_show_pages_visibility", "visibility"),
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
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False),
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
)

# Platform-agnostic chat message store. Every IM adapter (Slack, Discord,
# Telegram, Lark, WeChat, Avibe/Web UI) writes user+agent turns here so the
# workbench Inbox and per-session history can read from a single ORM
# surface instead of round-tripping the platform's own API. ``platform`` +
# ``native_message_id`` is unique when present so a duplicate webhook
# delivery is a no-op upsert. ``read_at`` drives unread counts for the
# Inbox; legacy IM platforms ignore it.
messages = Table(
    "messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False),
    Column("session_id", String, ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True),
    Column("platform", String, nullable=False),
    Column("author", String, nullable=False),
    # Fine-grained message type, distinct from the coarse ``author``:
    # user / assistant / tool_call / notify / result. The inbox preview uses
    # the latest ``assistant`` row. Persisted regardless of IM display muting.
    Column("type", String, nullable=False, server_default="assistant"),
    Column("author_id", String, nullable=True),
    Column("author_name", Text, nullable=True),
    # Origin of the message (user / agent / harness), distinct from the coarse
    # ``author`` role — a Harness-triggered prompt is author='user' but
    # source='harness'. ``author_name`` holds the display name (username /
    # agent_name / task|watch), ``author_id`` the precise id.
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
    UniqueConstraint("platform", "native_message_id", name="uq_messages_platform_native"),
    Index("ix_messages_session_created", "session_id", "created_at"),
    Index("ix_messages_session_created_id", "session_id", "created_at", "id"),
    Index("ix_messages_session_type_created_id", "session_id", "type", "created_at", "id"),
    Index("ix_messages_platform_session_created_id", "platform", "session_id", "created_at", "id"),
    Index("ix_messages_unread_session", "platform", "type", "author", "read_at", "session_id"),
    Index("ix_messages_mark_read", "session_id", "author", "read_at", "created_at", "id"),
    Index(
        "ix_messages_inbox_activity",
        "platform",
        "session_id",
        text("created_at desc"),
        text("id desc"),
        sqlite_where=text("session_id is not null and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"),
    ),
    Index(
        "ix_messages_inbox_agent_reply",
        "platform",
        "session_id",
        text("created_at desc"),
        text("id desc"),
        sqlite_where=text("session_id is not null and type in ('result', 'notify', 'error')"),
    ),
    Index(
        "ix_messages_inbox_user_send",
        "platform",
        "session_id",
        text("created_at desc"),
        text("id desc"),
        sqlite_where=text(
            "session_id is not null and author = 'user' and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
        ),
    ),
    Index("ix_messages_scope_created", "scope_id", "created_at"),
    Index("ix_messages_scope_unread", "scope_id", "read_at"),
    Index("ix_messages_author_created", "author", "created_at"),
)

# Opaque-token proxy for chat media. The workbench browser can't load
# ``file://`` and we deliberately neuter arbitrary remote images, so a local
# file referenced by an agent reply (or uploaded by the user) is registered
# here and served back over ``/api/media/<token>``. The URL carries only the
# opaque ``token`` — never a filesystem path, never a session — so it is stable
# across messages/sessions and the browser can cache it. ``content_type`` /
# ``file_ext`` are stored so the response and the UI file card don't have to
# re-derive them; ``kind`` (image|file) selects inline-image vs download-card
# rendering; ``source`` distinguishes agent output from user uploads so one
# table serves both. ``size_bytes`` + ``mtime_ns`` are the content fingerprint:
# :func:`storage.media_service.register` reuses an existing token for the same
# (local_path, size_bytes, mtime_ns) so a re-referenced file keeps one cacheable
# URL, while a changed file mints a fresh token (busting the browser cache).
media_objects = Table(
    "media_objects",
    metadata,
    Column("token", String, primary_key=True),
    Column("scope_id", String, ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False),
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
    # Backs register()'s dedup lookup (machine-global content fingerprint).
    Index("ix_media_objects_dedup", "local_path", "size_bytes", "mtime_ns"),
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

# Vaults — secret management for agents (design: docs/plans/vaults.md).
# A named secret is referenced by a globally-unique env-style ``name``; ``group_name``
# is a lightweight organizational + unlock-scope axis (NOT a separate keyspace, NOT a
# 1Password-style multi-vault container). Values are envelope-encrypted at rest
# (storage/vault_crypto.py): standard tier under a machine key, protected tier (P1)
# under a password/passkey-derived key. ``ciphertext``/``nonce`` are base64 text and
# ``wrap_meta`` is a JSON blob of the wrapped DEK + scheme — there is deliberately no
# plaintext column. The ``vault_secrets`` table is denylisted in ``vibe data query``.
vault_groups = Table(
    "vault_groups",
    metadata,
    Column("name", String, primary_key=True),
    Column("description", Text, nullable=True),
    # Whether secrets in this group may be covered by a scope grant (P1). Stored as
    # Integer (0/1) per the codebase boolean convention. Forced 0 if the group holds
    # a keypair (signing is never grantable).
    Column("grantable", Integer, nullable=False, server_default="1"),
    Column("max_grant_ttl_seconds", Integer, nullable=False, server_default="900"),
    Column("created_at", String, nullable=False),
)

vault_secrets = Table(
    "vault_secrets",
    metadata,
    Column("id", String, primary_key=True),
    # Globally-unique reference key, ENV-style ``^[A-Z][A-Z0-9_]*$``.
    Column("name", String, nullable=False),
    Column("group_name", String, ForeignKey("vault_groups.name"), nullable=False, server_default="default"),
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
    Index("ix_vault_secrets_group", "group_name"),
)

# Skills <-> secrets is an M:N declared-dependency relation (a skill declares the
# secrets it needs), NOT ownership: one secret used by three skills is one row here
# plus three link rows, never duplicated. ``source`` distinguishes skill-meta-derived
# links from agent/user-made ones. (P1.)
vault_links = Table(
    "vault_links",
    metadata,
    Column("id", String, primary_key=True),
    Column("secret_name", String, ForeignKey("vault_secrets.name", ondelete="CASCADE"), nullable=False),
    Column("skill_name", String, nullable=False),
    Column("source", String, nullable=False),  # skill_meta | agent | user
    Column("required", Integer, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    UniqueConstraint("secret_name", "skill_name", name="uq_vault_links_secret_skill"),
    Index("ix_vault_links_skill", "skill_name"),
)

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
    Index("ix_vault_requests_status_created", "status", "created_at"),
)

# Metadata + audit of active scope-typed unlock grants (P1). The key material — the
# cached DEK set — is NEVER stored here; it lives only in daemon memory. This row
# records the scope + bounds for the UI and audit; the member set is frozen at grant
# time. Created in P0, exercised in P1.
vault_grants = Table(
    "vault_grants",
    metadata,
    Column("id", String, primary_key=True),
    Column("scope_type", String, nullable=False),  # secret | skill | group
    Column("scope_ref", String, nullable=False),
    Column("member_snapshot", Text, nullable=True),  # JSON: frozen secret-name set (audit)
    Column("session_id", String, nullable=True),  # null = any-session
    Column("status", String, nullable=False, server_default="active"),  # active | expired | revoked
    Column("created_by_request_id", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("revoked_at", String, nullable=True),
    Index("ix_vault_grants_status_expires", "status", "expires_at"),
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
