# Workbench refinements

Status: approved by the owner on 2026-11-01, together with the answers to the
design questions (see "Owner decisions"). Mockups are on the Show Page of the
design Session.

Three refinements to the Workbench:

1. The Inbox separates Bot conversations from ordinary Sessions, and
   notifications can be switched per category.
2. The sidebar's Projects section collapses like Capabilities.
3. Runs of Harness messages in a conversation fold into one group.

Delivery: items 2 and 3 are front-end only and ship now, one PR each. Item 1
waits for the Bots backend (`docs/plans/bots.md`, PR 4 of its delivery plan):
the server work goes to the GPT line, the interface to the Claude line.

## Owner decisions (2026-11-01)

- A category whose notifications are off does not count in aggregate badges.
- The switches belong to the account, not the device.
- Muting a single Bot comes later; the storage reserves room for it.
- The desktop app notifying for Bot replies is the next small step after item 1.
- A Session's first message never folds into a Harness group.
- The preferences live in `state_meta`, not a new table (orchestrator, same day).

## 1. Inbox categories and notification switches

Implement after the Bots backend has merged.

### Current facts

- Unread means an unread Agent `result` row, counted per Session, excluding
  archived and non-foreground Sessions
  (`storage/messages_service.py::unread_counts_by_session`, `total_unread`).
  `vibe/message_types.json` marks `result` as the only `unread` type; `result`
  and `error` as `webPush`; and `notify` with `backend_failure` through
  `webPushWhenEvents`.
- `GET /api/inbox` (`vibe/ui_server.py::inbox_list`) returns the paged feed plus
  `unread_by_session`, `unread_total`, and `unread_sessions`.
- `ui/src/context/WorkbenchInboxProvider.tsx` owns `unreadBySession` and
  `totalUnread`, and mirrors the total to the PWA app badge (`setAppBadge`, plus
  a `vibe.app-badge-current` message to the service worker) and the tab favicon
  (`syncFaviconBadge`). `WorkbenchSidebar.tsx` renders the Inbox entry and
  `InboxHoverPopover`; `InboxPage.tsx` holds the Unread/All filter,
  `WebPushControl`, and mark-all-read.
- Web Push: `core/message_mirror.py` calls
  `core/web_push_notifications.py::maybe_notify_inbox_message` for each new
  Workbench Agent message of a notifiable type. `_send_to_enabled_subscriptions`
  waits 3 s, skips a message already read, resolves the owners from the latest
  user message's `_web_push_user_key(s)`, applies the Personal or Organization
  authorization and the Project policy, then sends to every enabled
  subscription of each owner with `badge_count` from
  `_badge_count_for_user_key`. Subscriptions are per device
  (`storage/web_push_service.py`).
- `ui/public/push-sw.js` shows a notification for every push (iOS requires it,
  so a push cannot be dropped on the device) and refreshes the app badge from
  `/api/inbox?platform=avibe&limit=1` → `unread_total`.
- Desktop app: `desktop/runtime-host/src/notifications.rs` follows
  `/api/events` and raises OS notifications only for `vaults.updated` (a
  pending approval) and `runs.updated` (a background run reaching a terminal
  state); the Dock badge reads `/api/inbox` → `unread_total`. The tray
  "Notifications" item (`desktop/src-tauri/src/notifications.rs`) stores a
  per-shell switch. No OS notification is raised for Agent replies. Contract:
  `docs/plans/desktop-notifications-sse.md`.
- The desktop pet lists Sessions with unread replies
  (`ui/src/pet/usePetReplyActivities.ts`). IM platforms notify through their
  own apps.
- On the owner's machine, foreground Sessions produced 4,139 Agent replies in
  the 30 days before the design, about 138 pushable messages a day.

### Behavior

Categories:

- `bot`: the message's Session is the permanent Session of a current Bot
  (`ses_<handle>` of an enabled Bot).
- `session`: every other Inbox Session, and workspace-addressed failure notices
  that have no Session.
- Bots are owner-only in phase 1, so other members have one category and see
  only the master switch.

Preferences belong to the account (`user_key`: `local` or `remote:<sub>`),
default all on:

