# iOS system schedule implementation

Date: 2026-11-17
Base: `origin/master@2a7ab2841aafd594e26e569ad5452dcbe28c9614`
Branch: `feat/ios-system-schedule`

Revision note: the 06:24 simplification after the third circuit breaker supersedes earlier compensation/UserDefaults/inferred-detach design in this plan. The 09:45 bounded execution amendment below closes round-five F1–F3 at baseline `7b2c7e1f493e29d7079c197be10ade5c18caca3e` (review base `e708e3ecf4c7d9f9d120e0be40e1705613c07910`); it does not change Runtime/Web contracts or user semantics.

## Goal

Add one trusted, product-level iOS bridge capability for user-confirmed system schedules. A trusted primary Runtime document can inspect availability, request a one-time alarm/reminder, and cancel a still-managed item. Native code owns confirmation, permission, OS submission, receipts, idempotency, and account/runtime lifetime fences.

## Frozen bridge contract

The shell advertises `systemSchedule: 1` and accepts only:

- `systemSchedule.capabilities` with no parameters.
- `systemSchedule.request` with exactly `requestId`, `operationId`, `kind`, `title`, `fireAt`, `timeZone`, `recurrence`, `instanceId`, `sessionId`, and `expiresAt`. `recurrence` is `null` in this slice.
- `systemSchedule.cancel` with exactly `requestId`, `operationId`, `kind`, and `purpose`. `purpose` is fixed to the closed value `user`; Runtime does not create compensation attempts and no raw platform API is exposed.

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

## 06:24 simplification

The implementation now follows seven reductions from the authoritative state machine:

1. replace the schedule ledger's `UserDefaults` cache with one App-container file committed by temp-file write, file `fsync`, same-directory atomic `rename`, and directory `fsync`;
2. detach only on explicit user logout or authenticated different-account activation, using the ledger's persisted `activeOwner`; bootstrap failure, server-signed-out, 2FA, missing snapshot, and cleared selection preserve ownership while exposing no Runtime authority;
3. remove Runtime compensation generation; Native retries exact cleanup-pending records on launch, foreground, and every schedule bridge claim;
4. user cancel means “ensure the deterministic item is absent” for managed, uncertain-create, and cleanup-pending states; detached-retained stays unmanaged;
5. conclusive absence during recovery commits a terminal no-item tombstone and never re-enters create;
6. deliver receipts per lease outside the claim mutex with a request timeout;
7. compare and return an existing Runtime operation before applying first-create time validation.

The cross-layer regression suite names the committed-head finding ID (`C*`, `D*`, or `E*`) beside every added race test.

## 09:45 bounded execution amendment

- **F2 / admission:** `NativeBridge.handleSystemSchedule` captures authority synchronously after main-frame/exact-origin/primary-WebView checks, before creating its Task. The immutable admission contains account/owner epoch, instance, Runtime identity, a per-WebView-lifetime UUID and navigation epoch. Later AppModel fences only validate that snapshot; validation never creates an owner epoch. Native closure wiring carries the snapshot rather than an origin to resolve later.
- **F1 / execution:** the existing coordinator owns one in-memory execution slot per `(accountID, ownerEpoch, instanceID, operationID)`. Admission installs the slot before its driver Task. Each OS suspension has one running-effect token and an observed completion. Create, recovery, cancel and lifecycle cleanup join the same slot; cancellation and synchronous exact-owner detach revoke creation immediately. No absent lookup can settle an outstanding create. AppModel owns confirmation presentation and delivery, not OS arbitration. A bridge claim retries other pending operations; its own exact operation is settled only by its slot, not retried again by that same claim's lifecycle sweep.
- **F3 / completion:** required commits use the unchanged fsync/rename file writer. Slots retain OS observation separately from commit failure and fan out one settled result under each waiter's request ID. No successful receipt precedes its required terminal commit. An unresolved `local_state_write_failed` cannot be replaced with an older ledger receipt; explicit recovery re-enters the slot from durable uncertainty and re-commits a visible-but-failed terminal row before replay. A failed pre-create reservation still requires phone confirmation. AppModel no longer derives cancel outcomes from the ledger. Confirmation-only retry continuations simply deliver the coordinator's result.
- **Preserved boundary:** durable `managedScheduled` before exact-owner detach remains `detachedRetained` forever. Losing authority while create can still land requests exact-item local cleanup. Re-login can wait for historical cleanup evidence but never regain management. MobileAccount's synchronous exact-owner detach is unchanged; network, 2FA and unknown account state still do not detach.

