# iOS Live Activity for one Turn (I5) — three-lane contract

Status: **FROZEN** for implementation on 2026-11-20. Owner decisions (Max, 2026-11-20 10:16 UTC+08:00): track the Turn that is running in a session; the Lock Screen shows the Agent name **and the session title**; "needs you" lights the screen; switching instance ends the activity; the relay and Apple may see the phase and times in the clear; an ended activity stays for one hour. Field names, routes, status codes, and constants below are exact. A lane that needs a change asks the orchestrator for an amendment first; a lane never edits another lane's side of this file.

Vectors shared byte for byte by every lane: `tests/fixtures/live_activity/contract_vectors.json`. Reference rules and the vector checks: `tests/test_live_activity_contract.py`. Design frames: `ui/design: ios/live-activity/*`.

## 1. The flow, once

1. Max opens the 首席安全官 session in the iOS App while a Turn runs and taps **Show on Lock Screen**. The Web UI calls `liveActivity.start({sessionId, agentName, sessionTitle})`.
2. The App reads the running Turn (`GET /api/web-push/live-activities/turn`), starts an `Activity<TurnActivityAttributes>` with `pushType: .token`, waits for its first push token, and registers it (`PUT /api/web-push/live-activities/<activity_id>`) together with this device's relay registration id.
3. The instance's controller watches that Turn. On each state change it POSTs a relay request to `https://avibe.bot/api/push/apns/<registration_id>/live-activity`. The relay turns that request into one APNs `liveactivity` push to the activity token. While the Turn keeps running, the controller also sends a low-priority heartbeat every 15 minutes.
4. The Turn ends. The controller sends `event: end` with a dismissal date one hour later and stops tracking.

The Agent name and session title stay on the phone (ActivityAttributes); they never reach the instance's tracking row, the relay, or Apple.

## 2. iOS (avibe-app `ios/`)

### 2.1 Types (compiled into the App and the `AvibeLiveActivity` extension)

```swift
struct TurnActivityAttributes: ActivityAttributes {
    enum Phase: String, Codable, Hashable { case working, needsYou, done, failed, stopped }
    struct ContentState: Codable, Hashable {
        var phase: Phase
        var startedAt: Int      // Unix seconds the Turn started
        var endedAt: Int?       // Unix seconds the Turn ended; terminal phases only
        var confirmedAt: Int    // Unix seconds the instance last confirmed this state
    }
    var agentName: String       // ≤ 40 grapheme clusters, ellipsized by the App
    var sessionTitle: String?   // ≤ 60 grapheme clusters; nil when empty or equal to agentName
    var accountID: String
    var instanceOrigin: String
    var sessionID: String
    var turnID: String
}
```

- `ContentState` must stay `Int` seconds. ActivityKit decodes push `content-state` with its default strategies, so a `Date` would read as seconds since 2001.
- `ContentState` decoding must accept every `content-state` in `relay_cases` (an iOS test decodes each `apns.body_utf8`). Adding a phase is a contract amendment, because an older App cannot decode an unknown case.
- The session title is captured when tracking starts and is **not** updated afterwards; attributes are immutable. Following a rename would require the title to travel in `content-state` through the relay and APNs in the clear. That is out of scope.

### 2.2 Bridge (Web ↔ App), capability `liveActivities: 1`

| Method | Params | Result |
| --- | --- | --- |
| `liveActivity.status` | `{sessionId}` | `{enabled, tracking}`. `enabled` is `ActivityAuthorizationInfo().areActivitiesEnabled`; `tracking` is true when a local activity exists for this session. |
| `liveActivity.start` | `{sessionId, agentName, sessionTitle}` (`sessionTitle` may be `null`) | `{ok: true}` or `{ok: false, code}`, where `code ∈ disabled \| no_active_turn \| push_unavailable \| failed` |
| `liveActivity.stop` | `{sessionId}` | `{ok: true}`. Ends the activity `.immediate` and DELETEs with `user_stopped`. |
| `liveActivity.openSettings` | `{}` | `{ok: true}`. Opens this App's page in Settings. |

When `start` is refused, the Web shows the shipped toast:
- `no_active_turn` (warning): 「这一轮已结束。」 / "This turn has ended."
- `push_unavailable` (warning): 「开启通知后才能在锁屏上显示。」 / "Turn on notifications to show this on the Lock Screen."
- `failed` (error): 「未能更新锁屏显示，请重试。」 / "Couldn't update the Lock Screen. Try again." (ASCII apostrophe)
- `disabled` never shows a toast; the entry switches to its Settings state instead.

