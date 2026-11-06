# PWA Web Push for Workbench Inbox · Design Doc

> **Branch**: `feature/pwa-web-push-plan`
> **Status**: Implementation in progress
> **Owner**: cyhhao

## 1. Goal

Add a first-class PWA Web Push notification path for the Workbench Inbox.

The product goal is not to replace the existing in-app realtime feed. It is to
reach the user when the Vibe Remote Web UI is installed as a mobile PWA and the
page is not actively open. The active Web UI should continue to use the
existing SSE realtime channel.

This plan intentionally excludes email, IM, and native app push channels.

## 2. Platform Feasibility

PWA Web Push can reach the iOS notification shade when the following conditions
are true:

- iOS/iPadOS is 16.4 or newer.
- The user has added the Web App to the Home Screen.
- The user opens the app from the Home Screen.
- The user grants notification permission from a direct user gesture.
- A registered service worker receives push events and calls
  `showNotification()`.

The same implementation also covers Chromium/Android and modern desktop
browsers through the standard Push API.

Important iOS constraints:

- Ordinary Safari tabs are not enough for iOS notification delivery. The app
  must be a Home Screen Web App.
- Permission prompts must be initiated by a user action.
- Safari does not provide a useful invisible-push path for our use case. The
  service worker should show a notification for every delivered push event.
- Notifications are tied to each installed web app's origin. A subscription
  should therefore be treated as a per-device/per-install endpoint, not a
  global user preference.

## 3. Current Code Facts

### PWA shell

The UI already ships a PWA manifest and iOS standalone metadata:

- `ui/public/manifest.webmanifest`
- `ui/index.html`

The HTML currently says "No service worker on purpose" so frontend assets stay
network-fresh. That means the app is installable but cannot yet receive Web
Push.

### In-app realtime

Workbench realtime is already implemented and should stay as the active-page
transport:

- Frontend opens one `EventSource('/api/events')`.
- `WorkbenchInboxContext` owns the shared inbox cache.
- `inbox.session.updated` upserts/re-sorts a session card.
- `inbox.unread.changed` refreshes unread state after mark-read.
- The UI server exposes `/api/events` as an SSE stream with keep-alive pings.
- `vibe.sse_broker.SSEBroker` fans events out to browser subscribers.
- Controller-side agent replies publish through `core.inbox_events.bus`,
  `GET /internal/events`, and `vibe.inbox_bridge` into the UI server broker.

SSE is an in-memory fan-out, not a persistent event log. It is correct for
open-page realtime and should not be used as the mobile background notification
mechanism.

## 4. Boundary Decision

Keep two separate capabilities:

| Capability | Runtime state | Transport | Responsibility |
| --- | --- | --- | --- |
| Active Web UI realtime | Page open | SSE `/api/events` | Update chat, inbox list, unread counts |
| User reachability | Page closed/backgrounded | Web Push | Show OS notification and badge |

The Web Push path should trigger from durable message/inbox write paths, not
from browser SSE subscription state. A missing or disconnected browser should
not affect whether a notification is sent.

## 5. Data Model

Add a SQLite table for PWA push subscriptions.

Proposed table: `web_push_subscriptions`

| Column | Purpose |
| --- | --- |
| `id` | Stable local UUID / token |
| `user_key` | Local authenticated user key, or `"local"` when no remote user is known |
| `endpoint` | Push endpoint URL; unique |
| `p256dh` | Browser public encryption key |
| `auth` | Browser auth secret |
| `user_agent` | Optional client user agent snapshot |
| `device_label` | Optional label shown in settings/debug UI |
| `enabled` | Soft-disable without deleting history |
| `last_success_at` | Last successful send |
| `last_failure_at` | Last failed send |
| `failure_count` | Consecutive failures |
| `created_at` | Created timestamp |
| `updated_at` | Updated timestamp |

Why SQLite instead of V2 config:

- Subscriptions are runtime/device state, not user-authored configuration.
- A user may have multiple installed PWAs/devices.
- Endpoints rotate or expire and need operational metadata.
- The existing durable state model already owns messages, sessions, media, show
  pages, and background runs.

Migration work:

- Add `storage.models.web_push_subscriptions`.
- Add an Alembic migration after the current head.
- Extend `storage.migrations` drift repair and `HEAD_TABLES`/required columns if
  needed.
- Add a small `storage/web_push_service.py` with CRUD and failure handling.

## 6. VAPID Keys and Configuration

Use standard Web Push with VAPID.

Implementation choice:

- Add Python dependency `pywebpush` unless a smaller maintained library is
  preferred during implementation review.
- Generate a local VAPID keypair automatically on first use and store it under
  the Vibe Remote state directory, not in source.
