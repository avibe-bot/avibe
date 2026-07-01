# Fix: Deleted/stale channels keep showing in Group Settings

## Background

A Discord channel that was deleted on the platform still appears in the Web UI
**Group Settings → Groups** list. Investigation shows this is a shared-core gap,
not a Discord-only bug.

Data flow today (`core/chat_discovery.py`):

1. On `refresh_platform()` (Rescan), live channels are fetched via
   `_fetch_platform_channels()`. Every returned id goes into `seen_ids` and is
   upserted with `visibility_status = "visible"` (lines 469–498).
2. `_mark_not_returned()` (lines 740–769) walks the persisted `scopes` for that
   platform and, for any row **not** in `seen_ids`, soft-marks it
   `visibility_status = "not_returned"` plus `last_missing_at`. Rows are **never
   physically deleted**.
3. `channels_response()` (lines 585–648) calls `list_chats(..., include_not_returned=True)`
   and `_filter_response_chats()`. That filter only drops `not_returned` when
   `require_member=True`, which is **Slack-with-membership only**. For Discord and
   Lark (`require_member=False`) `not_returned` rows are returned verbatim.
4. The UI (`ui/src/components/steps/ChannelList.tsx`) never reads
   `visibility_status`; its "Show inactive" toggle filters by `config.enabled`
   (enabled/disabled), not by platform existence. So deleted channels always show
   and there is no way to remove them.

Per-platform audit:

| Platform | Live list API | Pagination handled | 429/backoff | `_mark_not_returned` | Deleted-still-shows |
| --- | --- | --- | --- | --- | --- |
| Slack | `users_conversations` / `conversations_list` | yes (cursor loop) | yes (5x backoff) | yes, `require_member` gated | yes |
| Discord | `GET /guilds/{id}/channels` (+ guild list) | channels: single call (complete); **guild list `users/@me/guilds` not paginated** | **no** | yes (`require_member=False`) | yes |
| Lark/Feishu | `GET /im/v1/chats` | yes (`page_token`, `max_pages=50` cap → `truncated`) | **no** | yes (`require_member=False`) | yes |
| Telegram | none (passive, message-driven) | n/a | n/a | **never called** | yes (cannot auto-detect deletion) |
| WeChat | none (no channel concept) | n/a | n/a | n/a | n/a (UI short-circuits) |

Key correctness coupling: hiding `not_returned` is only safe if we never
`_mark_not_returned` on an **incomplete** fetch (transient 429, truncated page).
`_mark_not_returned` already runs only after a fetch that did not raise — but it
runs inside the same transaction that has already upserted the seen rows, so a
fetch that *succeeds but is partial* (Lark `truncated`) will still mark the unseen
(but actually-live) rows `not_returned`. So P0 (hide) and P1 (never false-mark)
must ship together.

Note: Discord's per-guild channel fetch (`GET /guilds/{id}/channels`) returns the
whole channel set in one call, and `_fetch_platform_channels` for Discord already
requires a `guild_id`. The paginated `users/@me/guilds` endpoint is only used by
the setup-wizard guild picker and has **no** effect on `_mark_not_returned`
correctness — it is a UX item, not a P1 correctness item.

## Goal

1. Deleted/inaccessible channels stop appearing in the Groups list by default
   (the reported bug), across all active-refresh platforms (Slack, Discord, Lark).
2. Users can still review and manually remove stale entries (covers Telegram /
   WeChat and any soft-marked row), with a clear visual state.
3. Eliminate false `not_returned` marking caused by partial/rate-limited fetches.

Non-goals: redesigning discovery; automatic background physical deletion (left as
an explicit, optional follow-up); changing the meaning of the existing
"Show inactive" (enabled/disabled) toggle.

## Solution

### P0 — Hide `not_returned` from the Groups list by default (root-cause)

Backend (`core/chat_discovery.py`):

- Add `include_not_returned: bool = False` parameter to `channels_response()`.
  **`all_chats` keeps `list_chats(..., include_not_returned=True)` unchanged** (it
  must stay the full inventory for accurate counts). Apply the filter only to the
  returned `chats` view — extend `_filter_response_chats()` to drop
  `visibility_status == not_returned` whenever `include_not_returned` is `False`
  (for all platforms, not just `require_member`). This is the central, platform-
  agnostic fix, so every active-refresh platform inherits it.