These strings are drawn in `ui/design: ios/live-activity/app-start-refused`.

The Web passes the title it already shows in the conversation header; for a Bot's session (no title) it passes `null`. The Web shows the entry only when the capability is present and the session's Turn is in flight. When `enabled` is false it shows the Settings entry and never calls `start`.

### 2.3 Lifecycle owned by the App

- **start:** one activity at a time. Once the first three steps below have passed, and just before `Activity.request`, the App ends any other `TurnActivityAttributes` activity (the "displaces" row below). A start refused by `disabled`, `push_unavailable`, or `GET …/turn` leaves the current activity alone; a later failure leaves no activity. Every step has a defined outcome, and `start` answers with the first refusal it meets:

| Step | Outcome | `start` answers |
| --- | --- | --- |
| Live Activities are off | stop | `disabled` |
| no active relay registration for this instance in `NativePushCoordinator` | stop | `push_unavailable` |
| `GET …/turn` | `200`: continue. `409 no_active_turn` or `404`: stop. `503` or no answer: stop | `no_active_turn`, `no_active_turn`, `failed` |
| `Activity.request(attributes, content: (state from the turn, staleDate: confirmedAt + 2400), pushType: .token)` | throws: stop | `failed` |
| first value of `pushTokenUpdates` | none within 10 s: end `.immediate` | `failed` |
| `PUT` | `200`: registered; apply the state | `{ok: true}` |
| | `409 turn_not_active`: end with the returned terminal state | `no_active_turn` |
| | `403 registration_not_owned`: end `.immediate` | `push_unavailable` |
| | `404`, `400`, `409 activity_conflict`: end `.immediate` | `failed` |
| | `503` or no answer: keep it, unregistered | `{ok: true}` |

After `start`, each local activity is in exactly one state, persisted on the device across launches:
- **registered:** the instance holds its current token, because the last PUT was answered `200`;
- **unregistered:** it has no PUT answered `200` since its last token.

This table is the whole lifecycle. Every event has one row; "end" removes the activity from the device. The App never re-creates an ended activity on its own; only the person's explicit `start` makes a new one, for the same Turn as well.

| Event | Unregistered | Registered |
| --- | --- | --- |
| a new token from `pushTokenUpdates` | PUT → R(PUT) | becomes unregistered, then PUT → R(PUT) |
| the App comes to the foreground | PUT → R(PUT) | GET → R(GET) |
| unregistered for 28800 s since `Activity.request` | end `.immediate`, then DELETE `system_ended` | — |
| `liveActivity.stop` | end `.immediate`, then DELETE `user_stopped` | same |
| a new `start` displaces it | end `.immediate`, then DELETE `replaced` | same |
| the person dismisses it, or the system ends it, with no App action behind it (`activityStateUpdates`) | DELETE `dismissed` or `system_ended` | same |
| Live Activities are turned off (`activityEnablementUpdates` is false) | end `.immediate`, then DELETE `disabled` | same |
| logout, account switch, instance switch, or instance removed | end `.immediate`, then DELETE `logout`, `account_switch`, or `instance_switch` | same |

- **R(PUT):**
  - `200`: registered; apply the returned state.
  - `409 turn_not_active`: end with the returned terminal state.
  - `403`, `404`, `400`, `409 activity_conflict`: end `.immediate`.
  - `503` or no answer: stay unregistered. The local `staleDate` marks the card out of date until a later row registers it.
- **R(GET):**
  - `status: tracking` with a live phase: update.
  - A terminal phase: end with that state.
  - `status: stopped` or `404`: end `.immediate`.
  - `503` or no answer: change nothing.
- **DELETE** is best-effort and never retried; its answer changes nothing on the device. A row whose DELETE was lost stops by itself, either on the relay's `410` once the token is dead or at `expires_at`. Pushes to an activity that is gone are ignored by the system.
- Logout also revokes the relay registration, so the instance stops on the relay's `410` even when the DELETE is lost.
- **tap:** `widgetURL(avibe-app://live-activity/<activityID>)`. The App registers the `avibe-app` scheme in `CFBundleURLTypes` (no entitlement). It resolves the id only against its own `Activity<TurnActivityAttributes>.activities` and opens that account, instance, and `/chat/<sessionID>` through the existing instance-path route. An unknown id opens the App without navigating.
- The voice capture cleanup stays scoped to `Activity<VoiceCaptureAttributes>`.

