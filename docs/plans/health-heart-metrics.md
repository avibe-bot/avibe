# Heart metrics — implementation contract

2026-11-27 · Baseline: `origin/master` **79ad754c53** · Status: **approved by main 2026-11-27; implemented without sample counts**.

**Replacement-connect decision.** A synthetic baseline test proves replacement deletes retained November 1 data which HealthKit can no longer return (deleted samples or revoked read access); an empty 30-day backfill cannot restore it. Per main’s conditional approval, retain the same-device expansion, backfilling only the added metrics while existing metrics keep their ordinary 8-day window.

**Outcome.** Add three daily HealthKit summaries while preserving the existing four metrics, connection, retention, owner binding and pause/cleanup policy. Deliver **one PR** containing instance, iOS and the small Web bridge compatibility change, so one reviewed contract covers all consumers. No real health data, merge, deployment, TestFlight or signing changes.

## 1. Daily data

| Key | `value` when `availability:"ok"` | Query and multiple-source rule |
|---|---|---|
| `heart_rate` | `{min_bpm, average_bpm, max_bpm}` | `heartRate`; daily `HKStatisticsCollectionQuery` with `.discreteMin/.discreteAverage/.discreteMax`, bpm. Use HealthKit's combined statistics across readable sources; no invented preferred-device ranking. |
| `resting_heart_rate` | `{bpm}` | `restingHeartRate`; choose the day's latest readable sample by end time, then start time, source bundle ID and UUID for deterministic ties. This is the latest daily estimate, not the minimum of ordinary heart-rate readings; multiple sources are not averaged. |
| `hrv` | `{sdnn_ms}` | `heartRateVariabilitySDNN`; daily `.discreteAverage`, milliseconds, combined readable sources. This is SDNN, not RMSSD. |

Use the existing single Gregorian/calendar/timezone snapshot per query and local midnight buckets (calendar-day addition, including DST); today ends at query time. Obtain real sample freshness separately, never from statistics bucket ends. No sample counts or quantity-series enumeration. Do not claim the summary matches Apple Health's presentation/source-priority policy. No source whitelist or Watch-presence test: readable third-party samples count too.

Each queried metric/day has one row. No readable data → `empty`, `value:null`; no upload → `not_synced`. Preserve `latest_sample_end_at`, `queried_at`, instance-generated `received_at`, and `closed`. Numeric values must be finite/nonnegative with min ≤ max. iOS clamps the mean to that interval; the instance allows at most 0.001 bpm of mean overshoot to absorb independent floating-point unit conversions. The fixed bound cannot grow with the value, never permits reversed extrema, and still rejects a 0.01 bpm discrepancy. Query failure retains the previous metric data. Existing 8-day resync and 30-day initial backfill also refresh corrected resting estimates.

## 2. Existing users: explicit expansion, without reconnecting

Example: an upgraded, already-connected phone keeps syncing the original four. An Agent asks for HRV; `read` returns `not_synced` plus metadata indicating the type is outside the device's **Avibe-confirmed metrics**. The Agent requests `connect`; notification/card opens the existing v4 confirmation, then `requestAuthorization(toShare:[], read:...)`. Only an explicit foreground confirmation reaches HealthKit. Launch/resume never prompts.

HealthKit hides read-grant status: neither `authorizationStatus(for:)` nor empty samples prove refusal. `getRequestStatusForAuthorization` can indicate whether another sheet is needed, not whether access was granted. After the permission request completes, record the types as *requested/confirmed*, even if reads are empty; do not repeatedly prompt from empty results.

`connect` always requests all instance-supported metrics not yet confirmed, or all supported metrics for an initial connection. There is no selective connect flag (it never shipped); `read --metric` remains selective. One owner/instance has at most one pending connect, including a paired request awaiting its first sync. Repeated requests return the original request and continuation. Pairing or expansion settles any other legacy pending connects in the same transaction: covered cards succeed without fabricated data results, inapplicable cards cancel, and their codes are retired. The winning request still requires its normal upload/ACK/result.

Keep `health.connect` and its one-time code, but add a **same-device expansion** path: authenticated `PUT /manifest` may consume that code, validate its pending connect request/owner/instance, atomically union the confirmed metrics and bind that request to the current device. It never revokes/re-pairs or resets sequences/history/outbox. Ordinary manifests cannot expand consent. Persist the user's approved expansion and mark its manifest due before requesting the existing convergence owner. That owner serializes manifest HTTP, applies revocation/suspension and Retry-After policies, and retries independently of the native reply deadline. On a lost response, reconcile server-confirmed metrics and request binding before retrying. Decline, expiration, timeout and interruption preserve the old connection. Resume existing observers/outbox; observe/query only confirmed, negotiated metrics; backfill the newly enabled types for 30 days. Success still requires upload ACK followed by accepted status-only results.

## 3. Capability and version contract

