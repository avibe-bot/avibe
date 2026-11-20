# HealthKit v2: One-way health data reading in conversations

Date: 2026-11-18 (Asia/Shanghai)
Status: **Plan confirmed; ready to begin.** At 08:47 on 2026-11-18, Max said:
“All agents in the instance can read it, not just Vibey. Follow the other
recommendations, update the document and Show Page, and prepare to start.”
Section 7 records the confirmed decisions; section 8 describes the implementation
plan; the appendices define the implementation baseline interfaces. Each of the
following still requires Max's separate approval: merging, Apple App ID / profile
changes, internal TestFlight, and instance releases.

## 0. Updated requirement

Max's original request at 08:39 on 2026-11-18:

> My requirement is simply that Avibe can read iOS health data in one direction.
> Avibe does not need a separate entry point; the entry point can be a call
> within a conversation. The main work is permissions and interface integration.

This document replaces conflicting product behavior in `healthkit-h0-contract.md`
(see section 6). The H0 boundaries that remain valid are:

- Read only.
- Snapshot / summary format and freshness fields.
- Empty results do not mean zero or “denied.”
- Audit records contain no values.
- No `mobile.invoke`.
- Web pages cannot obtain health data.

## 1. Design in one sentence

After you authorize the phone once, it automatically syncs health data in one
direction to your own instance. An agent in a conversation reads the instance's
data through `vibe mobile health` and can see when the data was collected.

```text
iPhone                                            Your instance (Mac)
HealthKit --1--> Avibe native sync --3 device API--> Health storage <--5 vibe mobile health-- Agent
                        ^                               |
                        +----4 notification-------------+
Instance web page (conversation card) --2 native bridge health.connect--> App
                                      (authorization only; returns no data)
```

## 2. In-conversation workflow

### First connection: the entry point is the conversation

1. In a verified owner Mac, Web, or phone conversation, or an IM direct
   conversation verifiably belonging to the owner, ask any agent to read health
   data. Group conversations, other people's conversations, and unverifiable
   sources must not read.
2. Vibey runs `vibe mobile health read`, receives `not_connected`, and then runs
   `vibe mobile health connect` to create a connection request.
3. The request appears in two places; either can handle it:
   - **iPhone notification:** “Vibey requests access to your health data.”
     Open Avibe to handle it.
   - **Conversation card:** when viewing this conversation on the phone, the
     reply has an “Authorize on iPhone” button.
4. The phone presents two screens in order:
   - **Avibe native confirmation:** identifies the instance, data groups, the
     fact that data syncs to the instance, and how to stop.
   - **iOS system health authorization:** individual data types can be toggled.
5. The app registers the device and backfills the last 30 days. Vibey receives
   the receipt and continues answering.

**Initial authorization must happen on the iPhone.** Only the app in the
foreground can present system health authorization; the Mac cannot do it.

### Subsequent reads

- The agent reads data already on the instance; the phone need not be online.
- Each data type includes three times: latest sample time, phone query time,
  and instance receipt time.
- If data is old, `read --refresh` sends a refresh request:
  - Phone in foreground: completes within a few seconds.
  - Otherwise: sends a notification and waits for you to open it; the agent
    answers using existing data and states its age.

### Stopping

- Ask Vibey to run `vibe mobile health revoke`: stops sync and deletes health
  data on the instance.
- Disable access in iOS Settings › Health › Data Access › Avibe: stops new reads;
  existing instance data expires according to retention.
- Sign out of the app: clears local sync state and device credentials.

## 3. Permissions (four layers)

| Layer | Granted by | Scope | Revocation |
| --- | --- | --- | --- |
| iOS health authorization | You, in the system UI | Data types the app can read; no writes | iOS Settings. The app cannot distinguish denial from no data |
| Avibe device authorization | You, once in Avibe native confirmation | This iPhone → this instance; selected data groups | `vibe mobile health revoke`, sign-out, or automatic expiry after 60 days without credential use |
| Reading within the instance | Max (November 18, 08:47; option C at 10:44) | Any agent may serve the owner, not just Vibey; each read requires a verified owner private source; group, other-person, unknown and mixed sources are denied | Record allowed and denied access metadata, without values. Use revoke to stop sync and delete instance data |
| AI service | Max (confirmed November 18) | The model currently used by the instance's agent may read; values enter that agent's model context and conversation record | Content already sent to a model provider cannot be recalled. Use internal TestFlight first; resolve review issues before external release |