The three baseline counterexamples are captured as tests before product changes. F2's pre-fix cleanup suspension is removed from the request path; its post-fix checks therefore inject the frozen admission before the suspension/replacement instead. Its fixture forwards real WKNavigation callbacks, and a separate real-WebKit bridge test asserts admission runs outside a Swift Task before synchronously reopening the Runtime. No product-only testing hooks were added.

| Matrix | Owning evidence (`SystemScheduleTests` unless noted) |
| --- | --- |
| 1 | `testConcurrentRetryJoinsIssuedCreateWithoutRecoveryLookup` |
| 2 / F1 | `testInFlightCreateOwnsLateCompletionAcrossDetachAndCleanup` |
| 3 | `testUserCancelWhilePlatformCreateIsInFlightRemovesTheCreatedItem`; App `testTwoBridgeCancelsDismissInFlightConfirmationBeforeOSCompletion` |
| 4 | `testRemoveCompletionIsSharedByCancelAndForegroundCleanup` |
| 5 / F2 | App `testCancelDoesNotAdoptDocumentAuthorityAfterCleanupSuspension`, `testBridgeFreezesAuthorityBeforeItsTaskForCreateAndCancel` |
| 6 | `testCancelRevokesBeforeSuspendedPermissionReturns`; existing App permission/Runtime-replacement and late-success tests |
| 7 | `testRequiredIntentCommitFailurePreventsDependentOSWrites` (real file `beforeRename` fault) |
| 8 / F3 | `testCleanupTerminalCommitFailureReachesCreateReceipt`, `testCleanupCommitFailureFansOutAndRestartRecoversDurablePending`, `testVisibleButFailedTerminalCommitMustBeRecommittedBeforeReplay`; existing Web sticky-write-error test |
| 9 | `testCleanupCommitFailureFansOutAndRestartRecoversDurablePending` (second coordinator reopens actual ledger) |
| 10 | `testDetachIsPermanentAndReloginGetsANewOwnerEpoch`; existing App lifecycle cleanup and MobileOnboarding ambiguous-auth tests |

The slot tests use controlled continuations and explicit admission signals, with effect counts, persisted lifecycle, per-request errors/identities and exactly-once completion assertions. Simulator/fixture and unsigned-build evidence is not real-device AlarmKit/EventKit evidence. This round freezes one repair head and obtains the original independent review before any PR, release or installation; recurring execution/authority/commit findings stop the lane.

## Review-fix invariants

These invariants close the defect classes found by the first read-only review. Tests assert the properties rather than only the reported examples.

### Source and context fencing

- A schedule request is authoritative only while its retained primary WebView is the selected instance's exact origin **and no primary navigation is provisional**. Authority is revoked at navigation start, before `WKWebView.url` changes, and is restored only after a committed/finished trusted main-frame document establishes the new context.
- Confirmation, submission, and every async platform completion re-check the same account/instance/origin/Runtime/WebView-navigation authority. A retained old URL is never sufficient authority during a provisional navigation.
- Retries are identified by the stable operation intent, not the transport request ID. A new request ID for the same pending operation joins the same confirmation and waits for its terminal result; a changed intent under the same operation ID fails closed.

### OS creation, persistence, and recovery