- `_summary()` derives counts from the unfiltered `all_chats`:
  `discovered_count = len(all_chats)` (unchanged) and a new
  `not_returned_count = len([c for c in all_chats if c.visibility_status ==
  VISIBILITY_NOT_RETURNED])` — no extra DB query.
- **Fix the refresh-trigger guard** at `channels_response()` line ~623. Today it
  is `force or not chats or cache_requires_refresh`. After P0, `chats` excludes
  `not_returned`, so an install whose channels were all deleted would have
  `chats == []` on every request and re-refresh on every load (bypassing the TTL,
  which only lives inside `refresh_platform`). Replace the `not chats` signal with
  a "never successfully fetched" signal: `force or state.last_success_at is None or
  cache_requires_refresh`. The background `should_refresh()`/`_schedule_refresh`
  path (line ~635) already handles staleness via TTL and is unchanged.
- Keep the existing `require_member` + `not_returned` check in
  `_filter_response_chats()` as defense-in-depth (harmless once the new default
  filter is in place; protects callers that pass an unfiltered list).
- API surface: `vibe/api.py` `list_channels` / `discord_list_channels` /
  `lark_*` / `telegram_list_chats` and the UI endpoint gain an opt-in
  `include_not_returned` (default `False`) so a power-user view can still fetch
  stale rows for review/removal.

UI (`ui/src/components/steps/ChannelList.tsx`):

- By default render only channels the backend returns (now excludes
  `not_returned`). No behavior change needed if backend defaults to hiding.
- Add a small affordance ("N unavailable — review") that, when toggled, requests
  `include_not_returned=true` and renders those rows with a distinct
  "Unavailable / removed on platform" badge derived from `visibility_status`
  (+ `metadata.last_missing_at`), visually separate from Enabled/Disabled.

### P0b — Manual remove (physical delete of a scope)

- New API + UI action to permanently delete a discovered scope (the `scopes` row
  plus its `scope_settings`). Reuses the existing scope-id helpers; add a focused
  `delete_scope()` in the discovery/store layer.
- This is the only deletion path that also works for Telegram (no live list) and
  is the user-facing escape hatch. Confirm-before-delete in UI.
- FK note: `scope_settings.scope_id` is `ON DELETE CASCADE` and
  `scopes.parent_scope_id` is `ON DELETE SET NULL` (`storage/models.py`). SQLite
  enforces cascade only when `PRAGMA foreign_keys = ON`. `delete_scope()` must
  therefore explicitly delete the `scope_settings` row(s) first (do not rely on
  the pragma), then the `scopes` row. Deleting a guild-type scope nulls the
  `parent_scope_id` of its child channel scopes (SET NULL) — correct behavior.

### P1 — Never false-mark on incomplete fetches (robustness)

The single safety-critical change here is: **`refresh_platform()` must skip
`_mark_not_returned()` when the fetch was incomplete**, while still upserting the
rows it did get. Because both the foreground and background (`_schedule_refresh`)
paths call `refresh_platform()`, changing it there covers both.

`core/chat_discovery.py`:

- Extend `_fetch_platform_channels()` return from
  `tuple[list[dict], str | None]` to `tuple[list[dict], str | None, bool]` where
  the new bool is `fetch_complete`. Update the single destructure at line ~454
  (`rows, refreshed_parent = ...`). Values: Slack `True` (cursor loop exhausts),
  Discord `True` (single complete call), Lark `not result.get("truncated", False)`.
- In `refresh_platform()`, guard the `_mark_not_returned(...)` call (line ~499) on
  `fetch_complete`; when incomplete, log and skip marking (rows still upserted).

`vibe/api.py`:

- Lark (highest priority): the `truncated` flag already exists in the
  `lark_list_chats_live` return dict (line ~9148); `_fetch_platform_channels` just
  reads `result.get("truncated", False)`. Add 429/5xx retry/backoff to the Lark
  fetch so transient limits don't surface as partial success.
- Discord (hygiene, lower priority): add 429/5xx retry with backoff to
  `_discord_api_get` / `_discord_api_get_async` (honor `Retry-After`), mirroring
  the Slack helper. Per-guild channel fetch rarely hits 429; this is defensive.