| Field | Meaning |
| --- | --- |
| `enabled` | master switch |
| `bots` | Bot messages |
| `sessions` | ordinary Session messages |

A category is **loud** when `enabled` and its own switch are on, otherwise
**quiet**. A quiet message is not pushed and not counted in aggregate badges;
it is still delivered, listed, and unread.

| Path | Category quiet | Master off |
| --- | --- | --- |
| Web Push (every subscribed device) | nothing sent for that category | nothing sent |
| PWA app icon badge, desktop Dock badge, tab favicon | count excludes the category | cleared |
| Sidebar Inbox icon | number counts loud categories; grey dot when only quiet unread remains | grey dot when anything is unread |
| Desktop OS notifications | `run.terminal` for that category's Sessions suppressed; Vault approvals still notify | none |
| Per-conversation counts (Bot avatars, project-tree Session rows, Inbox rows) | unchanged; Inbox rows of a quiet category draw their count grey | unchanged |
| Pet, IM | unchanged | unchanged |

- `badge_total` is the unread sum over loud categories. With the default
  preferences it equals `unread_total`, which keeps the Inbox total of
  `docs/plans/bots.md` D5.
- Vault approvals block on the user and are not messages: they notify while
  `enabled` is on, whatever the category.
- The Web Push test ignores categories; its toast says when a category is
  quiet.
- The tray item stays the device switch for desktop OS notifications, as the
  subscription is for Web Push. The device switch and the account preference
  must both allow a notification.
- Next step after this item: the desktop app raises an OS notification for a Bot
  reply while Bot messages are loud and its window is not focused.

### UI

- Inbox popover and Inbox page: two sections, Bots first, then Sessions. Each
  section header shows its unread count; a quiet section shows a grey count and
  a "Muted" mark. The popover shows up to three Bot rows and three Session
  rows, unread first within each, with the existing "more unread in the full
  Inbox" link per section. A Bot row is avatar, display name, and preview; it
  needs no Bot tag inside its own section. The page keeps Unread/All and adds
  "mark this section read" to the Sessions header.
- `NotificationMenu`: the master switch, Bot messages, Session messages (with a
  one-line note when Sessions are off), this device's push state with Test or
  Enable, and a link to Settings › Notifications. It opens from a bell in the
  popover header (left of "Mark all read") and from a "Notifications: All /
  Bots only / Sessions only / Off" button on the Inbox page toolbar. The bell is
  `Bell` when everything is on, `BellMinus` with a short label when partial, and
  `BellOff` when off. The hover popover stays open while the portaled menu is
  open.
- Settings › Notifications, a new page right after General in
  `ui/src/components/settings/SettingsLayout.tsx`: the same switches with
  one-line explanations, "This device" with the full `WebPushControl` (moved out
  of the Inbox toolbar), and a note that desktop OS notifications are switched
  from the tray menu.
- Sidebar Inbox icon: a cyan number for `badge_total`; a grey dot with no number
  when `badge_total` is 0 and anything is unread.

### Server and API

- Storage: `state_meta` key `notification_preferences:<user_key>`, value JSON
  `{"enabled": true, "bots": true, "sessions": true}`, read and written through
  the existing `get_state_meta` / `set_state_meta` helpers; no migration. A
  missing key or field means on; unknown fields are preserved on write. A later
  per-Bot mute adds `muted_bots: [<handle>, ...]` to the same value.
- `GET /api/notifications/preferences` and `PUT /api/notifications/preferences`,
  scoped to the caller's `user_key` (the key `_web_push_user_key` resolves),
  with CSRF like other writes. A write publishes SSE
  `notifications.preferences.updated`.
- One helper owns categorization, for example a Bot-session-id set read from the
  Bot registry, shared by the Inbox listing, push gating, and badges. The
  category cannot come from the scope: Bot Sessions have none.
- `GET /api/inbox`: each row gains `category`; the response gains
  `unread_by_category` (`{bot, session}`) and the caller's `badge_total`. An
  optional `category=bot|session` filter on `list_inbox_sessions` lets each
  Inbox page section page on its own.
