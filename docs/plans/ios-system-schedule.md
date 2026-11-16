# iOS system schedule implementation

Date: 2026-11-16
Base: `origin/master@db0fa462bcf28d885c780d6f7435cc6aa128a0af`
Branch: `feat/ios-system-schedule`

## Goal

Add one trusted, product-level iOS bridge capability for user-confirmed system schedules. A trusted primary Runtime document can inspect availability, request a one-time alarm/reminder, and cancel a still-managed item. Native code owns confirmation, permission, OS submission, receipts, idempotency, and account/runtime lifetime fences.

## Frozen bridge contract

The shell advertises `systemSchedule: 1` and accepts only:

- `systemSchedule.capabilities` with no parameters.
- `systemSchedule.request` with exactly `requestId`, `operationId`, `kind`, `title`, `fireAt`, `timeZone`, `recurrence`, `instanceId`, `sessionId`, and `expiresAt`. `recurrence` is `null` in this slice.
- `systemSchedule.cancel` with exactly `requestId`, `operationId`, `kind`, and `purpose`. `purpose` is the closed internal control value `user` or `compensation`; it does not expose a raw platform API or widen the Agent product surface.

`kind` is `alarm` or `reminder`. The first slice exposes exactly one creation path per OS: iOS 26+ supports fixed-time AlarmKit alarms; iOS 17–25 supports EventKit reminders. The capability response names only the product path supported on that OS and maps framework authorization to `not_determined`, `authorized`, `denied`, `restricted`, or `unsupported`. A reminder created before an OS upgrade can still be cancelled through its retained record, but iOS 26 does not advertise or create new reminders.

A receipt has contract version 1, request/operation/kind identity, `state`, the requested fire instant and time zone, current authorization, an opaque system identifier when one exists, and a stable error object when applicable. Terminal receipt states are:

- `user_denied`
- `permission_denied`
- `unsupported`
- `scheduled`
- `cancelled`
- `failed`
- `expired`
- `stale_context`

`scheduled` is emitted only after the OS API accepts the item. `awaiting_device` is not a Native receipt state: it is the Agent/CLI operation state shown when a create was already claimed by its bound iPhone, its claim deadline has passed, and the matching terminal receipt has not arrived yet. The operation includes `status_message: "Waiting for your iPhone to return the result."`.

## Review-fix invariants

These invariants close the defect classes found by the first read-only review. Tests assert the properties rather than only the reported examples.

### Source and context fencing

- A schedule request is authoritative only while its retained primary WebView is the selected instance's exact origin **and no primary navigation is provisional**. Authority is revoked at navigation start, before `WKWebView.url` changes, and is restored only after a committed/finished trusted main-frame document establishes the new context.
- Confirmation, submission, and every async platform completion re-check the same account/instance/origin/Runtime/WebView-navigation authority. A retained old URL is never sufficient authority during a provisional navigation.
- Retries are identified by the stable operation intent, not the transport request ID. A new request ID for the same pending operation joins the same confirmation and waits for its terminal result; a changed intent under the same operation ID fails closed.

### OS creation, persistence, and recovery

- An OS item has a deterministic identity scoped by the selected Avibe instance and operation before the system call. Two instances using the same Agent-chosen operation ID cannot collide in the iPhone's system namespace. Recovery can find the corresponding AlarmKit/EventKit item after a crash between OS acceptance and local persistence, so retry never blindly creates a duplicate.
- A locally persisted `scheduled` state is written only after the system accepted the item. If persistence fails, Native either removes the accepted item or retains enough durable cleanup ownership to retry later; it never reports a clean failure while forgetting a live item.
- Every cleanup failure retains the real opaque system identifier and a managed recovery record. Subsequent retry/cancel can address the same item; no path discards the only handle to a live system item.
- A waiting user-cancel/compensation request is settled from the exact persisted cleanup record, independently of the immutable create outcome. Cleanup failure reports a failed cancel with the identifier; only proven absence/removal reports `cancelled`.
- Completed no-item/removed cleanup tombstones are replayable idempotently after Web/receipt loss, while deliberately retained detached items never become compensation targets.
- Runtime keeps `(operationId, action, generation)` unique through a canonical `attempt_key` stored in a literal unique column on the new table, so the repository's existing additive analyzer proves the constraint without an analyzer exception.
- Permission-denied and unsupported platform results atomically persist and return the authorization observed by that result; an earlier `not_determined` capability snapshot cannot leak into the terminal receipt.
- EventKit cancellation never treats an unstable `calendarItemIdentifier` miss as proof of absence. It recovers by stable operation metadata/identity and reports `cancelled` only after the matching item is absent or removal succeeds.
- A persisted `creatingUnknown` record enters stable-identity recovery before request-entry expiry/fire-time guards. Exists plus expired/cancelled authority is compensated, conclusive absence closes without another OS write, and unknown retains the original identity; the App never shows a second confirmation for that recovery.