Additional rules:

- Any instance kind (personal or organization) and any sharing state may
  connect. The instance owner pairs their own iPhone. Anyone who can reach the
  instance, and therefore its Agents, can read synced data; the owner controls
  that through the instance's access settings (Max, November 20, 09:01).
- An iPhone syncs to only one instance at a time.
- Option C constrains the request source, not the agent roster. Delegation,
  bots, turns with multiple initiators, and Task/Watch execution must preserve
  verifiable original provenance. Forwarding a group request into a local
  session does not make it an owner request. Missing or mixed provenance is
  denied; joining a group does not by itself disable all synchronization.
- **Implementation gap:** current IM bindings have no Cloud-owner mapping.
  Admin status, binding, local CLI execution, environment variables, or the
  target session's creator do not authenticate the initiator. Trusted mapping
  and provenance propagation require a separate scoped decision. Same-OS
  arbitrary shell execution can bypass a CLI guard; it is not a sandbox.

## 4. Interface integration

| # | Boundary | Parties | Interface | Authentication and limits |
| --- | --- | --- | --- | --- |
| 1 | HealthKit | App ↔ iOS | Read-only `HKHealthStore` authorization; daily statistics (`HKStatisticsCollectionQuery`); sleep and workout samples; `HKObserverQuery` + `enableBackgroundDelivery` | System authorization; app main target only |
| 2 | Native bridge | Instance web page → app | Capability `health: 1`; `health.connect({requestId, pairingCode})` → `{state}` | Instance main frame only. Triggers native confirmation and system authorization; **returns no health data** |
| 3 | Device API | App → instance | `POST /api/mobile/device/v1/pair`; `PUT …/health/days` (whole-day replacement); `POST …/requests/claim`; `POST …/requests/{id}/result`; `DELETE …/health` | Revocable device credential in device-only Keychain, valid only for these routes. The remote access gateway needs this authentication category and a separate security review |
| 4 | Notifications | Instance → phone | Add a health-request kind to the existing encrypted push relay; opening it handles connection or refresh | Existing RFC 8291 encryption; relay cannot see content. No silent push in the initial release |
| 5 | Agent command | Agent → instance | `vibe mobile health status / read / connect / revoke` | Local CLI reads local storage directly. **No health-reading API callable by any web page, including the workbench** |

Pairing-code handoff:

1. The authenticated instance page containing the card or notification requests
   a single-use pairing code valid for five minutes.
2. The page passes it to native code through interface 2.
3. Native code exchanges it for a device credential through interface 3.

The credential never passes through the web page.

Draft commands:

```bash
vibe mobile health status
vibe mobile health read --metric sleep,steps --period last7d [--max-age 6h] [--refresh --wait 20]
vibe mobile health connect
vibe mobile health revoke
```

`connect` sends a connection request to the phone. `revoke` stops sync and deletes
instance health data. `read` returns:

- Daily data per metric, with `availability` (`ok` / `empty` / `not_synced`)
  and all three timestamps.
- `device_wait_timeout` with existing data if refresh has not completed; it
  never reports “refreshed” in that case.

## 5. When data updates

| Trigger | Behavior |
| --- | --- |
| First connection | Backfill the last 30 days |
| App in foreground | Immediately sync the last eight days (whole-day replacement automatically corrects deletions and additions) |
| Background | HealthKit wakes the app when new data arrives; steps at most hourly; data unavailable while locked is caught up after unlock |
| Agent refresh request | Send notification; completes within seconds if app is foreground, otherwise waits for opening |
| Instance retention | Rolling 30 days; revoke deletes immediately, with complete cleanup within 24 hours |

**Not guaranteed:**