- Expose the public VAPID key through an authenticated API endpoint.

Suggested storage:

- Private key: `~/.vibe_remote/state/web_push_vapid.json`
- Public key: returned by `GET /api/web-push/vapid-public-key`

Rationale:

- Self-hosted installs should work without external setup.
- VAPID private key is secret runtime state and must not live in repo config.
- Keeping a stable key preserves existing subscriptions across restarts.

## 7. Backend API

Add authenticated UI APIs:

### `GET /api/web-push/status`

Returns feature availability and current browser-independent server status:

- `configured`
- `public_key`
- `subscription_count`

### `GET /api/web-push/vapid-public-key`

Returns the base64url public VAPID key for `PushManager.subscribe()`.

### `POST /api/web-push/subscriptions`

Accepts the browser `PushSubscription` JSON and upserts by endpoint.

Input:

```json
{
  "endpoint": "https://...",
  "keys": {
    "p256dh": "...",
    "auth": "..."
  },
  "user_agent": "..."
}
```

Validation:

- `endpoint` must be a non-empty HTTPS URL.
- `keys.p256dh` and `keys.auth` must be non-empty strings.
- Reject malformed payloads with 400.

### `DELETE /api/web-push/subscriptions`

Accepts an endpoint and disables/removes that subscription.

### `POST /api/web-push/test`

Sends a test notification to the current subscription/device. This is necessary
because iOS support must be validated on a real device after Home Screen install
and permission grant.

## 8. Frontend and Service Worker

### Service worker

Add `ui/public/push-sw.js` or equivalent static asset.

Responsibilities:

- Listen for `push`.
- Parse payload defensively.
- Call `self.registration.showNotification(title, options)`.
- Update badge when supported (`navigator.setAppBadge` is page-side; service
  worker support differs, so keep badge best-effort).
- Show every notification with `navigate` set to its absolute target URL.
- Listen for `notificationclick` for engines that ignore `navigate`.
- Focus an existing client if available; otherwise open the target URL.

Payload shape:

```json
{
  "title": "Agent replied",
  "body": "Short preview text",
  "url": "/chat/<session_id>?msg=<message_id>&from=notification",
  "tag": "session:<session_id>",
  "message_id": "<message_id>",
  "session_id": "<session_id>",
  "badge_count": 3
}
```

A notice without a session opens `/harness`; anything that is not a
same-origin path opens `/inbox`.

### Notification taps

A tap opens the exact chat, scrolled to the notified message (`msg`, the same
deep-link anchor Search uses). When that chat is gone or not readable for the
reader (bootstrap 404/403), `from=notification` makes ChatPage replace the route
with the Inbox instead of showing an error.

iOS Home Screen apps cannot route a tap through `notificationclick`
reliably: iOS launches the app at the manifest start URL, a suspended client
can reject `focus()` or miss `postMessage`, the worker that handles the click
may not reach a client the user launched, and `openWindow()` can resolve before
its client is live (WebKit 252544, 259212, 268797, 279456). The page used to
read the Cache Storage handoff once per launch or resume, so a handoff written
after that read was missed and the app stayed on its last page.

Safari 18.4 and later, including iOS Home Screen apps, open a notification's
`navigate` URL themselves on a tap, cold or suspended, and do not fire
`notificationclick`. The page then loads at the target, so nothing else is
shown first. Chromium ignores `navigate`; its taps still focus a window and
post `vibe.notification-click`, or open the target, with the Cache Storage
handoff as before. iOS before 18.4 keeps that best-effort path.

The worker calls `skipWaiting()` on install. It has no fetch handler, and a
suspended Home Screen app would otherwise keep an older worker until killed.

The UI server logs `web push: notification opened <path> msg=<id>` when a
document loads with `from=notification`, which is the device-side evidence that
the platform opened the tapped URL.

### App registration

Add a small frontend module, for example `ui/src/lib/webPush.ts`.

Responsibilities:

- Detect support:
  - `serviceWorker` in `navigator`
  - `PushManager` in `window`
  - `Notification` in `window`
- Detect standalone display where possible:
  - `matchMedia('(display-mode: standalone)')`
  - iOS `navigator.standalone`
- Register service worker only when the user opts in.
- Request permission only inside the click handler.
- Subscribe with `userVisibleOnly: true` and the server public VAPID key.
- Upsert the subscription to the backend.

UX placement:

- Put the first implementation in an existing Workbench/settings surface, not as
  an aggressive global prompt.
- Show the enable button only when the browser claims support.
- On iOS Safari that is not standalone, show a concise blocked state: add to
  Home Screen first, then open from the Home Screen.

All user-visible copy must go through `ui/src/i18n/en.json` and
`ui/src/i18n/zh.json`.