### Account-transition timing

- Account transition revokes schedule authority and dismisses/settles pending confirmation synchronously before any awaited push/logout cleanup.
- Cold-start detachment requires a confirmed signed-out or different-account bootstrap result. Network/server/decoding failure preserves the persisted owner epoch and ledger unchanged while exposing no Runtime authority.
- If a `creatingUnknown` existence lookup returns after logout detached its record, Native reloads the exact record and reconciles exists/absent/unknown through compensation states; it never returns an identifier-less stale result while cleanup may remain.
- Logout detaches ownership using the persisted departing account identity when the in-memory snapshot is absent. Once detached, records do not become managed again merely because the same account signs in later.
- Before a new owner epoch reserves a create, Native checks the exact instance+operation history across owners. A detached or closed operation returns its immutable prior outcome (or stale cleanup identity) and cannot create a second AlarmKit/EventKit item after re-login.

### Runtime producer state machine

- Runtime terminal states are monotonic. Expiry, cancellation, denial, failure, unsupported, and stale-context states cannot be revived by a late receipt.
- Runtime expires only an unclaimed create. A claimed create that crosses its deadline remains nonterminal as `awaiting_device`; the matching device receipt later writes the single terminal outcome normally. Receipt transport delay alone never creates compensation.
- A claimed create must be cancelled by its existing `operationId`; the Agent must not create a replacement operation while the status is `awaiting_device`.
- A receipt advances state only when operation, request, action, instance, device, lease token, current lease state, and immutable intent all match. Exact duplicate receipts are idempotent; conflicting or stale receipts fail closed.
- The first device claim is sticky for the operation lifetime. A second device never takes over an expired/in-flight request, and cancellation of a created item routes only to the creating device. A cancellation claim deadline expires only an unclaimed request; once the device has claimed it, the lease remains current until its terminal receipt so a successful late OS removal is not discarded as `expired`.
- Sticky polling returns only nonterminal attempts. Once user cancellation terminalizes a claimed create, the original in-flight device may still submit its receipt as evidence, but Runtime never dispatches that create lease again while the cancel is in flight, after cancel failure, or after cancel success.

### Accessibility

- Presenting the blocking confirmation moves VoiceOver focus into the modal card and announces the changed screen. Dismissing it restores focus to the underlying Runtime surface.


- NativeBridge keeps its existing main-frame + exact-origin policy. WebViewController additionally grants schedule handlers only to the retained primary WebView.
- AppModel binds a pending request to the current account ID, instance ID, exact origin, Runtime context, and `runtimeIdentity`. The request-provided session ID is retained and shown in confirmation.
- Instance replacement, account transition/logout, Runtime cancellation/timeout, or any retired opening generation invalidates the request context. A request not yet submitted resolves as `stale_context`. An in-flight system call loses creation authority, hides the confirmation, and waits for the platform adapter's stale-context cleanup result before releasing its native continuation.
- A late OS success after cancellation or exact context/owner loss is cancelled before Native returns. If cleanup cannot be proven, Native retains the deterministic identity and a cleanup-pending ledger record instead of claiming the item was removed. Merely crossing a claimed request deadline after an OS write is not context loss: that device result remains `scheduled`, and no compensation runs.
- One native confirmation is active at a time. While a create is awaiting confirmation, Web continues claiming cancellation work but tells Runtime not to lease another create. A retry of the same pending operation joins the existing confirmation and receives its eventual terminal result; a conflicting direct bridge operation receives `failed/confirmation_in_progress`.
- Native rechecks both `now < expiresAt` and `now < fireAt` before presentation, after confirmation, after permission/lookup awaits, and immediately before the OS create write. Once that write has happened, later wall-clock deadline crossing cannot rewrite the device result or trigger cleanup.