(`users/@me/guilds` pagination is a UX item — see P4 below — not part of this
correctness fix.)

### P2 — Optional auto-GC (follow-up, gated on P1)

- Physically delete rows that have been `not_returned` for a grace window
  (e.g. ≥ N consecutive successful refreshes or ≥ X days via `last_missing_at`),
  configurable and off by default. Documented as a separate PR; not implemented
  in this change unless review requests it.

### P3 — Telegram / WeChat

- Telegram: no server list to diff, so deletion cannot be auto-detected. Rely on
  P0b manual remove; optionally surface a "last seen" staleness hint. No
  auto-marking.
- WeChat: unchanged (no channel concept; UI already short-circuits).

### P4 — Discord guild-list pagination (UX, separate follow-up)

- Paginate `users/@me/guilds` with the `after` cursor in `discord_list_guilds*`
  so the setup-wizard guild picker is complete for bots in >100–200 guilds. This
  does **not** affect `_mark_not_returned` correctness and is out of scope for the
  primary fix; documented here so it is not lost.

## Testing

- Unit (`tests/`): extend chat_discovery tests with a hermetic SQLite
  `db_path` to assert:
  - a row marked `not_returned` is excluded from `channels_response()` by default
    and included when `include_not_returned=True`;
  - `_summary()` still counts it under `discovered_count` + new
    `not_returned_count`;
  - a truncated/partial fetch upserts seen rows but does **not** mark missing rows
    `not_returned` — covering **both** the foreground `refresh_platform()` call and
    the background `_schedule_refresh()` → `refresh_platform()` path (mock a
    truncated Lark result in each);
  - after P0, an install whose channels are all `not_returned` does not trigger a
    refresh on every `channels_response()` call (assert the `last_success_at`-based
    guard, not `not chats`);
  - `delete_scope()` removes the scope and its `scope_settings` (assert the
    explicit child-delete works regardless of the SQLite FK pragma).
- API: unit-test Discord 429 retry and guild-list pagination, and Lark `truncated`
  propagation, with mocked HTTP (no live calls; keep hermetic per CLAUDE.md).
- UI: `cd ui && npm run build`; add/extend a ChannelList test if a pattern exists
  for the unavailable-rows view.
- Manual: Incus regression for cross-platform sanity (Slack/Discord/Lark) per
  CLAUDE.md, after CI-style local checks.

## Rollout / risk

- Behavior change: deleted channels disappear from the default list. Mitigated by
  the opt-in "unavailable" view + manual remove, and by P1 preventing false hides.
- No destructive DB migration; soft-mark semantics preserved. Physical deletion
  only via explicit user action (P0b) or optional P2.
- Scope of this PR: P0 + P0b + P1 (+ tests). P2 is a documented follow-up.

## TODO

- [ ] P0: `include_not_returned` param on `channels_response` (default `False`);
      keep `all_chats` unfiltered, filter only `chats` via `_filter_response_chats`;
      add `not_returned_count` to `_summary`; fix refresh-trigger guard to use
      `state.last_success_at is None` instead of `not chats`; thread the opt-in
      through `vibe/api.py` + UI endpoint.
- [ ] P0b: `delete_scope()` store helper (explicit `scope_settings` delete then
      `scopes`) + API endpoint + UI remove action with confirm.
- [ ] P1: `_fetch_platform_channels` returns `fetch_complete` (3-tuple), update
      destructure at line ~454; `refresh_platform` skips `_mark_not_returned` when
      incomplete (covers foreground + background paths); Lark `truncated`
      propagation + Lark 429 retry; Discord 429 retry (hygiene).
- [ ] UI: unavailable-rows view + badge from `visibility_status`/`last_missing_at`.
- [ ] i18n: add new UI strings to `ui/src/i18n/en.json` and `ui/src/i18n/zh.json`.
- [ ] Tests: chat_discovery unit (filter default, summary counts, refresh-guard,
      truncation skip in fore+background, delete_scope), API retry/truncation unit,
      `cd ui && npm run build`.
- [ ] Update user docs for the unavailable/remove behavior.
- [ ] P4 (separate follow-up): `users/@me/guilds` cursor pagination for the guild
      picker.