### 2.4 What a card shows

- Shown: the Avibe mark, `agentName`, `sessionTitle` (one line, tail-truncated), the phase, and time. A live card shows the elapsed time (`Text(timerInterval:)` from `startedAt`, counted on the phone); every ended card (done, failed, stopped) shows the total time, `endedAt - startedAt`.
- Never shown: message text, tools, files, or the instance name.
- Stale (`context.isStale`) on a working or needsYou card: the timer stops, and the card reads "May be out of date. Last confirmed working at {confirmedAt}". No timer keeps running for a state nobody confirmed.
- The words are in the design frames. Lock Screen and island strings live in the extension's xcstrings (zh, en). The alert keys `AVIBE_LIVE_ACTIVITY_NEEDS_YOU_TITLE` and `AVIBE_LIVE_ACTIVITY_NEEDS_YOU_BODY` live in the App's `Localizable.xcstrings`.

## 3. Relay (avibe-backend)

### 3.1 Route and authorization

`POST /api/push/apns/<registration_id>/live-activity` with `Authorization: vapid t=<ES256 compact JWT>, k=<base64url P-256 key>` and `Content-Type: application/json`. This is the same authorization the relay's existing badge operation uses (`updateBadge` in avibe-backend `lib/push/relay.ts` at `6200b745`; its frozen contract is avibe-backend `docs/plans/badge-only-apns-contract.md` §2). The rules are restated here so this file is complete. Checks run in this order, and no APNs call is made on any failure:

1. No relay configuration: `503 relay_unavailable`.
2. The registration id is not 43 base64url characters decoding to 32 bytes, or no such registration exists: `410 registration_not_found`. A revoked registration: `410 registration_revoked`.
3. VAPID, verified against the registration's stored `vapid_key_sha256`:
   - a malformed header or a bad signature: `400 invalid_vapid`;
   - a different key: `410 vapid_key_mismatch`;
   - `aud` is not the exact configured origin: `400 invalid_audience`;
   - `exp` has passed: `400 vapid_expired`;
   - `exp` is more than 24 hours ahead: `400 vapid_exp_too_far`.
4. The body (§3.2), validated only after authentication: `400 invalid_live_activity`.
5. The installation snapshot:
   - the device token was marked invalid: `410 apns_provider_invalid`;
   - no environment or topic: `503 apns_token_unavailable` with `Retry-After: 60`;
   - an environment or topic outside the configured sets: `502 apns_bad_gateway`.
6. Rate limit: `429 relay_rate_limited` with `Retry-After`.

No installation management credential is required.

### 3.2 Body (≤ 1024 UTF-8 bytes, strict)

**The raw bytes are judged before the object** (reference: `validate_relay_raw`):
- The body is at most 1024 bytes as received. Check this before parsing, so whitespace padding or `\uXXXX` escapes cannot slip past it.
- Decode it as strict UTF-8 without stripping a BOM. In TypeScript use `new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })`.
- Parse it strictly, keeping integer and non-integer number tokens distinct: `1795000900.0` and `1.7950009e9` are not integers and fail. Duplicate keys are rejected. Plain `JSON.parse` meets neither rule; avibe-backend's existing `parsePushJson`, followed by field type checks, meets both.
- The top level must be a JSON object, with no trailing data and no `NaN` or `Infinity`.
- Any failure here is `400 invalid_live_activity`.

The vectors `relay_raw_accepts` and `relay_raw_rejections` give exact byte bodies (base64), including a body of exactly 1024 bytes and one of 1025.


| Field | Required | Rule |
| --- | --- | --- |
| `activity_token` | yes | lowercase hex, even length, 16–512 characters |
| `event` | yes | `update` or `end` |
| `timestamp` | yes | Unix seconds |
| `priority` | yes | integer `5` or `10` |
| `phase` | yes | `update`: `working` or `needsYou`; `end`: `done`, `failed`, or `stopped` |
| `started_at`, `confirmed_at` | yes | Unix seconds, `started_at ≤ confirmed_at` |
| `stale_at` | `update` only | `> confirmed_at` |
| `ended_at`, `dismiss_at` | `end` only | `started_at ≤ ended_at ≤ dismiss_at` |
| `alert` | optional | only `"needsYou"`, only with `phase: needsYou` and `priority: 10` |