## 9. Notification Trigger

Trigger Web Push after durable inbox-relevant messages are persisted.

Initial trigger rule:

- Platform: `avibe`
- Session-backed message
- Message author: `agent`
- Message type: `result` or terminal `notify`
- Message is unread after persistence

Preferred implementation boundary:

- Add `core/web_push_notifications.py` as the notification dispatcher.
- Call it from the same durable write paths that already compute/publish
  `inbox.session.updated`.
- Keep it non-blocking for agent turns: failures should be logged and recorded
  on subscriptions but must not break message persistence or SSE realtime.

The first implementation can be synchronous in a threadpool or fire-and-forget
task, but it should have one clear service boundary so later batching/rate-limit
logic can land without changing message persistence again.

Deduping:

- Use the persisted `message.id` as the logical notification id.
- If needed, add a small `web_push_deliveries` table later. Do not add it in the
  first pass unless duplicate sends appear in tests or runtime.

## 10. In-App Realtime Check

The current SSE path is good enough to remain the active UI realtime foundation.

Recommended small hardening:

- On `EventSource` `connected`, refresh the inbox snapshot. This reconciles any
  events missed during reconnect.
- On `visibilitychange` from hidden to visible, refresh the inbox snapshot.
- Keep realtime events as fast incremental updates, but treat REST snapshots as
  authoritative.

This hardening belongs in `WorkbenchInboxContext`; it does not require a new
transport.

## 11. Test Plan

Backend unit tests:

- Subscription upsert validates required fields and endpoint uniqueness.
- Disabled/expired subscription is skipped.
- 410/404 Web Push response disables a subscription.
- Inbox notification trigger sends only for avibe unread agent `result`/terminal
  `notify` messages.
- Message persistence still succeeds when Web Push send fails.

Frontend tests/build:

- `npm run build`.
- Type-check service worker registration helper.
- Verify i18n keys exist in English and Chinese.

Manual device validation:

1. Serve the branch through the regression or an isolated dev environment over
   HTTPS.
2. Open on iOS 16.4+ Safari.
3. Add Vibe Remote to Home Screen.
4. Open the Home Screen app.
5. Tap "Enable notifications".
6. Confirm the iOS permission prompt.
7. Send `/api/web-push/test`.
8. Lock the phone or leave the app.
9. Confirm the notification appears in the iOS notification shade.
10. Tap the notification and confirm it opens the intended Workbench session.

Non-goals for the first validation:

- Email fallback.
- IM fallback.
- Cross-device notification preference UI.
- Full delivery analytics.

## 12. Implementation Phases

### Phase 1: Plan and skeleton

- Land this plan.
- Add storage service and migration for subscriptions.
- Add VAPID key generation/loading.

### Phase 2: API and service worker

- Add authenticated Web Push APIs.
- Add `push-sw.js`.
- Add frontend helper and opt-in UI.
- Add test notification endpoint.

### Phase 3: Inbox trigger

- Hook durable agent result/notify persistence into Web Push dispatcher.
- Keep failures isolated from message persistence.
- Add focused tests.

### Phase 4: Realtime hardening

- Refresh inbox on SSE connected/reconnected.
- Refresh inbox on page visibility restore.

### Phase 5: Device validation

- Build UI.
- Run focused Python tests.
- Validate on a real iOS 16.4+ Home Screen PWA.

## 13. Current Implementation Status

Implemented in this branch:

- SQLite `web_push_subscriptions` model, Alembic migration, and storage helpers.
- Local VAPID key generation under the Vibe Remote state directory.
- Authenticated Web Push status, subscribe, unsubscribe, public-key, and test-send APIs.
- Static push service worker with `push` and `notificationclick` handling.
- Inbox toolbar opt-in control with iOS standalone/PWA support detection.
- Durable Workbench inbox trigger from agent `result` / `notify` / `error` messages.
- Owner scoping: subscriptions are bound to the current remote user when known,
  with local-install fallback for non-remote local UI.
- Targeted backend tests, migration coverage, and frontend production build.

Still pending before release:

- Real iOS Home Screen PWA validation on an actual device.
- Product decision on notification noise suppression when the same session is
  actively open.
- Optional settings surface for listing/removing multiple registered devices.

## 14. Open Questions

1. Should the first opt-in UI live in Settings or directly in the Inbox header?
   Settings is less intrusive; Inbox is more discoverable.
2. What should `user_key` be when remote access is disabled? The first pass can
   use `"local"` and evolve once multi-user local Web UI is real.
3. Should terminal `notify` messages push in v1? They are inbox-visible and can
   represent failures that need attention, but regular progress notifications
   should not wake the user.