- Web Push: `_send_to_enabled_subscriptions` drops, after authorization and the
  Project policy, the owners for whom the message is quiet, recording a new
  `muted` disposition in the diagnostics ring; `_badge_count_for_user_key`
  returns `badge_total`.
- Service worker: the badge refresh reads `badge_total`, falling back to
  `unread_total` for an older server.
- Desktop runtime-host: the Dock badge reads `badge_total` (falling back to
  `unread_total`). For `run.terminal`, the run detail it already fetches gains
  `notification_category`, and the host reads preferences from
  `GET /api/notifications/preferences` on connect and on
  `notifications.preferences.updated`. This revises the frozen contract in
  `docs/plans/desktop-notifications-sse.md` and ships as its own PR after the
  server part.

### Code that changes

- Server: `storage/messages_service.py` (category split and filter),
  `vibe/ui_server.py` (`/api/inbox` fields and preference routes),
  `core/web_push_notifications.py`, a small preference service over
  `state_meta`, and the Bot-session helper.
- Web: `WorkbenchInboxProvider.tsx` and `WorkbenchInboxContext.ts` (expose
  `unreadByCategory`, `badgeTotal`, and the preferences; the app badge, favicon,
  and worker message use `badgeTotal`), `WorkbenchSidebar.tsx` (popover sections
  and bell), `InboxPage.tsx` (sections and notification button;
  `WebPushControl` leaves the toolbar), a new `NotificationMenu.tsx`, a new
  `settings/SettingsNotificationsPage.tsx` with its route,
  `ui/public/push-sw.js`, and `en`/`zh` strings.
- Desktop: `desktop/runtime-host/src/notifications.rs`.
- Tests: push gating per category and master switch, badge totals per
  preference, the `/api/inbox` fields, the worker fallback, section rendering,
  menu state, and desktop host filtering against a fake server.

## 2. Collapsible Projects

### Current facts

- In `WorkbenchSidebar.tsx`, Capabilities is a header button (label on the
  left, chevron on the right, turned `-90deg` when closed) over a `nav`. Its
  state is `useState(true)`, so it reopens on every reload.
- The Projects header has the label on the left and borderless Search and New
  project icons on the right, and does not collapse.
- Per-browser UI memory follows `ui/src/lib/agentsViewMemory.ts`: versioned
  `avibe.*` keys and best-effort `localStorage`.

### Behavior

- Projects collapses like Capabilities. Header: the label (toggle), Search, New
  project, and the chevron (toggle). Both sections' chevrons sit at the right
  edge in one column.
- Collapsed, the header keeps Search and New project; the list is hidden and the
  footer stays pinned. The header shows no unread or running summary: the Inbox
  icon and the Bot avatars carry unread.
- Creating a project while the section is collapsed opens it, so the new
  project is visible. Navigation never opens it otherwise.
- Both sections' open state persists per browser under `localStorage` key
  `avibe.sidebar.sections.v1` as `{"capabilities": true, "projects": true}`.
  Both default to open; a missing, unknown, or corrupt value reads as open.
- The mobile `ProjectsPage` is unchanged.

### Code that changes

- `WorkbenchSidebar.tsx`: the Projects header becomes a row of a toggle button
  over the label, the two icon buttons, and a chevron toggle button, with
  `aria-expanded` and `aria-controls`; Capabilities reads and writes the same
  memory.
- A new `ui/src/lib/sidebarSectionsMemory.ts` with tests, and sidebar tests for
  collapsing, persistence, and New project reopening the section.

## 3. Auto-collapse Harness messages

### Current facts

- A Harness row is the `isHarness` branch of `MessageRow` in
  `ui/src/components/workbench/ChatPage.tsx`: a cyan `RoleAvatar` with a clock,
  the source label from `harnessChipLabelKey` (`ui/src/lib/chatTrigger.ts`), and
  a dashed chip with a one-line preview that expands per row. The render kind
  `harness` covers the `harness` and `vault` types.
- `Transcript` renders the loaded messages in order and places Activity chips
  from `activity.beforeAnchor` and `activity.afterAnchor`.