- Updates at an exact time.
- Data while locked.
- Apple Watch data not yet synced to the phone.

## 6. Differences from accepted H0 behavior

| H0 (November 16 / November 18) | v2 |
| --- | --- |
| In-app health summary page; phone-only by default | No separate entry point; use a conversation notification / card |
| Confirm every share, each covering 1 / 7 / 30 days | Authorize once (system + Avibe native confirmation), then automatic one-way sync; rolling 30-day instance retention |
| No background sync in the initial release | Background sync included from the start |
| Agent reads only manually shared snapshots | Agent reads automatically synced data and can request best-effort refresh |
| Health-purpose AI services only | Allow the model currently used by the instance agent; resolve review issues before external release |
| (Undecided in H0) Reading scope | All agents may serve the verified owner; option C restricts the initiating source |
| Native bridge `app.showHealth()` | Native bridge `health.connect()`, authorization only |

Unchanged:

- Read only.
- Web pages cannot obtain health data.
- The instance owner's own iPhone, on any instance kind or sharing state.
- No `mobile.invoke` or raw second-level samples.
- Empty does not mean zero.
- Access records contain no values.

## 7. Confirmed decisions (Max, 2026-11-18 08:47)

1. **Data scope:** initially sync only steps, activity, workouts, and sleep.
   Heart rate, resting heart rate, and HRV are deferred.
2. **Background sync from the start:** requires
   `com.apple.developer.healthkit.background-delivery`.
3. **Allow the current model used by the instance agent to read:** internal
   TestFlight first. Before external TestFlight or App Store distribution,
   resolve Apple's two rules:
   - Third parties receiving health data must provide health / fitness services.
   - App Review 5.1.2(i) requires disclosure of third-party AI and permission.
4. **Every agent in the instance may read for the owner**, not just Vibey.
   No agent allowlist. Record who, when, metrics and dates, without values.
5. **November 18, 10:44:06: option C.** Allow owner App/Web conversations and
   verifiably owner-bound IM direct conversations. Deny groups (including an
   owner asking in a group), other people's direct conversations, and unknown
   or mixed sources. Delegation, background work, and session reuse cannot
   convert the original source into owner authority. Unverified environment
   variables may aid diagnostics, never authentication.
6. **November 18, 13:10:41: use the existing instance.** This supersedes the
   earlier separate-instance route. Do not create an instance, OS user,
   sandbox, VM, host, VPN, phone-only entrance, or data migration.

The supported read-only configuration check at 13:13 found no enabled external
IM platforms, no bindings on all five platforms, no extra live-allowlist grants,
and a UI listener on 127.0.0.1. This owner-only configuration is the acceptance
target. These facts do not establish whole-machine or browser isolation, accept
all residual risks, or remove option C. Future sharing or IM changes require
another assessment.

**Current implementation status:** CLI caller/session environment values are
audit labels. The CLI checks the live instance owner (any kind or sharing state);
it does not yet enforce trusted original-owner provenance across delegation.
That gap remains explicit. The bounded file-service repair does not require a
cross-repository filesystem rewrite or whole-platform isolation project.
Merge, installation, Apple profiles, TestFlight, and real-data acceptance remain
subject to their separate authorizations.

## 8. Implementation plan

### Slices

| Slice | Scope | Repository / suggested lane | Dependencies | Gates |
| --- | --- | --- | --- | --- |
| **S1 Instance** | Checklist below | avibe-app Python; new `codex` session | None | One PR; pr-delivery-loop; separate device-credential security review |
| **S2 Conversation card** | Checklist below | avibe-app `ui/`; `frontend` | Appendix A interface shapes (parallel with S1) | PR; `npm run build`; design frames follow `ui/design/AGENTS.md` |
| **S3 iOS native** | Checklist below | avibe-app `ios/`; iOS lane (currently occupied by I3; main arranges another session or queues it) | Appendix A; Apple changes before archive | PR; foreground simulator verification; **Apple App ID / profile changes need Max's approval** |
| **S4 End-to-end acceptance** | Internal TestFlight on a real device + install instance package containing S1; run appendix E scenarios | — | S1–S3 merged | **Merging, instance release / installation, and TestFlight each require Max's authorization** |

