# Desktop notifications over Workbench SSE

## Status

Frozen implementation contract for gap G4. Companion to
`desktop-product-gaps.md` (G4) and `desktop-start-receipt.md` (ownership of
the Runtime is already settled; this contract does not reopen it).

Owner: Avibe core
Date: 2026-09-10
Baseline: `desktop` branch after #1975 / #1977 (tray + start-receipt).

## Product outcome

When the Avibe window is unfocused or closed (tray still alive), the user
gets a native OS notification for the two classes of event that actually
need them to look:

1. A Vault request that is waiting on them (`approval.requested`).
2. A background agent run that has reached a terminal state
   (`run.terminal`), only if the run was clearly backgrounded or ran
   longer than ~30 seconds.

Default on. The tray has a checkable "Notifications" item that persists
the preference. System Focus / Do Not Disturb is left to the OS; Avibe
does not reimplement it.

Clicking a notification focuses the Avibe window. Deep-link navigation
into a specific session (`avibe://session/<id>`) is G5 and is **not**
in this slice — the click is "come back to Avibe", not "open this row".

## Why SSE, not polling

The shell already probes `GET /ready` every two seconds after navigation;
that loop is the liveness signal ("is the Runtime still there?").
Attention ("something needs you") is a different job. Polling it off
`/ready` would:

- bound notification latency to the probe period (bad for blocking
  Vault approvals);
- tax a tray-resident Agent OS with permanent empty-cycle HTTP;
- fork the product's event model: Workbench already consumes SSE,
  desktop notifications would pull.

v1 therefore **reuses the existing event source** (`GET /api/events`)
and leaves `/ready` as liveness-only.

## Frozen transport

### Endpoint

`GET {origin}/api/events` on the adopted Runtime origin (the same
loopback origin the shell already validated and navigated to).

No new HTTP route. The Workbench EventSource client at
`ui/src/context/ApiContext.tsx` already opens this path with no extra
auth on loopback; the shell does the same. Adding a cookie or token
requirement to `/api/events` is a breaking change for both clients and
is out of scope.

SSE framing already in production (`vibe/ui_server.py::workbench_events`):

- handshake: `: stream connected` comment, then `event: connected`;
- 15-second keep-alive comment (`: ping`) so proxies do not idle-kill;
- each application event: `event: <type>` + `data: <json>` where the
  JSON is `{"type": <type>, "data": <payload>}` (see `sse_broker.py`).

The shell parses this framing; it does not invent a second protocol.

### Connection ownership

The **shell** owns the SSE connection, not the WebView.

- Opened once the Runtime is adopted/started and `/ready` has succeeded.
- Survives window close (the tray is the user's handle on a living
  Runtime; notifications must still arrive).
- Torn down when the shell gives up on the Runtime: three consecutive
  `/ready` failures (existing bootstrap recovery), explicit Stop of an
  owned Runtime, Uninstall, or Quit-and-stop. Adopted Runtimes that
  outlive the shell keep running; the SSE dies with the shell, which
  is the same visibility the user already accepted by quitting.
- Reconnect with bounded backoff on stream drop, independently of the
  `/ready` probe. A reconnect does not replay history (the broker has
  no replay); the shell must tolerate missed events. Vault pending
  state is recoverable on click by opening the Workbench.

`/ready` probing and the SSE are two loops with one shared origin.
Neither substitutes for the other: a live SSE with a failing `/ready`
still triggers bootstrap recovery; a live `/ready` with a down SSE
retries the stream and does not take the window back to bootstrap.

### Filtering, not a second bus

The broker is a global fan-out. The shell **subscribes to the existing
stream and filters**. It does not ask Python for a dedicated attention
channel in v1. Adding `GET /api/desktop/attention` would be a contract
v2 if the filter ever becomes a product-logic burden the shell should
not carry.

v1 filter (exact event type strings, already published):

| SSE `event`        | Payload fields used                         | Notification class    | When it fires                          |
| ------------------ | ------------------------------------------- | --------------------- | -------------------------------------- |
| `vaults.updated`   | `request_id`, `request_status`              | `approval.requested`  | `request_status == "pending"` and `request_id` present |
| `runs.updated`     | `run_id`, `status`, `session_id`, `run_type`| `run.terminal`        | `status` in `{succeeded, failed, canceled}` AND the run is "background" (see below) |

Every other event (`message.new`, `session.activity`, `queue.updated`,
`inbox.unread.changed`, `show.event`, keep-alives, unknown types) is
ignored by the notification path. The filter is an allow-list: a new
event type never becomes a notification by accident.

### "Background" for `run.terminal`

A terminal `runs.updated` notifies iff **at least one** of the two
properties below holds. This is not an allow-list of product verbs
and is not extended in v1 (a new always-background kind is a contract
revision). `run_type=agent_run` — including default async
`vibe agent run` — is not a special case: it follows the duration
rule only. v1 does not add `visibility` to the SSE payload.

A terminal `runs.updated` notifies if **either**:

1. the run's wall time since it **started** is ≥ 30 seconds. The
   clock is the run's durable `started_at`, not "when this shell first
   saw a `running` event" — the broker has no replay, so attaching
   mid-flight or reconnecting can miss that transition. Terminal
   `runs.updated` events therefore include `started_at` (ISO-8601). If
   a terminal event omits it, the shell refetches `GET /api/harness/runs/<run_id>`
   once and reads `started_at` there before applying the threshold.
   A missing timestamp after refetch fails closed (no notify) rather
   than treating "first seen at terminal" as zero duration.
   or
2. the run was started in a way the product already treats as
   background. Stored `run_type` values (from `core/scheduled_tasks.py` /
   `core/watches.py`) are `scheduled` and `watch` — not the user-facing
   definition kind `task`. Notify when `run_type` is in `{scheduled, watch}` even under 30s.