New instances add `supported_metrics` to pairing-code responses and manifest responses; the Web bridge forwards the pairing metadata to new iOS. Absence means exactly the legacy four. New iOS advertises native `health:2`; expansion cards require 2 so old Apps cannot enter their destructive replacement-connect path. First pairing remains compatible with `health:1`. New iOS persists confirmed metrics (old records decode as the legacy four), and learns instance support before building pair/manifest/upload payloads. Newly supported types do not become consented automatically.

| App / instance | Behavior |
|---|---|
| Old / old | Existing four-type behavior. |
| New / old | Missing capability list → four only; confirmation, authorization, pair, manifest, upload window, failures and result keys all use that subset. |
| Old / new | Four continue; new types read `not_synced`; expansion card asks for an App update. No new metric is injected into old-device refresh claims. |
| New / new | Fresh consent may include seven; existing devices retain four until explicit expansion. Refresh claims use device-confirmed metrics; results exactly match each claim. |

Filter before durable enqueue; preserve immutable queued request/upload scopes. Persist an approved pending expansion before HTTP; manifest retries reconcile lost acknowledgements, while old uploads retain their original metric sets. Capability lookup/transport failure is not evidence of consent or an instruction to erase data.

## 4. Copy changes (original → replacement)

Keep the v4 title, destination, address, access text, buttons and layout. The first card renders the negotiated list (seven types or the three additions). Per Max’s 2026-11-27 16:54 decision, the second card uses the same short summary for both scopes.

| Surface | Original → replacement |
|---|---|
| `health.metrics` EN / ZH | “Steps, activity, workouts, and sleep” → “Steps, activity, workouts, sleep, heart rate, resting heart rate, and heart rate variability”; “步数、活动、锻炼和睡眠” → “步数、活动、锻炼、睡眠、心率、静息心率和心率变异性”. Expansion list: “Heart rate, resting heart rate, and heart rate variability” / “心率、静息心率和心率变异性”. |
| `health.summary` EN | “Sync your steps, activity, workouts, and sleep to %@, keeping the last 30 days.” → “Sync this data to %@, keeping the last 30 days.” |
| `health.summary` ZH | “将你的步数、活动、锻炼和睡眠数据同步到 %@，保留最近 30 天。” → “将这些数据同步到 %@，保留最近 30 天。” |
| `NSHealthShareUsageDescription` (Info.plist + localized strings) | EN replace “steps, activity, workouts, and sleep” with “steps, activity, workouts, sleep, heart rate, resting heart rate, and heart rate variability”; ZH replace “步数、活动、锻炼和睡眠” with “步数、活动、锻炼、睡眠、心率、静息心率和心率变异性”. All remaining wording unchanged. |
| Devices details and read activity | Four-type adapter/filter/labels → seven types in linked functions, unlinked connections and audit entries; unknown types remain filtered. |
| Expansion on an old App | Generic unsupported-Health note → “Update Avibe on this iPhone to add these. Your current sync keeps running.” / “更新这台 iPhone 上的 Avibe 后才能添加这些数据，现有同步不受影响。” Keep the original note for unsupported first connections. |
| Web cards and notification landing | Raw heart metric keys / a fixed four-type sentence → localized labels and the actual request metric list in English and Chinese. The native v4 layout remains unchanged. |
| Skill description | “Read steps, activity, workouts and sleep synced…” → “Read steps, activity, workouts, sleep, heart rate, resting heart rate and heart rate variability synced…”. |
| Skill authorization guidance | “Authorization happens once…” → “Confirm additional metrics on iPhone when requested. Use connect to confirm all supported metrics outside the confirmed set; empty data does not reveal HealthKit permission status.” Add the exact shapes/units, SDNN meaning, multi-source rules and connection CLI example. |

## 5. Changed behavior and evidence

Four supported keys → seven; connected `connect` no-op → no-op only when all supported types are already confirmed; missing new rows → actionable confirmation metadata without inventing permission state; fixed four-type iOS query/payload/observer list → negotiated confirmed subset; replacement-only connect → preserve-device expansion; fixed confirmation list → actual requested list. The original four aggregation semantics remain unchanged.

Tests first: synthetic upload/CLI/results round trip, empty vs zero, malformed values, multiple sources and resting tie-breaks, DST/midnight samples, all four version combinations, legacy persisted-state decoding, expansion decline/timeout/lost response/relaunch, and old-four synchronization during pending expansion. Use a disposable cloned simulator only, ≥20 GiB free and no other xcodebuild; remove it afterward. One exact-head Codex pass, zero unresolved threads, all required CI green and CLEAN before handback. No claim of real Watch/background-delivery verification.

**Evidence:** source inspected at the baseline above; Apple documentation fetched 2026-11-27 via its documentation JSON: `authorizing-access-to-health-data`, `hkhealthstore/getrequeststatusforauthorization(toshare:read:completion:)`, `hkquantitytypeidentifier/{heartRate,restingHeartRate,heartRateVariabilitySDNN}`, `hkquantitysample/count`, and `hkstatisticsoptions/discreteaverage`.