**S1 Instance:**

- First translate this document into English at `docs/plans/health-data.md`
  (the filename reserved in `ios-app.md` section 10).
- Separate mobile storage (appendix C).
- Device API and remote gateway device-credential authentication (appendix A1).
- Pairing-code API.
- Request queue, waiting / callbacks (appendix B).
- Health-request push kind (appendix A3).
- `vibe mobile health` CLI (appendix A4), Skill instructions, and parser contract tests.
- Access records and retention cleanup.
- Fully isolated tests with a fake device.

**S2 Conversation card:**

- Render health requests as conversation cards:
  - On the phone, “Authorize on iPhone” calls the native bridge.
  - Elsewhere, show “Sent to iPhone; handle it on your phone.”
- Add notification landing route `/mobile/health/:requestId`.
- Add `health` capability to `ui/src/lib/native.ts`.
- English and Chinese translations.

**S3 iOS native:**

- Permissions and configuration:
  - HealthKit and background-delivery entitlements.
  - English and Chinese `NSHealthShareUsageDescription`.
  - Update `testflight.py` archive checks.
- Native UI and bridge:
  - Avibe native confirmation (SwiftUI).
  - Native bridge `health.connect`.
- HealthKit reads:
  - Query according to appendix D.
  - Register observer and background delivery at startup.
- Device client:
  - Pair; store credential in device-only Keychain.
  - Outbox and background `URLSession` uploads.
  - Claim requests and return results.
- Completion:
  - Handle taps on health-request notifications.
  - Clean up on sign-out.

### Order

1. S1, S2, and S3 proceed in parallel against appendix A interface shapes.
2. Merge S1 first (Max's authorization required) and include it in a local
   acceptance package.
3. Enable HealthKit in Apple's console and re-sign the profile (Max's approval
   required); S3 can then be archived.
