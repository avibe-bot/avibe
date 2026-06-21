# SQLite State Migration Design

## Status

- Branch: `feature/sqlite-state-migration-plan`
- Scope: migrate Settings and Sessions from JSON files to SQLite.
- Source compatibility: keep existing `SettingsStore` and `SessionsStore`
  Python APIs during the first pass.
- Release assumption: the previous 14-table draft has not shipped, so the
  initial Alembic revision is rewritten to the final six-table model instead of
  adding a migration from an unreleased schema.

## Product Intent

This migration is not a mechanical JSON-to-table conversion. The goal is to
separate durable product state from user-editable files and establish a small
data model that can support Web UI Chat, sub-agents, session recovery, and
future querying without inheriting the accidental shape of the old JSON files.

The durable model recognizes six concepts:

- `scopes`: external IM objects that Vibe Remote can configure or observe.
- `scope_settings`: current default configuration for a scope.
- `agent_sessions`: Vibe Remote-owned agent session handles.
- `runtime_records`: typed restart/dedup/recovery records.
- `auth_codes`: user binding code lifecycle.
- `state_meta`: database-level migration and compatibility markers.

## Storage Boundary

SQLite becomes the source of truth for:

- channel, guild, platform policy, and user settings;
- bound user roles;
- bind codes;
- agent session mappings;
- active poll/thread recovery state;
- processed-message dedup records;
- discovered chat identity metadata.

These remain file-based for now:

- `config/config.json`, because it contains global runtime/platform/backend
  config and secrets;
- project-level agent/skill config, because users should be able to inspect and
  version it in their repository;
- task/watch persistence, because volume is low and the current queue semantics
  are already clear;
- process runtime artifacts such as pid/status files.

## Database File

Default path:

```text
~/.vibe_remote/state/vibe.sqlite
```

Connection setup:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

WAL allows the Web UI process and service process to share the same SQLite file
with concurrent readers. `busy_timeout` keeps short write races from failing
immediately.

## Table Design

### `scopes`

`scopes` represents a long-lived external target in an IM platform. A scope is
not a thread. Thread and flat-chat differences are represented by
`agent_sessions.session_anchor`.

Fields:

- `id`: semantic primary key, formatted as
  `<platform>::<scope_type>::<native_id>`, for example
  `slack::channel::C123`. This is a readable DB identity, not something runtime
  code should parse to recover the platform ID.
- `platform`: `slack`, `telegram`, `discord`, `lark`, `wechat`, or future
  platform name.
- `scope_type`: `channel`, `user`, `guild`, or `platform`.
- `native_id`: the original platform ID used by IM adapters, for example a
  Slack channel ID or Telegram chat ID.
- `parent_scope_id`: optional parent scope, such as a Discord channel pointing
  to its guild scope.
- `display_name`: UI name such as `#general`, `Core Forum`, or `Alex`.
- `native_type`: platform-native kind such as Telegram `supergroup`.
- `is_private`: private channel/chat/DM marker.
- `supports_threads`: normalized thread/topic capability.
- `metadata_json`: platform-observed metadata that is not a primary query
  dimension.
- `first_seen_at`, `last_seen_at`, `updated_at`: lifecycle timestamps.

Constraints and indexes:

- Primary key: `id`.
- Unique: `(platform, scope_type, native_id)`, because one external IM object
  must map to one scope.
- Index: `(platform, scope_type)` for platform UI lists.
- Index: `parent_scope_id` for parent/child navigation.

### `scope_settings`

`scope_settings` stores the current default configuration attached to a scope.
Channel settings, user settings, guild settings, and platform-level guild policy
share one table. Stable query dimensions are columns; volatile or nested details
stay in JSON.

Fields:

- `scope_id`: primary key and foreign key to `scopes.id`.
- `enabled`: whether the scope is enabled.
- `role`: user permission role, currently `admin` or `member`; only meaningful
  for user scopes.
- `workdir`: default working directory for this scope.
- `agent_backend`: deprecated legacy scope backend field; ignored by current
  Agent-first routing.
- `agent_variant`: default agent/sub-agent name.
- `model`: default model for this scope.
- `reasoning_effort`: default reasoning effort when the backend supports it.
- `require_mention`: channel/platform mention policy override; null means
  inherit.
- `settings_version`: version of `settings_json`.
- `settings_json`: non-primary-query details such as `show_message_types`,
  bound DM chat ID, and backend-specific temporary settings.
- `created_at`, `updated_at`: lifecycle timestamps.

Constraints and indexes:

- Primary key: `scope_id`. One scope has one current default settings row.
- Index: `role` for admin/user listing.
- Index: `workdir` for project-level grouping and management.
- Legacy index: `(agent_backend, model)` may remain until schema cleanup.

### `agent_sessions`

`agent_sessions` stores Vibe Remote-owned agent session instances. The ID is a
short random handle intended to be injected into prompts, shown in logs, and
passed back to CLI commands.

Fields:

- `id`: primary key, formatted as `ses` plus 10 lowercase base32-like
  characters, for example `sesk8m4q2p7x`. It has no symbols, is not purely
  numeric, and is short enough for agents to copy.
- `scope_id`: optional foreign key to `scopes.id`.
- `agent_backend`: backend used to create the native session.
- `agent_variant`: routing agent or sub-agent name; defaults to `default` for
  future APIs.
- `model`: model used when the native session was created.
- `reasoning_effort`: reasoning effort used when the native session was
  created.
- `session_anchor`: conversation anchor. A Slack thread uses thread/message ID;
  a flat Telegram chat uses the chat itself.