## Idempotency and retained items

A versioned UserDefaults-backed installation-local store records Avibe-created system identities and permanent lifecycle tombstones. A management owner is the exact `(accountId, ownerEpoch)` generation, and each item identity also includes the selected instance and operation. The ledger key and every compensation lookup use the exact `(accountId, ownerEpoch, instanceId, operationId)` tuple, so two instances may safely use the same Agent operation ID without overwriting or cleaning up each other's records. Repeating an accepted operation under that exact owner returns its existing terminal receipt instead of creating another item. Cancellation is idempotent for known records. An unreadable or future-version store disables creation and cancellation with `local_state_unavailable`; it is not overwritten, and the trusted bridge returns that same actionable error instead of misclassifying the request as stale or forbidden.

On logout, completed records for the departing owner become permanently detached without deleting the AlarmKit alarm or EventKit reminder. The system item remains, and Avibe can no longer read, cancel, or otherwise manage it. A create that is still in flight when logout detaches its owner retains only exact compensation authority, so a later OS success is removed rather than becoming managed. Signing in again — even to the same account — creates a new `ownerEpoch` and cannot reclaim detached records. Instance switching without logout only removes current document authority; returning to the same signed-in owner and selected instance can manage its recorded item.

## Platform implementation

- iOS 26+: fixed-date `AlarmManager.schedule`, with product authorization mapping and `AlarmManager.cancel`.
- iOS 17–25: `requestFullAccessToReminders`, default Reminders calendar, explicit due components/time zone, absolute `EKAlarm`, and exact matching by the deterministic Avibe metadata URL. Lookup returns `exists`, conclusively absent, or unknown; unknown never authorizes duplicate creation or claims successful cancellation.
- App target only: add localized `NSAlarmKitUsageDescription` and `NSRemindersFullAccessUsageDescription`. NotificationService and entitlements remain unchanged.
- If the pinned Xcode 26.6 archive or Apple signing path requires an AlarmKit capability/profile change, stop and report it; do not change Portal state or signing assets in this branch.

## User confirmation

RootView presents a native blocking confirmation card above the current Runtime. It shows:

- alarm vs reminder;
- title;
- exact date/time in the requested time zone;
- current instance;
- originating session ID;
- system-owned behavior disclosure.

The user can cancel or create the item. Submission remains visibly in progress while permission/OS work is running. All copy is localized in English and Simplified Chinese and must be reviewed by main before Internal TestFlight.

## Validation

1. Shared contract vectors in `tests/fixtures/system_schedule_contract_vectors.json` are consumed by Python, TypeScript, and Swift and cover ASCII/non-ASCII titles, millisecond timestamps, both cancel purposes, and every terminal receipt state.
2. The Runtime migration uses only repository-mechanically-proven additive Alembic operations, and the historical epoch-3 tests run against a fixture graph pinned through `20261114_0089` instead of the moving repository head.
3. Parser, authorization mapping, OS adapter, store, idempotency, cancellation, detach/logout, and stale-callback unit tests.
4. NativeBridge method allowlist, metadata, exact-origin/main-frame/primary-WebView tests.
5. AppModel confirmation and generation/account/instance invalidation tests.
6. Focused XCTest, full owning XCTest, and unsigned simulator/device-architecture builds locally.
7. Before Internal TestFlight: send main the exact new copy list, phone acceptance route, and Xcode 26.6/profile gate status.
8. Because the signed TestFlight workflow is master-only, open and fully gate the PR first; after main merges it, dispatch the merge commit to Internal TestFlight and keep real-device behavior as a separate evidence layer.
