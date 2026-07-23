# Telegram Topic Settings

## Background

Telegram forum messages already carry `message_thread_id` through
`MessageContext.thread_id`, so replies and Agent Sessions are isolated by topic.
Persistent settings, authorization, routing, and the Web UI still resolve only
the parent chat, which prevents operators from enabling and configuring selected
topics independently.

## Goal

Make a Telegram forum topic a first-class child settings scope. An explicitly
configured topic must be able to override the parent group's access, mention,
binding, Agent routing, working directory, and message visibility settings.
Topics without an override continue to inherit the group unchanged.

## Design

### Scope model

- Persist topics as `scopes.scope_type = "thread"`.
- Use `<chat_id>/<message_thread_id>` as the thread scope's native ID because a
  Telegram topic ID is unique only inside its chat.
- Set `parent_scope_id` to the Telegram channel scope.
- Store discovered topic names and Telegram IDs in scope metadata.
- Treat Telegram General as topic ID `1` for settings resolution without
  changing the existing outbound/session behavior when Telegram omits the ID.

### Precedence

1. DMs continue to use bound-user settings.
2. A forum topic with an explicit thread setting uses that complete setting.
3. A forum topic without an explicit setting uses its parent channel setting.
4. Non-forum groups continue to use channel settings.

Creating an override snapshots the parent's effective channel settings. Removing
the override restores inheritance. This makes `group disabled + selected topic
enabled` and `group enabled + selected topic disabled` both possible without
introducing field-level inheritance ambiguity.

### Runtime resolution

Use one context-aware settings key/resolver for mention policy, authorization,
message visibility, cwd, and Agent routing. Keep the existing channel-based
session key stable; topic identity remains the session anchor.

### Topic identity contract

The logical topic identity is always `(chat_id, topic_id)`. Telegram General is
topic `1` even when inbound and outbound Bot API payloads omit
`message_thread_id`.

- persisted thread scope native ID: `<chat_id>/<topic_id>`
- runtime settings key: `thread::<chat_id>::<topic_id>`
- explicit delivery key: `telegram::channel::<chat_id>::thread::<topic_id>`
- runtime session anchor: `telegram_<chat_id>_<topic_id>`

Session-ID targeting must map a persisted thread scope back to its parent
channel plus topic delivery key. Existing `telegram_<topic_id>` session anchors
are compatibility inputs only and migrate to the scoped anchor on first use so
native backend history is preserved without retaining cross-chat collisions.

### Discovery and API

Telegram cannot list all forum topics. Discover topics passively from inbound
messages and topic service events, and include them under forum chats returned by
the Telegram discovery API. Add focused thread-setting mutation endpoints so a
single topic save never replaces the platform's full channel map.

### UI

Forum group rows expose a nested Topics section. Each topic shows whether it
inherits the group or has a custom override. Operators can customize, disable,
or reset a topic while reusing the existing routing configuration panel.

## Tracker

- [x] Add thread scope state and SQLite round-trip support.
- [x] Add Telegram topic discovery and General-topic normalization.
- [x] Add context-aware effective settings resolution.
- [x] Apply thread settings to mention, auth, routing, cwd, and visibility.
- [x] Add topic settings API endpoints.
- [x] Add nested topic settings UI and translations.
- [x] Add unit, API, scenario, and UI coverage.
- [x] Update Telegram setup documentation.
- [x] Capture a browser-verified screenshot.