The duration clock is durable `started_at`, not SSE observation time.
Python does not grow a "please notify" flag or a `visibility` field in v1.

### Dedup

- `approval.requested`: key = `request_id`. The same pending request
  is never notified twice per shell lifetime. Status transitions away
  from `pending` drop the key so a later re-open can notify again.
- `run.terminal`: key = `run_id`. Terminal is once per key while the
  key is retained. Retention is **bounded** (LRU or TTL, frozen
  default: 512 entries or 24 hours, whichever hits first). The bound
  only needs to cover repeated terminal publications of the same run
  (SSE bursts / reconnect duplicates), not process lifetime. A
  tray-resident shell must not grow this set without bound.

A notification is suppressed (not queued) when:

- the Avibe window is focused and visible; or
- the tray "Notifications" item is unchecked.

System DND / Focus is the OS's problem.

## Frozen presentation

### Copy lives in the shell, keyed by class + locale

Existing SSE payloads are refetch hints (`run_id` / `request_id`), not
finished user copy. The shell therefore owns notification title/body,
using the same locale catalog pattern as the tray (`sys-locale` →
`desktopBootstrap` keys in `ui/src/i18n/{en,zh}.json`, verified by
`npm run test:i18n`).

v1 keys (names frozen; wording is i18n content, not contract):

- `desktopBootstrap.notifications.approvalRequested.title`
- `desktopBootstrap.notifications.approvalRequested.body`
- `desktopBootstrap.notifications.runSucceeded.title`
- `desktopBootstrap.notifications.runSucceeded.body`
- `desktopBootstrap.notifications.runFailed.title`
- `desktopBootstrap.notifications.runFailed.body`
- `desktopBootstrap.notifications.runCanceled.title`
- `desktopBootstrap.notifications.runCanceled.body`
- `desktopBootstrap.notifications.toggle` (tray menu label)

Bodies may include the `session_id` / `run_id` as a short suffix when
present; they must not interpolate unsanitized Runtime strings into
the OS notification (the payload is untrusted process output on its
way to a privileged surface, same rule as refused origins).

### Plugin and permission

`tauri-plugin-notification` on the shell. Permission prompts are the
OS's (macOS notification permission on first fire). The plugin is
invoked from Rust, never from the WebView.

**No new IPC command, no capability widening.** Notifications are
shell-privileged the same way the tray is: the Workbench page cannot
trigger them. `shell_boundaries.rs` gains an assertion that the
notification path does not grant any `remote` capability.

### Click

Click = `show` + `unminimize` + `set_focus` on the main window.
No Workbench navigation in v1 (that is G5). If the window was hidden
to tray, this is the same path as the tray "Open Avibe" item.

## Frozen architecture split

```
Python Runtime                         Shell (Rust)
──────────────                         ────────────
SSEBroker ──GET /api/events──►         attention filter
  vaults.updated                       (allow-list)
  runs.updated                         + 30s / run_type rule
  (everything else ignored)            + dedup
                                       + focus/pref gate
                                       └── tauri-plugin-notification
/ready probe (existing, 2s)  liveness only, never attention
```

Python changes in v1: include `started_at` on terminal `runs.updated`
payloads (`run_updated_payload` / `publish_run_updated`). The two
event types already flow to `/api/events`; this is an additive field
on an existing payload, not a new route. Interactive Workbench refetch
consumers ignore unknown fields.

Rust changes live in `desktop/runtime-host` (filter, dedup, reconnect
— Tauri-free, testable) and a thin `src-tauri` adapter that fires the
plugin and reads window-focus / tray-pref state.

## Tests (invariants, not enumerations)

- Filter allow-list: given a stream that mixes ignored types with one
  `vaults.updated {request_status: pending, request_id}` and one
  terminal `runs.updated`, only those two classes produce a
  notification intent. A newly invented event type in the same
  fixture must not.
- Dedup: two identical `request_id` pendings → one intent; a
  non-pending transition then a new pending with the same id → a
  second intent.
- Background rule (property): a short `agent_run` / missing `run_type`
  with `started_at` < 30s → no intent; the same row with `started_at`
  ≥ 30s → intent; `run_type=scheduled` (or `watch`) → intent even
  when < 30s. Mid-flight attach uses `started_at`, not first-seen.
  Terminal event without `started_at` and a refetch that also lacks
  it → no intent. Adding a new run_type to the fixture must not
  notify unless it is `scheduled`/`watch` or the duration rule hits.
- Focus gate: window focused + visible → no intent even on a matching
  event.
- Reconnect: stream drop then restore does not panic, does not
  replay, does not take bootstrap recovery by itself.
- Boundary: `shell_boundaries` still forbids remote capabilities;
  notification code is not reachable from a navigated Workbench page.
- i18n: the new keys exist in both catalogs with matching
  placeholders (`npm run test:i18n`).

Drive the filter with a fake SSE collaborator under tokio's virtual
clock (same pattern as `runtime-host/tests/bootstrap.rs`) so the 30s
threshold is exercised in microseconds.

## Explicit non-goals (v1)

- A new HTTP route or a Python "please notify" flag.
- Deep-link click targets (G5).
- Notifying on every chat message, every `session.activity`, or
  foreground turns that finish quickly.
- Replaying missed events after reconnect.
- Changing `/api/events` auth.
- Windows-vs-macOS copy differences beyond the shared catalog.

## Sequencing

Depends on the tray (#1975, merged): the Notifications toggle lives
on the same menu as "Start at Login", and window-hidden-to-tray is
the state in which notifications matter.

Does not depend on G1 signing or G2 auto-update.

Does not block G5; G5 should reuse the same notification click
once deep links exist (swap `show window` for `show + navigate`).