Unix seconds are JSON integers in [1600000000, 4102444800]. The token must match `(?:[0-9a-f]{2}){8,256}` over the whole string, so a trailing newline fails. In TypeScript use `/^(?:[0-9a-f]{2}){8,256}$/` without the `m` flag; in Python use `fullmatch`. The validator type-checks every field before it uses it; a wrong JSON type in any field, including an object or array for `event`, `phase`, or `alert`, is a validation failure and never a server error. Any other key, a `null` in place of an absent field, a float, a boolean, or a string number fails with `400 invalid_live_activity`. Nothing in the body is free text, so the route cannot carry names or titles.

### 3.3 APNs request (exact; see `relay_cases[].apns`)

- path `/3/device/<activity_token>`, sent to the fixed APNs host of the installation's environment
- `apns-push-type: liveactivity`
- `apns-topic: <installation.topic>.push-type.liveactivity`, derived from the installation's already-allowed topic (no new configuration)
- `apns-priority`: the body's priority
- `apns-expiration`: `timestamp + 3600` for an update, `timestamp + 14400` for an end
- no `apns-collapse-id`
- body, compact JSON in this key order: `{"aps":{"timestamp","event","content-state":{"phase","startedAt"[,"endedAt"],"confirmedAt"}, "stale-date" | "dismissal-date"[, "alert"]}}`. `alert` is exactly `{"title":{"loc-key":"AVIBE_LIVE_ACTIVITY_NEEDS_YOU_TITLE"},"body":{"loc-key":"AVIBE_LIVE_ACTIVITY_NEEDS_YOU_BODY"},"sound":"default"}`.

### 3.4 Responses

- Success: `201 {"ok":true,"registration_id":"<id>","apns_status":200}`. Before the APNs call, failures are those of §3.1.
- APNs outcome mapping (`classifyApnsResponse`):
  - `device_gone` (410, BadDeviceToken, DeviceTokenNotForTopic, Unregistered): **`410 activity_token_gone`**;
  - `payload_too_large`: `413 apns_payload_too_large`;
  - `upstream_rate_limited`: `503 apns_upstream_rate_limited` with `Retry-After: 60`;
  - `timeout`: `503 apns_timeout` with `Retry-After: 30`;
  - `unavailable`: `503 apns_unavailable` with `Retry-After: 30`;
  - `bad_gateway` or `provider_auth`: `502 apns_bad_gateway`.
- **`activity_token_gone` never marks the installation's device token invalid** (no `markPushProviderInvalid`). One dead activity must not cost the phone its notifications.
- Rate bucket `push:liveActivityRegistration:<sha256(registration_id)>`, 60 per minute, burst 10.
- No relay state is added: no table and no migration. The token is never logged; at most a redacted hash is.

## 4. Runtime (avibe-app instance)

### 4.1 API (ui_server; same authorization class as `/api/web-push/subscriptions`, rows scoped to `_web_push_user_key()`)

| Request | Result |
| --- | --- |
| `GET /api/web-push/live-activities/turn?session_id=` | `200 {ok, session_id, turn: {turn_id, phase, started_at, confirmed_at}}`, `409 no_active_turn`, or `404 session_not_found` (also when the caller cannot read the session) |
| `PUT /api/web-push/live-activities/<activity_id>` with `{session_id, turn_id, push_registration_id, activity_token}` | `200 {ok, activity}`, `409 turn_not_active` with the ended `activity`, `409 activity_conflict` (same id, different session/turn/registration), `403 registration_not_owned` (the registration is not a native relay subscription of this caller), `400 invalid_request`, or `404 session_not_found` |
| `GET /api/web-push/live-activities/<activity_id>` | `200 {ok, activity}` or `404 not_found` |
| `DELETE /api/web-push/live-activities/<activity_id>` with `{reason}` | `200 {ok, stopped}`. Idempotent. `reason ∈ user_stopped, dismissed, system_ended, replaced, logout, account_switch, instance_switch, disabled` |

- `activity` is `{activity_id, session_id, turn_id, phase, started_at, ended_at, confirmed_at, status}`, with `status ∈ tracking, ended, stopped`.
- Every route above can also answer `503 service_unavailable` when the controller is unreachable, as other controller-backed routes already do. What the App does with it depends on the call, and §2.3 defines that per call: `start` fails on a `503` from `GET …/turn`; a `503` on PUT keeps the activity unregistered and retries it; a registered activity's GET changes nothing; a DELETE is not retried.
- **Serialization:** the controller process is the single owner of tracking rows and sends. The four routes reach it over the existing internal IPC; ui_server does not write the table itself. Registration, token replacement, DELETE, and every send for one `activity_id` are serialized there, so no send uses a token or state that a later PUT or DELETE has already replaced.
- `activity_id` is ActivityKit's `Activity.id`: 1–64 characters of `[A-Za-z0-9-]`.
- The PUT is an upsert. The same activity with a new token replaces the token. A new activity on the same `push_registration_id` stops the previous tracking with reason `replaced`.