- `workdir`: working directory used for the session.
- `native_session_id`: underlying backend session ID, encoded as a string while
  preserving legacy non-string values.
- `title`: optional Web UI title.
- `status`: `active`, `stale`, `archived`, or future state.
- `metadata_json`: compatibility and backend-specific details.
- `created_at`, `updated_at`, `last_active_at`: lifecycle timestamps.

Constraints and indexes:

- Primary key: `id`.
- No business unique constraint on `scope_id + session_anchor + workdir`. Future
  Web UI Chat, forks, sub-agents, and parallel agents can create multiple
  sessions under the same context.
- Index: `(scope_id, session_anchor, workdir)` to restore/list sessions for a
  context.
- Index: `(agent_backend, agent_variant)` for backend/agent filtering.
- Index: `(status, last_active_at)` for "recent active session" queries.
- Index: `native_session_id` for reverse lookup and compatibility.

### `runtime_records`

`runtime_records` keeps restartable operational state and idempotency markers
without splitting every low-level runtime concept into its own table.

Fields:

- `id`: primary key, formatted as `runtime::<record_type>::<record_key>`.
- `record_type`: `active_poll`, `active_thread`, `processed_message`, or future
  runtime record type.
- `record_key`: type-local stable key.
- `scope_id`: optional foreign key to `scopes.id`.
- `session_anchor`: optional conversation anchor for cleanup or listing.
- `workdir`: optional project directory for diagnostics and cleanup.
- `payload_json`: type-specific recovery payload.
- `expires_at`: optional cleanup time.
- `created_at`, `updated_at`: lifecycle timestamps.

Constraints and indexes:

- Primary key: `id`.
- Unique: `(record_type, record_key)`, because `record_key` is defined as the
  type-local business identity.
- Index: `(record_type, scope_id, expires_at)` for recovery and cleanup.
- Index: `(scope_id, session_anchor)` for context-level runtime lookup.
- Index: `workdir` for project-level cleanup and diagnostics.

### `auth_codes`

`auth_codes` stores bind-code lifecycle. It is independent from scopes because
codes can be created before any user scope exists.

Fields:

- `code`: primary key and user-entered token.
- `type`: `one_time` or `expiring`.
- `is_active`: whether the code can still be used.
- `expires_at`: optional expiration.
- `used_by_json`: list of user scope keys or legacy user IDs that consumed it.
- `created_at`, `updated_at`: lifecycle timestamps.

Constraints:

- Primary key: `code`.
- No extra unique constraint.

### `state_meta`

`state_meta` stores database-level metadata.

Fields:

- `key`: primary key.
- `value_json`: JSON-encoded value.
- `updated_at`: timestamp.

Current keys:

- `json_import_completed_at`
- `sessions_last_activity`

## JSON Payload Policy

SQLite JSON functions exist, but this design does not rely on JSON as the main
query interface. If a field is expected to be filtered, sorted, joined, or used
for authorization, it becomes a column.

Column fields:

- `role`
- `workdir`
- `agent_variant`
- `model`
- `reasoning_effort`
- `native_type`
- `is_private`
- `supports_threads`
- `session_anchor`
- `status`

JSON fields:

- `show_message_types`, because current code reads it with settings as a whole;
- backend-specific temporary routing details;
- platform raw metadata such as username or flags;
- active poll internals such as baseline message IDs and seen tool calls.

## Migration Flow

1. Resolve target DB and state directory.
2. Acquire `migration.lock`.
3. Run Alembic migrations.
4. If `state_meta.json_import_completed_at` exists, skip JSON import.
5. Clear previously imported SQLite rows if a previous import failed before
   writing the marker.
6. Back up `settings.json`, `sessions.json`, and `discovered_chats.json` to
   `state/backups/sqlite-state-migration-<timestamp>/`.
7. Parse JSON from temporary copies so source JSON is never rewritten during
   import.
8. Import settings through `SQLiteSettingsService`.
9. Import sessions through `SQLiteSessionsService`.
10. Import discovered chats into `scopes`.
11. Run `PRAGMA integrity_check`.
12. Write `state_meta.json_import_completed_at`.

The old JSON files are not deleted and are no longer written after the stores
switch to SQLite. Later startup decisions rely on the SQLite marker, not on
whether JSON files still exist.

## Compatibility Notes

- Existing `SettingsStore` methods continue returning `ChannelSettings`,
  `GuildSettings`, `UserSettings`, and `BindCode`.
- Existing `SessionsStore` methods continue returning `SessionState` and
  `ActivePollInfo`.
- Settings writes upsert missing scopes but do not erase observed scope metadata
  such as private/thread capability flags gathered from discovered chats.
- Legacy session values that were not strings are stored with a JSON prefix and
  decoded back to their original shape.
- Legacy raw session scope keys are preserved in `agent_sessions.metadata_json`
  so direct `SessionsStore.save()` / reload behavior does not rename keys.
- Platform backfill for old sessions still requires a primary platform when the
  platform cannot be inferred.

## Validation

Focused validation for this migration:

```text
uv run pytest \
  tests/test_sqlite_state_migration.py \
  tests/test_sqlite_settings_store.py \
  tests/test_sqlite_sessions_store.py \
  tests/test_sqlite_state_startup.py
```

The test coverage checks:

- Alembic creates and stamps the initial schema;
- old JSON files are backed up and not rewritten;
- import marker is only written after successful import;
- invalid JSON can be fixed and retried;
- custom state directories do not create default home directories;
- Settings and Sessions stores reload external SQLite writes;
- legacy session mappings and non-string session values round-trip.