4. Internal TestFlight (Max's authorization required).
5. S4 real-device end-to-end acceptance.

### Approval points during implementation

- Merge each PR.
- Install the instance acceptance package containing S1.
- Apple App ID / profile changes.
- Upload internal TestFlight.

## Appendix: Implementation baseline interfaces

S1–S3 share the following interface shapes. Fields may be added during
implementation, but semantics must remain unchanged; update this document before
changing semantics.

### A1. Device API (app → instance)

Prefix: `/api/mobile/device/v1`. JSON only; maximum request size 256 KiB.

Authentication:

- `Authorization: Bearer <device credential>`, valid only for these routes.
  Cookies are not accepted, so CSRF is unnecessary.
- The instance stores the credential's SHA-256.
- Every request checks:
  - Device has not been revoked.
  - Pairing user is still the instance owner.

| Method and path | Request → response | Notes |
| --- | --- | --- |
| `POST /pair` | `{pairing_code, device_name, platform, app_version, metrics}` → `{device_id, credential, instance:{id,name}}` | Single-use pairing code; five-minute lifetime; 128 bits. Pairing the same phone again replaces its old device |
| `PUT /manifest` | `{contract:1, app_version, os_version, timezone, metrics, background_delivery, push_wake}` → `{accepted_at}` | Send at every launch and when changed |
| `PUT /health/days` | See A2 → `{accepted_seq, received_at}` | Replace whole days |
| `POST /requests/claim` | `{wait_seconds:0..25, max:1..10}` → `{requests:[…]}` | Foreground long polling; use 0 on background wake |
| `POST /requests/{id}/result` | `{claim_token, outcome:"succeeded"\|"failed", result?, error?}` → `{state}` | Idempotent for the same `(id, claim_token)` |
| `DELETE /health` | → `{purged:true}` | Called when stopping from the phone |
| `DELETE /` | → `{}` | Unregister on sign-out |

**Error codes:**

| HTTP | code | Phone behavior |
| --- | --- | --- |
| 401 | `device_unauthorized` | Discard credential; await next connection |
| 403 | `device_revoked` | Stop sync |
| 403 | `instance_not_eligible` | Instance shared or no longer owned; stop sync |
| 409 | `stale_seq` | Discard this upload |
| 409 | `claim_mismatch` | Discard result |
| 410 | `request_terminal` | Discard result |
| 422 | `invalid_payload` / `incomplete_window` | Implementation defect; do not retry in a loop |
| 426 | `contract_unsupported` | Ask user to update app |
| 429 / 503 | — | Retry according to `Retry-After` |

**Pairing-code API:**

- `POST /api/mobile/pairings`: cookie + CSRF; owner only.
- Returns `{pairing_code, expires_at}`.
- The page passes the code to the app through the native bridge.

### A2. Upload format: `PUT /health/days`

```json
{
  "device_seq": 1042,
  "computed_at": "2026-11-18T07:40:12+08:00",
  "window": {"metrics": ["steps","activity","workouts","sleep"], "from": "2026-11-11", "to": "2026-11-18"},
  "items": [
    {"metric": "steps", "day": "2026-11-18", "timezone": "Asia/Shanghai",
     "availability": "ok", "value": {"count": 1234},
     "latest_sample_end_at": "2026-11-18T07:31:00+08:00",
     "queried_at": "2026-11-18T07:40:11+08:00", "closed": false}
  ],
  "failures": [{"metric": "workouts", "at": "…", "code": "protected_data_unavailable"}]
}
```

- `items` contains exactly one item for every metric × day in the window.
  Days with no data use `availability:"empty"` and `value:null`; missing items
  return `incomplete_window`.
- Only if `device_seq` exceeds the stored value does the instance replace
  device × metric × day records. The phone persists a monotonically increasing
  `device_seq`.
- Metrics in `failures` have no items in this upload; the instance preserves
  existing data and records `last_failure`.
- Window rules:
  - Default: today plus the previous seven days.
  - Initial connection backfills 30 days; at most 31 days per upload.
  - Historical corrections outside the window are not reflected (known limit).

### A3. Requests and push

**Request operations:**

| Operation | Initiator | Phone behavior | `requires_user` | Expiry |
| --- | --- | --- | --- | --- |
| `health.connect` | Agent | Native confirmation, system authorization, pairing, backfill | Yes | Default 24 hours |
| `health.refresh` | Agent | Query and upload, then return result | No | Default 15 minutes |

**Results:**

- Only per-metric status (`ok` / `empty` / error code); **no values**.
  Values exist only in storage.
- Unfinished `health.refresh` requests for the same device are coalesced.

**States:**

- `queued` → `claimed` → `succeeded` / `failed`, plus `expired` / `canceled`.
- Claim lease: 60 seconds, at most three attempts.
- Results arriving after cancellation are recorded as `late_result` without
  changing state.
- The phone executes at least once, keeping a local ledger so it does not
  execute the same request again.

**Push:**

- Reuse existing instance push: encrypted Web Push → relay → APNs alert,
  with `kind:"health.request"`.
- Tap destination: `/mobile/health/<requestId>`.
- Copy uses backend i18n, e.g. “Vibey requests access to your health data” /
  “Vibey requests an update to your health data.”
- At most three notifications per device per hour.
- Devices without notifications rely on the conversation card.

### A4. Agent command: `vibe mobile health`

Every command returns the standard CLI envelope (`schema_version:1`);
errors exit with status 1.

```bash
vibe mobile health status
vibe mobile health read [--metric steps,activity,workouts,sleep]
    [--period today|yesterday|last7d|last30d | --from YYYY-MM-DD --to YYYY-MM-DD]
    [--max-age 6h] [--refresh [--wait 20]] [--callback-session-id ID | --no-callback]
vibe mobile health connect [--wait 0] [--callback-session-id ID | --no-callback]
vibe mobile health revoke
vibe mobile requests show <request-id>
vibe mobile requests await <request-id> [--timeout S]
```

**`status`:**

- Connection state, device name, app version, last contact.
- Latest successful sync and `last_failure` per metric.
- `background_delivery` state.
- `live`: whether the phone is currently foreground long polling.

**`read`:**

- Returns `metrics.<name>.items[]`, one per day:
  - `day`, `timezone`, `availability` (`ok` / `empty` / `not_synced`),
    `closed`, `value`.
  - Three times: `latest_sample_end_at`, `queried_at`, `received_at`.
- Includes `metrics.<name>.freshness`.
- When `--max-age` is given, computes `stale`; old data does not fail the read.

**`--refresh`:**

- Creates or coalesces a `health.refresh` request and waits at most `--wait` seconds.
- Timeout returns `ok:false, code:"device_wait_timeout"` with existing data and
  request ID in `details`.
- Never reports “refreshed” on timeout.

**`connect`:**

- Creates `health.connect`, sends push, and leaves a card in the conversation.
- When already connected, returns current status directly.

**`revoke`:**

- Revokes the device and deletes instance health data.
- The phone receives `device_revoked` on its next contact.

**Waiting and callbacks:**

- Same continuation rule as Vault approvals: only one continuation owner.
- After timeout or process exit, hand over to the callback session, defaulting
  to `AVIBE_SESSION_ID`.
- A later synchronous waiter can reclaim an abandoned reservation only after
  its deadline expires and its OS lock is verified free in the same write
  transaction. Replacing the waiter preserves the request/result and callback
  destination; the old waiter cannot consume the replacement's continuation.
- Callback messages contain no values.

**Error codes:**

| Category | code |
| --- | --- |
| Connection | `not_connected`, `device_revoked`, `instance_not_eligible` |
| Arguments | `invalid_period`, `unsupported_metric` |
| Request | `device_wait_timeout`, `request_not_found`, `request_expired` |
| Phone-reported | `protected_data_unavailable`, `capability_unavailable`, `user_declined`, `internal` |

### B. Native bridge (instance web page → app)

| Item | Description |
| --- | --- |
| `capabilities.health = 1` | Capability declaration |
| `health.connect({requestId, pairingCode})` | Returns `{state: "connected" \| "declined" \| "failed"}`. `connected` means confirmation, pairing, manifest, exact request claim validation and durable local connection commit have succeeded; initial synchronization continues through the persisted reservation/outbox |

- Instance main frame only.
- `connected` confirms the connection, not completed health synchronization.
  The request becomes `succeeded` only after an actual upload acknowledgement
  and the server accepts the result receipt. Ordinary network delay after the
  local commit does not revoke the connection or imply successful sync. The
  existing three-claim lease contract and bounded network attempts stay intact.
- `last_contact_at` is historical contact, not real-time device availability.
  App sign-out revokes local access immediately and attempts remote deletion;
  it does not promise that offline remote history has already been deleted.
- **Returns no health data.**
- All other `health.*` operations return `unsupported`.

### C. Storage and logs

- **Location:** `~/.avibe/state/mobile/mobile.sqlite3`, mode 0600, with its own
  schema version and migrations.
- **Not exposed:** no registration in `vibe data query`, Show, memory, or exports.
- **Retention:**
  - Health data: rolling 30 days.
  - Completed requests: seven days.
  - Access records: 90 days.
- **Cleanup triggers:**
  - `revoke`.
  - Phone `DELETE /health` or `DELETE /`.
  - Instance becomes shared.
  - Credential unused for 60 days.
- **Logs:** IDs, operation, state, error codes only; no values.
- **Known exposure:** after an agent reads, values enter its conversation
  record and model context.
- Retention and revoke cleanup apply to active mobile storage, not all copies
  in transcripts, model contexts/caches, historical backups, or model-provider
  systems. This change does not configure system backup exclusions.
- File-service protection uses one operation's private inode snapshot.
  Descriptor namespaces are denied without relying on case-sensitive spelling,
  before path resolution; file downloads, search/replace text readers and copy
  input are checked with `fstat` and read through that same descriptor.
  Recursive measurement and copying check descendants; cross-filesystem move
  uses the same checked staging and does not publish a rejected tree or delete
  its source. A failed stage cleanup records its path. Ordinary delete and
  same-filesystem move retain their existing semantics; there is no
  cross-process atomic tree transaction. Cross-filesystem move retains the
  previous `copystat` metadata behavior before publishing and removing its
  source; ordinary copy retains its existing behavior and size budget.
- This finite boundary does not isolate same-OS arbitrary code, terminals,
  author-controlled Show code, existing copies, or new private inodes linked
  outside the tree after its snapshot. Media/Show path guards are not a general
  replacement for opened-object authorization throughout the platform.

### D. Metric definitions (read only)

| Metric | Source and algorithm | Daily `value` |
| --- | --- | --- |
| `steps` | `stepCount`; `HKStatisticsCollectionQuery` cumulativeSum per local calendar day (HealthKit merges sources) | `{count}` |
| `activity` | `appleExerciseTime`, `activeEnergyBurned`, cumulativeSum for each | `{exercise_minutes, active_energy_kcal}` |
| `workouts` | `HKWorkout` starting that day | `{workouts:[{activity, start_at, end_at, duration_minutes, active_energy_kcal?, distance_m?}]}` |
| `sleep` | `sleepAnalysis`; night D = local [D−1 18:00, D 18:00) | `{asleep_minutes, in_bed_minutes, start_at, end_at, stages}` |

Sleep calculation:

- Union intervals from multiple sources for each stage; do not sum them.
- Include `stages` only when exactly one source provides stage data; otherwise
  use `null`.

### E. Acceptance criteria

- **Fully isolated (S1, fake device):**
  - Pairing and credentials.
  - Whole-day replacement and `device_seq`.
  - Empty data does not mean zero.
  - Refresh timeout never reports success.
  - Expired leases can be reclaimed without duplicate execution.
  - Late results after cancellation.
  - Revoke and cleanup.
  - Sharing does not pause sync; a live owner change revokes and purges.
  - No values in logs or `vibe data query`.
  - Only one continuation owner across waiting and callbacks.
- **Negative tests:** instance pages (including Show) receive `unsupported`
  for every `health.*` call except `health.connect`; no cookie-callable health
  reading API exists.
- **Real device (S4):**
  - Both notification and conversation-card entry points.
  - Denial and partial authorization.
  - Lock screen.
  - Recovery after going offline.
  - Measured background wake latency distribution.
  - Reopen after force quit.
  - Deletion and backfill corrections.
  - Overnight sleep and time zones.
  - Revoke and sign-out.

## Amendment 1 (confirmed by main, 2026-11-18 09:02)

This fills the handoff before a phone has a device credential. The read-only,
route-scoped credential and value-free result boundaries remain unchanged.

1. **Pairing codes bind to a connect request.** The owner cookie + CSRF endpoint
   `POST /api/mobile/pairings` takes `{"request_id":"mreq_..."}` and issues a
   five-minute single-use code only for a nonterminal `health.connect` on this
   instance. Only its hash is stored. Pairing atomically validates and consumes
   that code, creates / replaces the device credential, binds the connect
   request to that device, and invalidates its other unused codes. A competing
   or replayed pairing cannot replace the binding. Terminal, canceled or expired
   requests cannot be paired. Pairing itself does not complete the request.
2. **Claim by request ID.** `POST /api/mobile/device/v1/requests/claim` adds optional
   `request_id`. A filtered claim returns only that request bound to the current
   device. Unbound connect requests never enter normal polling; unrelated devices
   receive an empty list. Each claimed object contains `id`, `operation`
   (`health.connect` / `health.refresh`), `claim_token`, `created_at`, `expires_at`,
   `requires_user`, and `metrics`, without pairing code, credential or values.
   The lease remains 60 seconds with three attempts. Reclaim changes the claim
   token; the phone's execution ledger prevents repeated execution.
3. **Status-only results.** `result.metrics` maps only the four metric keys to
   `ok`, `empty`, or `protected_data_unavailable`, `capability_unavailable`,
   `user_declined`, `internal`. `error` contains only `code`; unknown fields are
   rejected and freeform strings are not stored. `user_declined` means only
   “Not now” in Avibe's native confirmation, never an inference from an empty
   HealthKit result. Values upload through `PUT /health/days` before success.

   ```json
   {"claim_token":"...","outcome":"succeeded","result":{"metrics":{"steps":"ok","sleep":"empty"}}}
   ```

   ```json
   {"claim_token":"...","outcome":"failed","error":{"code":"protected_data_unavailable"},"result":{"metrics":{"steps":"protected_data_unavailable"}}}
   ```

4. **Refresh polls directly.** A paired foreground phone long-polls claim for up
   to 25 seconds. Otherwise the encrypted alert opens
   `/mobile/health/<requestId>` and the app resumes authenticated polling. Only
   the originally bound device can claim refresh.
5. **Repeat pairing identity.** Pair adds optional `installation_id`, a random
   app-install identifier stored in ThisDeviceOnly Keychain. It locates the old
   credential to revoke; it is never authentication. Without it, device names
   are not used as identifiers. S3 supplies this field.

## Amendment 2 (confirmed by main, 2026-11-18 09:14)

1. **Conversation card metadata.** Harness conversation message metadata adds
   `mobile_request: {id, operation, state, created_at, expires_at, requires_user,
   metrics}`. It contains no claim token, pairing code or health values.
   `GET /api/mobile/requests/{id}` requires owner cookie authentication and returns
   only the same metadata; it is not a health-reading API. `state` is exactly
   `queued | claimed | succeeded | failed | expired | canceled`.
2. **Sleep stages.** `sleep.stages` maps `awake`, `core`, `deep`, `rem`,
   `unspecified` to minutes using the interval union per stage. Omit absent
   stages; do not include inBed (use `in_bed_minutes`). Conflicting multiple
   sources use `null` according to appendix D. If either inBed or asleep data is
   absent, its component is `null`, never zero. The sleep day closes at 18:00.
3. **Workout types.** `workout.activity` is the Apple enumeration name in lower
   snake case, such as `running`, `cycling`, `walking`. Unknown values use
   `other_<rawValue>`.
4. **Instance validation.** Accept these nullable components and represent them
   as missing data; never substitute zero.

Amendment 2 addendum: declined / update-required presentation derives from
`failed` plus `error.code` (`user_declined` / `capability_unavailable`).
Disconnected is the device state after revocation, not a request state.

Amendment 2 further addendum (confirmed by main, 2026-11-18 09:49):
`mobile_request` and GET request metadata may include `error: {code} | null`.
Only the fixed error-code allowlist is allowed, without tokens, health values,
or freeform text.

## Amendment 3 (confirmed by main, 2026-11-18 09:34)

`pair` response `instance.id` is the instance ID from the Cloud instance catalog.
The instance obtains it from `V2Config.remote_access.vibe_cloud.instance_id`,
checks it against the live Permissions projection's `instance.id`, then returns
that exact ID. No locally generated fallback ID is used. Missing catalog ID, incomplete runtime credentials, a non-live or owner-less
answer, or a mismatched instance ID returns HTTP 503
`instance_eligibility_unavailable` with `Retry-After: 30`; the phone pauses and
no data is purged. Only a live, confirmed owner change (HTTP 403
`instance_not_eligible`), an explicit unpair or a user revoke revokes the
device and purges its data. Instance kind and sharing are not checked.
The pairing target comes only from the trusted instance currently selected in
the iOS app; the webpage cannot supply a target.

## Decision update (Max, November 20, 2026, 09:01)

"组织实例接健康数据 不需要限制实例权限，权限用户自己限制": personal and
organization instances, shared or not, let the instance owner pair their own
iPhone and sync. Who may reach the instance (and its Agents, and therefore the
synced data) is controlled by the owner through the instance's access settings.
This supersedes the November 19 direction that organization instances must prove
owner-only visibility or same-machine isolation. That design (owner attestation,
Turn capability, private session scope), the isolation gap inventory and the
isolation probe remain as history and are not release prerequisites. The
November 18 rule that every Agent in the instance may read is unchanged.