- The Native ledger is one crash-durable App-container file, not `UserDefaults`. Every reservation and tombstone completes temp write + file `fsync` + atomic `rename` + directory `fsync` before dependent OS work or bridge receipt.
- An OS item has a deterministic identity scoped by the selected Avibe instance and operation before the system call. Two instances using the same Agent-chosen operation ID cannot collide in the iPhone's system namespace. Recovery can find the corresponding AlarmKit/EventKit item after a crash between OS acceptance and local persistence, so retry never blindly creates a duplicate.
- A locally persisted `scheduled` state is written only after the system accepted the item. If the final commit fails, the earlier crash-durable `creatingUnknown` record remains sufficient for deterministic recovery; Web does not submit that `local_state_write_failed` result as a terminal Runtime receipt, so the same sticky lease re-enters Native recovery on a later poll.
- Every cleanup failure retains the real opaque system identifier and a cleanup-pending recovery record. Native retries these records on launch, foreground, and every bridge create/cancel claim.
- A waiting user-cancel request is settled from the coordinator's execution outcome and required commit outcome, independently of the immutable create outcome. Cleanup failure reports a failed cancel with the identifier; only proven absence/removal plus a successful terminal commit reports `cancelled`. A failed commit is delivered unchanged to every affected create/retry/cancel waiter, never reconstructed from the older pending record.
- Completed no-item/removed cleanup tombstones replay idempotently after Web/receipt loss, while deliberately retained detached items never become cleanup targets.
- Runtime keeps `(operationId, action, generation)` unique through a canonical `attempt_key` stored in a literal unique column on the new table, so the repository's existing additive analyzer proves the constraint without an analyzer exception.
- Permission-denied and unsupported platform results atomically persist and return the authorization observed by that result; an earlier `not_determined` capability snapshot cannot leak into the terminal receipt.
- EventKit cancellation never treats an unstable `calendarItemIdentifier` miss as proof of absence. It recovers by stable operation metadata/identity and reports `cancelled` only after the matching item is absent or removal succeeds.
- A persisted `creatingUnknown` record enters stable-identity recovery before request-entry expiry/fire-time guards. Exists converges to managed or local-cleanup state, conclusive absence commits a terminal no-item tombstone with zero create writes, and unknown retains the original identity. Recreating requires a new operation and user confirmation.

### Account-transition timing

- Account transition revokes schedule authority and dismisses/settles pending confirmation synchronously before any awaited push/logout cleanup.
- Only explicit user logout or authenticated different-account activation detaches. Bootstrap failure, server-signed-out, 2FA, missing snapshot, and cleared instance selection preserve the persisted owner and records while exposing no Runtime authority.
- Explicit logout detaches using the ledger's persisted `activeOwner`, even when the in-memory snapshot and stored selection are absent.
- If a `creatingUnknown` existence lookup returns after explicit logout detached its record, Native reloads the exact record and reconciles exists/absent/unknown through local cleanup states; it never returns an identifier-less stale result while cleanup may remain.
- Before a new owner epoch reserves a create, Native checks the exact instance+operation history across owners. A detached or closed operation returns its immutable prior outcome (or stale cleanup identity) and cannot create a second AlarmKit/EventKit item after re-login.

### Runtime producer state machine

- Runtime terminal states are monotonic. Expiry, cancellation, denial, failure, unsupported, and stale-context states cannot be revived by a late receipt.
- Runtime expires only an unclaimed create. A claimed create that crosses its deadline remains nonterminal as `awaiting_device`; the matching device receipt later writes the single terminal outcome normally. Receipt ordering never generates Native cleanup work in Runtime.
- Runtime compares an existing operation's canonical immutable intent and returns its current result before applying `fire_time_in_past`; time validation is only for first insertion.
- A claimed create must be cancelled by its existing `operationId`; the Agent must not create a replacement operation while the status is `awaiting_device`.
- A receipt advances state only when operation, request, action, instance, device, lease token, current lease state, and immutable intent all match. Exact duplicate receipts are idempotent; conflicting or stale receipts fail closed.
- The first device claim is sticky for the operation lifetime. A second device never takes over an expired/in-flight request, and cancellation of a created item routes only to the creating device. A cancellation claim deadline expires only an unclaimed request; once the device has claimed it, the lease remains current until its terminal receipt so a successful late OS removal is not discarded as `expired`.
- Sticky polling returns only nonterminal attempts. Once user cancellation terminalizes a claimed create, the original in-flight device may still submit its receipt as evidence, but Runtime never dispatches that create lease again while the cancel is in flight, after cancel failure, or after cancel success.