### 4.2 Tracking row (SQLite, new table `live_activity_trackings`)

Columns: `activity_id` (primary key), `owner_user_key`, `session_id`, `turn_id`, `push_registration_id`, `activity_token`, `status`, `stop_reason`, `last_phase`, `last_timestamp`, `confirmed_at`, `pending_state` (the undelivered state, or null), `created_at`, `updated_at`, `expires_at`. The lane may add internal columns. The migration revision number is allocated by the Runtime lane at rebase time (pr-delivery-loop §0). No title or Agent name is stored.

### 4.3 Publisher (controller process)

- **Phase:**
  - A Turn in `session_turns` that has reached `terminal` maps through `phase_map`.
  - A Turn still running is `needsYou` while that session's Turn is blocked on a pending owner decision that the person answers in the App. Otherwise it is `working`.
  - The definition is this property, not a list of sources. Today the sources are: a pending `vault_requests` row whose requester or delivery `session_id` is the session; a pending agent question held by any registered agent question handler (`PendingQuestion`, keyed by its gate's `base_session_id`); and a pending Computer Use confirmation (`ComputerScriptInteractionManager`).
  - The lane reads all of these through one chokepoint, so a source added later is covered by registering it there. A test seeds one pending request of every kind that exists and asserts `needsYou` for each.
- **Sends:**
  - A phase change sends priority 10. Only entering `needsYou` adds `alert: "needsYou"`.
  - While the Turn runs, a heartbeat is sent every 900 s at priority 5 with no alert.
  - The PUT response applies the first state locally, so no push is sent at start.
- **Times:** `stale_at = confirmed_at + 2400`. At the end, `dismiss_at = ended_at + 3600`. `timestamp = max(now, last_timestamp + 1)`, so the order is strict per activity.
- **Delivery:** the publisher keeps one pending state per activity: the latest state it has not yet delivered. A later change replaces it, and an end is never replaced. A send settles in one of three ways:
  - accepted (`201`): the pending state is cleared;
  - retryable (`429`, `503`, a timeout, or no answer): retried at most 3 times right away, honoring `Retry-After`. If it is still not accepted, it stays pending and is sent again on every following heartbeat tick (900 s), with a fresh `timestamp` and `confirmed_at`, until it is accepted or a stop rule applies. This holds for an end too; until the end is accepted, the row keeps `status: tracking` with its terminal phase, and GET returns that terminal state, so the App ends the activity locally on its next foreground. Meanwhile the card goes stale at its `stale_at`, so it never shows a confirmed running state for long;
  - rejected (`410` any code): stop. Any other `4xx` or a `502`: also stop, with `stop_reason: relay_rejected`, and log it; the App's foreground GET then ends the card locally.
- **Stop** (status `ended` or `stopped`; never sends again):
  - after the end push is accepted;
  - on any rejection as defined above;
  - on DELETE;
  - at `expires_at = created_at + 28800`, the system's eight-hour limit, without a push.
- **Restart:** tracking resumes from the table. A pending state is sent, and otherwise a send happens only if the phase differs from `last_phase`; the pending state is part of the row, so it survives a restart.
- Sending reuses the VAPID signing of `send_badge_update` and posts the body of §3.2 exactly.
- The Web entry and its zh/en copy are part of this lane (`ui/src/i18n`).

## 5. Invariants every lane tests

- Through the relay and APNs, only the phase enum and Unix seconds travel in the clear; names and titles stay on the phone.
- An activity token's death never affects the device's ordinary notifications.
- A tracked Turn produces at most one `end`, and nothing after it. An activity the person dismissed is never re-created.
- Logout, account switch, and instance switch leave no `TurnActivityAttributes` activity on the phone.
- Required end-to-end check: a real instance sends through the deployed relay to a TestFlight build with non-ASCII names (the vectors use `首席安全官` and `依赖审计 · avault 🔒`).

## 6. Out of scope

push-to-start, frequent updates (`NSSupportsLiveActivitiesFrequentUpdates`), several Turns at once, approving on the Lock Screen, step counts, percentages or ETAs, following a session rename, and tracking Tasks, Watches, or PRs.