- A Turn that ends without a reply writes no visible row. If it had activity,
  its group anchors `after` its trigger
  (`storage/agent_activity_service.py::_build_groups`); otherwise it leaves
  nothing.
- Across every Web Session on the owner's machine at design time: 33,628
  Harness inputs against 12,535 user messages; 4,960 runs of two or more held
  20,304 of them (60%); folding leaves 18,284 Harness items (46% fewer); 73% of
  runs ended with an Agent reply; 45% mixed sources; runs lasted about 52
  minutes on average and 323 lasted over two hours; 371 runs started at the
  Session's first message; about half of the inputs inside runs were followed
  by Activity.

### Rule

- A groupable row is a transcript row that renders as `harness`, except the
  Session's first message when the loaded window starts at the beginning of the
  Session (no older page and index 0).
- A run is two or more groupable rows with nothing visible between them except
  Activity chips anchored `after` a member of the run (Turns that did not reply,
  including interrupted ones).
- Anything else visible breaks a run: user rows, annotation cards, and Agent
  rows of any kind (replies, mid-Turn `output`, `interim`, status and failure
  notices, errors). A chip anchored `before` a breaking reply belongs to that
  reply and stays outside.
- Time gaps and source kinds do not break a run. A single groupable row renders
  as today.
- A pure function, `groupHarnessRuns`, turns the loaded messages into
  transcript items: a single message, or a Harness group with its ordered
  members.

### Look

- Collapsed, the default: a header with a cyan `RoleAvatar` (layers icon),
  "N Harness messages", per-source counts using the existing source labels,
  and the first and last times; below it a dashed chip stacked over two offset
  sheets, showing "Latest", the latest member's source, and its one-line
  preview. The whole chip toggles.
- Expanded: the same header with "Collapse"; the members in their existing
  rendering, with their Activity chips, on a cyan left rail; a trailing collapse
  link.

### Behavior

- Live: a group at the tail grows in place. The count and the latest preview
  update; a collapsed group stays collapsed and an expanded one appends. The
  running Activity card stays at the transcript tail, outside the group. If
  that Turn ends without a reply its chip joins the group; if it replies, the
  reply closes the group.
- A single row that becomes a run of two forms a collapsed group, unless that
  row was expanded, in which case the group starts expanded: what the reader is
  reading is never folded away, and the content around it does not jump.
  Closing that row afterwards keeps the group open; closing the group closes its
  rows.
- Unread: Harness inputs are never unread (only `result` is), so groups carry no
  unread mark and change no counts. Opening a Session still lands at the tail.
- Scroll: toggling keeps the group header in place (the existing anchor logic);
  a reader following the tail stays pinned; a jump or search target inside a
  group expands that group before scrolling and highlights the row; older pages
  prepended above can grow a group at its top. A group keeps its element as it
  grows at either end, so a scroll anchor held on it survives, and the anchor
  search accepts a row that stands for several messages
  (`data-transcript-row`). The anchor also names the message it holds, so a
  row that an older page turns into a group member keeps its place: it is found
  again inside the opened group, or the collapsed head takes its place
  (`data-harness-members`). Closing a group from below its head (its trailing
  control) brings the head back to the top of the view, since the anchored row
  went with the members.
- State: the set of expanded member ids and the set of opened Harness rows, in
  memory for the open Session. A group is expanded when any member is in the
  first set, so growth at either end keeps its state. Nothing is persisted.
- Web transcript only: search, copy, Retry eligibility, the API, and IM are
  unchanged.

### Code that changes

- `ChatPage.tsx` `Transcript`: render items instead of raw messages; an open
  group draws its members, with their Activity chips, through the same row
  rendering as any other row; expand on jump.
- `ui/src/lib/harnessGroups.ts`: `groupHarnessRuns` and
  `stableHarnessGroupKeys`, with unit tests for the break rules, the
  first-message rule, and key stability.
- A new `HarnessGroup` component for the group head and the collapsed stack;
  `ui/src/lib/transcriptScrollAnchor.ts` accepts `data-transcript-row`;
  `en`/`zh` strings.