### Accessibility

- Presenting the blocking confirmation moves VoiceOver focus into the modal card and announces the changed screen. Dismissing it restores focus to the underlying Runtime surface.


- NativeBridge keeps its existing main-frame + exact-origin policy. WebViewController additionally grants schedule handlers only to the retained primary WebView.
- AppModel binds a pending request to the current account ID, instance ID, exact origin, Runtime context, and `runtimeIdentity`. The request-provided session ID is retained and shown in confirmation.
- Instance replacement, account transition/logout, Runtime cancellation/timeout, or any retired opening generation invalidates the request context. A request not yet submitted resolves as `stale_context`. An in-flight system call loses creation authority, hides the confirmation, and waits for the platform adapter's stale-context cleanup result before releasing its native continuation.
- A late OS success after cancellation or exact context/owner loss is handled through Native local cleanup before success is claimed. If cleanup cannot be proven, Native retains the deterministic identity and a cleanup-pending ledger record. Later wall-clock deadline crossing after an accepted OS write does not trigger a new Runtime attempt.
- One native confirmation is active at a time. While a create is awaiting confirmation, Web continues claiming user-cancel work but tells Runtime not to lease another create. Receipt HTTP calls run independently per lease outside the claim mutex and have explicit timeouts. A Native `local_state_write_failed` result is not queued as terminal evidence: the lease remains sticky and is retried through Native recovery on the next poll/reactivation.
- Native rechecks both `now < expiresAt` and `now < fireAt` before presentation, after confirmation, after permission/lookup awaits, and immediately before the OS create write. Once that write has happened, later wall-clock deadline crossing cannot rewrite the device result or trigger cleanup.

## Idempotency and retained items

A versioned crash-durable App-container file records Avibe-created system identities and permanent lifecycle tombstones. A management owner is the exact `(accountId, ownerEpoch)` generation, and each item identity also includes the selected instance and operation. The ledger key and every local cleanup record use the exact `(accountId, ownerEpoch, instanceId, operationId)` tuple, so two instances may safely use the same Agent operation ID without overwriting or cleaning up each other's records. Repeating an accepted operation under that exact owner returns its existing terminal receipt instead of creating another item. User cancellation is idempotent “ensure absent” for known managed/uncertain/cleanup-pending records. An unreadable or future-version ledger disables creation and cancellation with `local_state_unavailable`; it is not overwritten.

On explicit logout, completed records for the persisted active owner become permanently detached without deleting the AlarmKit alarm or EventKit reminder. A create still in flight becomes exact local cleanup work. Server-signed-out bootstrap and 2FA do not detach. Signing in to a different account detaches before new Runtime authority; signing back into the same preserved lifecycle reuses the owner unless an explicit logout already detached it.

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

1. Shared contract vectors in `tests/fixtures/system_schedule_contract_vectors.json` are consumed by Python, TypeScript, and Swift and cover ASCII/non-ASCII titles, millisecond timestamps, the user-cancel shape, and every terminal receipt state.
2. The Runtime migration uses only repository-mechanically-proven additive Alembic operations, and the historical epoch-3 tests run against a fixture graph pinned through `20261114_0089` instead of the moving repository head.
3. Parser, authorization mapping, OS adapter, store, idempotency, cancellation, detach/logout, and stale-callback unit tests.
4. NativeBridge method allowlist, metadata, exact-origin/main-frame/primary-WebView tests.
5. AppModel confirmation and generation/account/instance invalidation tests.
6. Focused XCTest, full owning XCTest, and unsigned simulator/device-architecture builds locally.
7. Before Internal TestFlight: send main the exact new copy list, phone acceptance route, and Xcode 26.6/profile gate status.
8. Because the signed TestFlight workflow is master-only, open and fully gate the PR first; after main merges it, dispatch the merge commit to Internal TestFlight and keep real-device behavior as a separate evidence layer.
