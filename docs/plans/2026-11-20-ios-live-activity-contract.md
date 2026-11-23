# iOS Live Activity for one Turn (I5) — three-lane contract

Status: **FROZEN** for implementation on 2026-11-20. Owner decisions (Max, 2026-11-20 10:16 UTC+08:00): track the Turn that is running in a session; the Lock Screen shows the Agent name **and the session title**; "needs you" lights the screen; switching instance ends the activity; the relay and Apple may see the phase and times in the clear; an ended activity stays for one hour. Field names, routes, status codes, and constants below are exact. A lane that needs a change asks the orchestrator for an amendment first; a lane never edits another lane's side of this file.

**Amendment 1 (2026-11-22): automatic start behind a device setting.** Max withdrew the in-conversation entry on 2026-11-21 (#4125) after it stacked copies of itself in the conversation, and decided on 2026-11-22 01:39 UTC+08:00:
- the conversation page shows no Lock Screen entry or hint;
- a device-only setting in Settings › Notifications turns the feature on, off by default;
- with it on, leaving the App while the open conversation's Turn runs shows that Turn;
- a Turn dismissed from the Lock Screen is not shown again.

This amendment replaces §1 steps 1–2, §2.2, the start rules of §2.3, and §4.3's Web-entry bullet. The Runtime is unchanged: §4.4 already keeps one `tracking` row per device (`push_registration_id`), and two devices tracking the same Turn each keep their own row. The relay (§3), the vectors, and every other rule are unchanged.

Vectors shared byte for byte by every lane: `tests/fixtures/live_activity/contract_vectors.json`. Reference rules and the vector checks: `tests/test_live_activity_contract.py`. Design frames: `ui/design: ios/live-activity/*`.

## 1. The flow, once

This walkthrough only shows the happy path. Sections 2–5 are normative; where this walkthrough says less than they do, they decide.

1. Max has turned on Settings › Notifications › Show progress on the Lock Screen on his iPhone. He is reading the 首席安全官 session while its Turn runs, so the Web UI has told the App `liveActivity.observe({sessionId, agentName, sessionTitle, running: true})`, and the App has already read that Turn (`GET /api/web-push/live-activities/turn`).
2. He locks the phone. As the App leaves the foreground, it starts an `Activity<TurnActivityAttributes>` with `pushType: .token`, waits for its first push token, and registers it (`PUT /api/web-push/live-activities/<activity_id>`) together with this device's relay registration id.
3. The instance's controller watches that Turn. On each state change it POSTs a relay request to `https://avibe.bot/api/push/apns/<registration_id>/live-activity`. The relay turns that request into one APNs `liveactivity` push to the activity token. While the Turn keeps running, the controller also sends a low-priority heartbeat every 15 minutes.
4. The Turn ends. The controller sends `event: end` with a dismissal date one hour later. Tracking stops once the relay accepts that end, or when a §4.3 stop rule applies; until then the end stays pending and is resent.

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

### 2.2 Bridge (Web ↔ App), capability `lockScreenProgress: 1`

The `liveActivities` capability and its methods (`status`, `start`, `stop`, and the in-conversation entry) are retired. **The App never announces `liveActivities` again, at any version.** An older instance's Web bundle still renders its entry whenever that capability is present at version 1 or higher, so announcing it would bring the entry back. The new behavior uses a capability of its own, `lockScreenProgress: 1`: the Web uses the methods below only when the shell announces it, and old bundles do not know it. The App answers `liveActivity.status`, `liveActivity.start`, and `liveActivity.stop` with `unsupported`, for any page that still calls them.

| Method | Params | Result |
| --- | --- | --- |
| `liveActivity.setting` | `{}` | `{enabled, systemEnabled}`. `enabled` is this device's setting, `false` until the person turns it on, stored in the App only (never sent to an instance). `systemEnabled` is `ActivityAuthorizationInfo().areActivitiesEnabled`. |
| `liveActivity.setSetting` | `{enabled}` (a boolean) | the same shape after applying it. Turning it off ends every `TurnActivityAttributes` activity (§2.3 row "the device setting is turned off"). |
| `liveActivity.observe` | `{sessionId, agentName, sessionTitle, running}`, or `{sessionId: null}` | `{ok: true}`. Reports the conversation that is on screen: `sessionTitle` may be `null` (a Bot's session), `running` is the page's `runtimeState.foreground === 'running'`. The App keeps only the latest observation. |
| `liveActivity.openSettings` | `{}` | `{ok: true}`. Opens this App's page in the system Settings. |

Examples are the `bridge_v2` vectors. The `bridge` vectors are version 1 and are kept only as a record.

**Web rules:**
- Settings › Notifications shows one row when the shell announces `lockScreenProgress`: a switch bound to `setting` and `setSetting`. When `systemEnabled` is false, the switch is disabled and the row shows the existing system-off line with the existing Settings button (`openSettings`).
- The conversation page renders no Lock Screen element of any kind. A hook with no DOM sends `observe` whenever `sessionId`, `agentName`, `sessionTitle`, or `running` changes, and `{sessionId: null}` when the conversation page goes away. It skips a call identical to the last one and does not depend on the setting.
- The page passes the title it shows in the conversation header (`null` for a Bot's session).
- ChatPage tests fail on any React duplicate-key warning: the stacked copies came from two siblings sharing `key={sessionId}`.

**Words** (zh / en; reviewed 2026-11-22). The row's two strings are new; the other two are reused:
- Title: 「在锁屏上显示进度」 / "Show progress on the Lock Screen"
- Description: 「离开 Avibe 时，如果正在看的会话还在处理，就在锁屏和灵动岛上显示进度。」 / "When you leave Avibe while the conversation you are viewing is still working, its progress appears on the Lock Screen and in the Dynamic Island."
- System off (reused): 「实时活动在系统设置里关着」 / "Live Activities are off in Settings", and 「去设置」 / "Settings"

The version-1 toasts and the in-conversation frames (`app-offer`, `app-tracking`, `app-system-off`, `app-chips`, `app-start-refused`) are removed in this amendment. The setting row is drawn as `proposed` frames `ui/design: settings/live-activity/*` (off by default, on, and system off).

### 2.3 Lifecycle owned by the App

- **Observation and prefetch:** while an observation reports `running: true`, the App reads that Turn with `GET …/turn` and keeps the answer: `turn_id`, `phase`, `started_at`, `confirmed_at`. It reads again when the observation changes and when the App comes to the foreground. A `409`, `404`, `503`, or no answer leaves no prefetched Turn. `running: false` or `{sessionId: null}` drops it.
- **Ledger:** the App records, persisted on the device, every `turn_id` that has had an activity. **A Turn gets at most one activity, ever:** once recorded, no later start creates another for it, whatever ended the first one (dismissed, ended, replaced, or failed after `Activity.request` succeeded). An entry is kept until the App knows that Turn is over: a read for its session returns a terminal phase or a different `turn_id`, or its activity received a terminal state. It is not pruned on time alone, because a Turn can run past the 8-hour activity limit. An entry 30 days old may go regardless. Starts are serialized: at most one is in flight.
- **start** happens only on `UIScene.willDeactivateNotification`, while the App is still active (ActivityKit refuses `Activity.request` from the background). That notification also fires when the person only pulls down Notification Center or Control Center; a start then is accepted (owner, 2026-11-22), and the ledger keeps it to one per Turn. Every condition below must hold, checked in order; if one fails, nothing happens, with no UI and no toast:
  1. the device setting is on, and `areActivitiesEnabled` is true;
  2. the latest observation has a `sessionId` and `running: true`, and its session belongs to the selected account and instance;
  3. a prefetched Turn for that session exists, and its `turn_id` is not in the ledger;
  4. `NativePushCoordinator` has an active relay registration for this instance.
- Then, synchronously inside the notification callback, while the App is still active (nothing here awaits):
  - call `Activity.request(attributes, content: (state from the prefetched Turn, staleDate: confirmedAt + 2400), pushType: .token)`. If it throws, stop here: nothing is recorded, any other activity is left exactly as it was, and the next departure may try again;
  - on success, mark every other `TurnActivityAttributes` activity as displaced (its token and state observers stop at once, and it is never registered or updated again), record the new activity's `turn_id` in the ledger, and begin a background task.
- Inside that background task, after the callback has returned:
  - end each displaced activity and DELETE it with `replaced` (row "a new start displaces it"). The old card may stay visible next to the new one for a moment, but it is never restored;
  - wait for the first value of `pushTokenUpdates` (none within 10 s: end `.immediate`);
  - PUT, and handle the answer by R(PUT) below.

After `start`, each local activity is in exactly one state, persisted on the device across launches together with its `requested_at` (the Unix second of its `Activity.request`, sent on every PUT):
- **registered:** the instance holds its current token, because the last PUT was answered `200`;
- **unregistered:** it has no PUT answered `200` since its last token.

This table is the whole lifecycle. Every event has one row; "end" removes the activity from the device. Nothing re-creates an ended activity: the ledger allows one activity per Turn.

| Event | Unregistered | Registered |
| --- | --- | --- |
| a new token from `pushTokenUpdates` | PUT → R(PUT) | becomes unregistered, then PUT → R(PUT) |
| the App comes to the foreground | PUT → R(PUT) | GET → R(GET) |
| unregistered for 28800 s since `Activity.request` | end `.immediate`, then DELETE `system_ended` | — |
| a new start displaces it | end `.immediate`, then DELETE `replaced` | same |
| the person dismisses it, or the system ends it, with no App action behind it (`activityStateUpdates`) | DELETE `dismissed` or `system_ended` | same |
| Live Activities are turned off (`activityEnablementUpdates` is false) | end `.immediate`, then DELETE `disabled` | same |
| the device setting is turned off (`setSetting {enabled: false}`) | end `.immediate`, then DELETE `disabled` | same |
| the App launches with the device setting off | end `.immediate` with no request (an earlier build's leftovers), and forget their ledger rows' tokens | same |
| logout, account switch, instance switch, or instance removed | end `.immediate`, then DELETE `logout`, `account_switch`, or `instance_switch` | same |

- **R(PUT):**
  - `200`: registered; apply the returned state.
  - `409 turn_not_active`: end with the returned terminal state.
  - `403`, `404`, `400`, `409 activity_conflict`, `409 activity_expired`, or `200` with `status: stopped`: end `.immediate`.
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
- body, compact JSON in this key order: `{"aps":{"timestamp","event","content-state":{"phase","startedAt"[,"endedAt"],"confirmedAt"}, "stale-date" | "dismissal-date"[, "alert"]}}`. `alert` is exactly `{"title":{"loc-key":"AVIBE_LIVE_ACTIVITY_NEEDS_YOU_TITLE"},"body":{"loc-key":"AVIBE_LIVE_ACTIVITY_NEEDS_YOU_BODY"},"sound":"default"}`. This is the ActivityKit alert shape, not the legacy flat `title-loc-key` form: `title` and `body` are objects with `loc-key`, and `sound` sits inside `alert`. That is how Apple's ActivityKit push article shows it (["Starting and updating Live Activities with ActivityKit push notifications"](https://developer.apple.com/documentation/activitykit/starting-and-updating-live-activities-with-activitykit-push-notifications), update and start payload examples).

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
| `PUT /api/web-push/live-activities/<activity_id>` with `{session_id, turn_id, push_registration_id, activity_token, requested_at}` | `200 {ok, activity}`, `409 turn_not_active` with the ended `activity`, `409 activity_expired`, `409 activity_conflict` (same id, different session/turn/registration/`requested_at`), `403 registration_not_owned` (the registration is not a native relay subscription of this caller), `400 invalid_request`, or `404 session_not_found` |
| `GET /api/web-push/live-activities/<activity_id>` | `200 {ok, activity}` or `404 not_found` |
| `DELETE /api/web-push/live-activities/<activity_id>` with `{reason}` | `200 {ok, stopped}`. Idempotent. `reason ∈ user_stopped, dismissed, system_ended, replaced, logout, account_switch, instance_switch, disabled` |

- `activity` is `{activity_id, session_id, turn_id, phase, started_at, ended_at, confirmed_at, status}`, with `status ∈ tracking, ended, stopped`.
- Every route above can also answer `503 service_unavailable` when the controller is unreachable, as other controller-backed routes already do. What the App does with it depends on the call, and §2.3 defines that per call: `start` fails on a `503` from `GET …/turn`; a `503` on PUT keeps the activity unregistered and retries it; a registered activity's GET changes nothing; a DELETE is not retried.
- **Serialization:** the controller process is the single owner of tracking rows and sends. The four routes reach it over the existing internal IPC; ui_server does not write the table itself. Registration, token replacement, DELETE, and every send for one `activity_id` are serialized there, so no send uses a token or state that a later PUT or DELETE has already replaced.
- `activity_id` is ActivityKit's `Activity.id`: 1–64 characters of `[A-Za-z0-9-]`.
- `requested_at` is the Unix second at which the App called `Activity.request`. It is a JSON integer no more than 300 s in the future by the instance clock, and it is fixed for the activity: every PUT for one `activity_id` carries the same value. It anchors the eight-hour limit: `expires_at = requested_at + 28800`, and a PUT at or after that answers `409 activity_expired`.
- What a PUT does to an existing row is defined in §4.4.

### 4.2 Tracking row (SQLite, new table `live_activity_trackings`)

Columns: `activity_id` (primary key), `owner_user_key`, `session_id`, `turn_id`, `push_registration_id`, `activity_token`, `status`, `stop_reason`, `last_phase`, `last_timestamp`, `confirmed_at`, `pending_state` (the undelivered state, or null), `requested_at`, `created_at`, `updated_at`, `expires_at` (`requested_at + 28800`). The lane may add internal columns. The migration revision number is allocated by the Runtime lane at rebase time (pr-delivery-loop §0). No title or Agent name is stored.

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
  - rejected: `410 activity_token_gone` stops with `stop_reason: token_gone`. Any other `410` stops with `registration_gone`. Any other `4xx`, or a `502`, stops with `relay_rejected` and is logged. In every case the App's foreground GET then ends the card locally, except as §4.4 allows a new token to revive a `token_gone` row.
- **Stop** (status `ended` or `stopped`; never sends again):
  - after the end push is accepted;
  - on any rejection as defined above;
  - on DELETE;
  - at `expires_at = requested_at + 28800`, the system's eight-hour limit, without a push.
- **Restart:** tracking resumes from the table. A pending state is sent, and otherwise a send happens only if the phase differs from `last_phase`; the pending state is part of the row, so it survives a restart.
- Sending reuses the VAPID signing of `send_badge_update` and posts the body of §3.2 exactly.
- The Web side of this lane is the setting row and the observation hook of §2.2, with their zh/en copy in `ui/src/i18n`. The in-conversation entry is retired (Amendment 1).

### 4.4 Tracking row lifecycle

A row is `tracking`, `ended`, or `stopped`. This table is the whole row lifecycle; every event has one row, and "turn live" means the row's Turn has not reached `terminal`.

| Event | no row | `tracking` | `ended` | `stopped` |
| --- | --- | --- | --- | --- |
| PUT whose `session_id`, `turn_id`, `push_registration_id`, or `requested_at` differs from the row's | — | `409 activity_conflict` | same | same |
| PUT at or after `requested_at + 28800` | `409 activity_expired` | becomes `stopped` (`expired`); `409 activity_expired` | `409 activity_expired` | same |
| PUT, turn live | create `tracking`; `200` | store the token, keep `pending_state`; `200` | — | `stop_reason: token_gone` and a token different from the row's: back to `tracking` with the new token, keeping `pending_state`; `200`. Any other: `200` with `status: stopped`, unchanged |
| PUT, turn terminal | create `ended`; `409 turn_not_active` | becomes `ended` (no end push is owed: the App ends the card from this answer); `409 turn_not_active` | `409 turn_not_active` | `200` with `status: stopped`, unchanged |
| a PUT that creates or revives a `tracking` row for another `activity_id` on the same `push_registration_id` | — | becomes `stopped` (`replaced`) | — | — |
| the publisher's send | — | §4.3 | — | — |
| `now ≥ expires_at` | — | becomes `stopped` (`expired`), no push | — | — |
| the row's source goes away: its push registration is no longer an enabled subscription of the owner, the owner can no longer read the session, or the Turn row is gone | — | becomes `stopped` (`registration_unavailable`, `session_unavailable`, or `turn_unavailable`), no push | — | — |
| DELETE | `200 {stopped: false}` | becomes `stopped` (the DELETE reason); `200 {stopped: true}` | `200 {stopped: false}` | same |

`stop_reason` is one of the DELETE reasons, `replaced`, `expired`, `token_gone`, `registration_gone`, `relay_rejected`, `registration_unavailable`, `session_unavailable`, or `turn_unavailable`. Only `token_gone` can be revived, and only by a new token: an ActivityKit token rotation can race a send to the old token, and the App's PUT of the new token must not lose a live activity. The App sends that PUT on every rotation (§2.3).

## 5. Invariants every lane tests

- Through the relay and APNs, only the phase enum and Unix seconds travel in the clear; names and titles stay on the phone.
- An activity token's death never affects the device's ordinary notifications.
- A tracked Turn has exactly one terminal state. Once its end has been attempted, the publisher sends nothing but that same end again; on the wire it can arrive more than once, because a send whose answer is lost is resent (§4.3) and the relay keeps no idempotency state. Every copy carries the same `phase`, `started_at`, and `ended_at`. ActivityKit ignores a push to an activity that has already ended, so a duplicate changes nothing on the phone.
- A Turn gets at most one activity on a device (the App ledger). A device has at most one `tracking` row (§4.4); two devices of one owner tracking the same Turn keep a row each.
- The conversation page renders no Lock Screen element, and with the device setting off the App starts nothing.
- Logout, account switch, and instance switch leave no `TurnActivityAttributes` activity on the phone.
- Required end-to-end check: a real instance sends through the deployed relay to a TestFlight build with non-ASCII names (the vectors use `首席安全官` and `依赖审计 · avault 🔒`).

## 6. Out of scope

Amendment 1 adds: any in-conversation entry, hint, or toast; starting a Turn the person is not viewing when they leave; following a Turn into another conversation.

push-to-start, frequent updates (`NSSupportsLiveActivitiesFrequentUpdates`), several Turns at once, approving on the Lock Screen, step counts, percentages or ETAs, following a session rename, and tracking Tasks, Watches, or PRs.
