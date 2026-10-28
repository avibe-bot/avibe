# Single-scope settings edits

## Background

The Web settings pages for groups and DM users saved by posting their whole
in-memory copy back: every channel of a platform (`POST /api/settings`) or a
whole user row (`POST /api/users`). The server treated that copy as the truth,
so any edit from a page opened earlier:

- deleted channels another writer had enabled since the page loaded;
- reverted other fields and other channels to the page's old values;
- restored a user's admin or enabled flag that had been revoked in the meantime.

The only optimistic-concurrency check (`expected_agent_name`) covered the Agent
binding alone, and its token went stale after the page's own first autosave.

IM settings writes had a separate gap: `SettingsManager._save_settings` logged
and swallowed a refused save, so IM forms confirmed changes that never landed.

## Contract

`PATCH /api/settings/scope` (Member role, same access-policy checks as the
existing settings writes):

```json
{
  "platform": "lark",
  "scope": "channel | thread | user",
  "id": "<channel or user id>",
  "thread_id": "<topic id, thread scope only>",
  "changes": { "<field>": "<value>", "routing": { "<routing field>": "<value>" } },
  "expected_agent_name": "<required with a routing change>"
}
```

- Fields: channels and Telegram topics take `enabled`, `show_message_types`,
  `custom_cwd`, `require_mention`, `require_bind`, `routing`; DM users take
  `display_name`, `enabled`, `show_message_types`, `custom_cwd`, `routing`.
  Admin rights keep their dedicated endpoint. Unknown fields are refused (400).
- Only named fields are written. `routing` merges key by key into the stored
  route, and the result goes through the same Claude effort normalization as
  the existing save paths.
- The change is applied to the rows read under the SQLite write lock
  (`SettingsStore.apply`), so nothing the caller loaded earlier is written back.
- `expected_agent_name` is the Agent the caller's view of the route was based
  on. If the stored Agent differs, the change is refused with
  `settings_conflict` (409), unless it sets the stored Agent itself. A new
  binding must name an enabled Agent (`agent_unavailable`).
- A channel or topic without a row is created; a topic starts from its parent
  channel's stored settings and materializes the platform mention default, as
  the existing topic save did. A missing DM user is `user_not_found` (404).
- The response carries the stored row in the shape the settings reads return.

The bulk endpoints remain for the setup wizard and existing callers; the
settings pages no longer use them.

## Web pages

Users, groups, and Telegram topics send each edit as a diff of the row before
and after it, with the routing view's Agent as `expected_agent_name`. The
stored row in the response replaces the local one unless a newer edit of the
same scope is in flight; a refused edit reloads that platform's rows.

## One write path

`core.services.scope_settings.apply_scope_change` owns the change; the Web
PATCH route validates the request and calls it, and every IM settings write
reaches it through `SettingsManager.change_scope_settings`:

| IM surface | Change |
| --- | --- |
| routing form (all platforms), resume | `routing`, as the form's complete route |
| settings form | `show_message_types`, plus `require_mention` outside DMs, in one write |
| message-type toggle | `show_message_types` |
| set working directory | `custom_cwd` |

- Routes go through one `normalize_scope_routing`, so a Claude effort is kept
  or dropped by the Model Hub catalog when Claude runs through the Hub, whichever
  surface saved it. IM previously checked only the native catalog.
- An IM routing form submits a complete route; its `expected_agent_name` is the
  Agent the scope runs when the form is saved, so only a concurrent change in
  that window conflicts.
- A topic's first override keeps the mention gate it showed while inheriting:
  the parent channel's explicit gate, else the platform default.
- A refused save raises to the IM handler, which reports it with the same
  localized conflict or unavailable-Agent text as the Web pages; the runtime
  view is rebuilt from the committed rows.
- IM handlers run these writes on a worker thread, not the controller loop.
- Runtime bookkeeping on bound users (Slack DM channel id, WeChat bind hint)
  uses `SettingsStore.update_user_fields`, which sets only those fields under
  the write lock instead of writing back a cached copy of the user.

## Out of scope

- Removing the legacy per-backend `<backend>_model` / `<backend>_reasoning_effort`
  routing fields from the API, the Web types, and the stored routing JSON. They
  are read-only aliases today; removing them is a separate contract cleanup.
- Display fixes on the settings pages (effective model, inherited vs override,
  new-session hint) and per-row save status.
