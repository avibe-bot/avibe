# Native HealthKit delivery

## Change contract

Implement HealthKit v2 S3 against the orchestrator-owned
`healthkit-v2-one-way-read.md` (2026-11-18), sections 3/5/7/8 and appendices A–E.
The instance lane owns the canonical English `health-data.md` specification.

Only the configured instance main frame may invoke `health.connect`; its reply
contains a connection state only. All other `health.*` methods remain unsupported.
Native consent precedes read-only system authorization and pairing. Empty results
never imply denied authorization. This phone syncs to one personal instance.

Credentials live in a private, non-synchronizing ThisDeviceOnly Keychain item.
Health payloads and monotonic sequence/request bookkeeping use protected,
backup-excluded app storage. Account changes stop queries, cancel transfers,
remove credentials, and delete local sync state. Device authentication uses no
browser cookies. Upload success precedes successful refresh acknowledgements.

## Validation and release boundary

Use synthetic HealthKit samples, injected credential stores and a loopback fake
instance only. Verify request/reply boundaries, durable sequencing, deduplication,
HTTP failure policy, logout races, and metric algorithms with XCTest. Build with
isolated DerivedData under caffeinate. Do not read real health data, contact the
production instance, reset a simulator, change Apple capabilities/profiles, sign
an archive, publish TestFlight, or merge without separate owner authorization.

Physical-device authorization, protected-data behavior, background wake latency,
and signed archive validation remain explicit integration checks after approval.


## Evidence (2026-11-18)

- Unsigned generic iOS Simulator build and full build-for-testing passed with
  isolated DerivedData. No archive or provisioning profile was inspected.
- The first focused simulator run passed all 82 tests. After strengthening the
  background completion ordering, all 34 HealthKit tests passed; an existing
  notification cleanup test's process exited during overlapping simulator lane
  activity. A final isolated selection passed all 37 tests: 17 reader/consent,
  18 device/sync, the health notification landing path, and that cleanup case. The reader suite passed all 18 tests after adding modern
  workout activity mappings.
- The native consent page was rendered in the simulator against a synthetic
  personal destination and its XCTest image attachment was inspected.
- Archive/profile validation: 113 hermetic pytest cases passed, including strict
  boolean entitlements, extension isolation, and read-only purpose strings.
- Changed Python files passed Ruff; bundled mobile UI preparation passed.

Physical-device HealthKit permissions, real background delivery, signed archive
validation, Apple App ID/profile changes, and TestFlight remain unperformed.


## Review follow-up

The first Codex review identified an observer event that could reuse a query
started before the event. Each sync invocation now waits for the active task and
then runs its own fresh query; task identity prevents an older waiter from
clearing a newer task. A held-reader XCTest proves a second observer completion
waits for a second durable payload. All 19 sync tests passed after this change.

The exact entitlement-set CI test now includes both new boolean HealthKit grants
without weakening its comparison. Its full 79-case pipeline test file and all
113 archive-validator cases passed locally.

The second review identified a restored identity that could outlive account
bootstrap. Every resume now validates the current account independently of the
selected instance, before restoration or connection early returns. Unknown
accounts cannot start queries or device requests. A mismatched account
invalidates pending work and clears the previous connection. Failed credential
deletion leaves a durable cleanup marker and removes payloads, so relaunch can
retry cleanup without reviving the connection. All 22 sync XCTest cases passed,
including a suspended bootstrap request, an account without a selected instance,
and failed logout cleanup followed by relaunch.

The unrelated macOS desktop window probe failed its foreground timing assertion
in CI run 44221510579, matching the existing master failure in run 44184871334.
The orchestrator confirmed this check is not required and instructed this lane
to document it without modifying or rerunning desktop code.


## Identity lifecycle candidate (2026-11-18)

The orchestrator approved one lifecycle correction after independent design
review. A paired device remains provisional until its setup manifest and exact
connect claim validate. A single state replacement records the unexecuted
reservation and committed device ID before publishing the operational identity.
Only committed identities can register observers, query, upload or poll. The
bridge's `connected` reply means the connection is established; the durable
reservation/outbox continues initial sync. A request succeeds only after upload
ACK and accepted result delivery, independently of the bridge reply.

Health account events are separate from push cleanup. Logout and account changes
freeze authority synchronously; same-account two-factor checks pause work while
retaining the committed binding. Fresh nonempty authoritative catalogs can revoke
a removed paired instance without treating another instance's removal as logout.
Before each device request, the list avibe.bot last confirmed in this process must
still list the paired instance at its origin; otherwise (removed or renamed) the
connection ends locally with no further request to that origin. An unknown or
empty list decides nothing.
The existing named WebKit store UUID is captured in the private Health Keychain
identity; missing or mismatching bindings fail closed after account/protected-start
validation. Account UUID generation, rotation timing and transactions are unchanged.

Precommit failures roll back their captured identity before normal sync failure
policy applies. Cleanup removes local credentials immediately, persists the
existing cleanup marker and disables all system Health background delivery even
when this process has no observer. Remote revocation is best effort using only the
captured old credential. Late callbacks cannot mutate a new epoch's identity.

Atomic file replacement is not a power-loss guarantee. If both exiting-marker
persistence and Keychain deletion fail, and no account-store rotation is persisted,
the same visible account may restore an older committed record after restart.
The UUID gate does not repair account initialization when defaults are unreadable.
Offline logout can leave remote historical state until retention or explicit
Agent revocation. These are finite storage/network limits, not stronger guarantees.

The candidate requires fresh focused XCTest, unsigned build and independent
exact-head review; earlier evidence above belongs to the preceding commits.


Fresh candidate validation: unsigned generic Simulator build-for-testing exited
0. The final isolated run passed 58 XCTest cases (37 HealthSync, 18 Reader and
three existing account consumers), including an actual 41-second unacknowledged
upload, setup rollback/relaunch, late epoch failures, protected-start handling,
concurrent reservation recovery, cold system delivery cleanup and account hooks.
The initial 46-case run retained its failed xcresult: its sole failure was the
old bridge expectation that pending upload must return failed. The revised test
requires connected before logout and still forbids successful request receipts.
No physical-device, signing, provisioning, installation or release evidence is
implied. Independent exact-head review and current-head CI remain pending.

## Final integration follow-up (2026-11-18)

Independent review accepted the identity lifecycle candidate, then identified an
ACK arriving during an account authorization pause that could be discarded. ACK
bookkeeping now belongs to the committed connection, independently of temporary
work epochs: the current queue head, committed device marker and captured
connection epoch must match, and the accepted sequence must equal the stored
payload sequence. Authorization still gates every subsequent device request and
result submission. A stale sequence response is not treated as an upload ACK.
Logout or replacement pairing retires the connection epoch, so an old ACK cannot
consume a new connection's payload, even when both use the same sequence.

Reauthentication resumes a paused committed connection once, subject to the
existing epoch, active-connect and restore gates. A real WebKit consumer test
also checks that an invalidated account's runtime cannot reenter health connect.
The exact bridge-method set adds only `health.connect`.

The final integration retains master
`cee7a6e41281d431db28fe269773645088f2deae`, including the Direct rendezvous
source/transport lifetime and the system-schedule wiring. Its mobile resources
were regenerated from source with the documented build and license-generation
commands. The unsigned build-for-testing exited 0.

The complete six-class simulator run executed 358 tests: DirectPath 106,
HealthSync 41, HealthDataReader 18, MobileOnboarding 85 and SystemSchedule 49
passed; NavigationPolicy executed 59 with one unexpected failure in
`testRootViewHandsTheStatusBarToTheSurfaceOnTop`
(`InvalidTransition`, idle to failed(deinit)). The overall exit was 65. The
original 345-test result and the integrated result remain separate; rebuilding
the mobile resources resolved the earlier DirectPath fixture failure, and the
bridge method-set test now passes without weakening its assertion.

One authorized baseline comparison built that exact master in an independent
worktree and DerivedData, regenerated its own mobile resources, and ran only the
same status-bar test on the same iOS 26.4 simulator with the same test options.
It executed one test, failed once with the identical idle-to-failed(deinit)
error, and exited 65. The generated bundle inventories matched
(`9e3f368460eb1438088bbadb39094716faae43f30bfa3181c2e8775c94341883`).
This establishes a reproducible baseline failure; it does not make the complete
candidate run green. No product code, assertion or timeout was changed for this
comparison, and neither run was retried.

## Cold restoration follow-up (2026-11-18)

Review found two gaps after the final integration: a background upload completion
delivered after iOS relaunched the app, before the account or Keychain identity was
restored, was dropped; and an authenticated event could leave a connection with no
operational identity paused. This replaces the captured connection epoch described
above. No persistent field, store, account anchor, device protocol or polling loop
is added.

Upload completion is local accounting. The delegate's serial processing chain waits
only for synchronous checks and one state write: the protected state file must exist
and be readable, contain a committed device ID and no cleanup marker, and have the
completed task's upload UUID at the queue head. A 2xx response must carry a positive
integer `accepted_seq` (not a boolean) equal to that payload's `device_seq`. This
needs no operational identity, Keychain, account or connection epoch. `enqueue` mints
a fresh UUID for every immutable payload, `taskDescription` carries it unchanged,
pairing replaces the queue and cleanup removes it, so an old task cannot consume a
new connection's head even when the sequences match. Any non-2xx response, including
409 `stale_seq` with a matching sequence in its body, is a failure; no ACK or
successful result is synthesized from it.

The accepted upload is removed from the queue, its dependent result is released and
the backfill flag updated in one state write; the payload file is deleted afterwards.
This publishes no identity and grants no request, query or observer authority. Result
submission and the next upload run in a separate task gated by the current epoch,
account, binding and cleanup state, so `urlSessionDidFinishEvents` completes without
waiting for login, manifest, result HTTP or another transfer. Irrelevant or retired
completions return immediately. Failures, including unreadable or unwritable protected
storage, keep the outbox and report no success; only an authorized continuation applies
the existing terminal/retry policy.

Finite storage failure domains remain explicit. Termination before the state write
loses that ACK; the retransmission then receives `stale_seq` and the request is
reported failed although the instance accepted the payload. A state write that
commits before payload deletion can leave an orphan file. No orphan scan or
power-loss guarantee is added.

An authenticated event queues one restoration only when there is no operational
identity or authorization is paused; a normal identity triggers no extra sync. The
pending pass runs after the account publishes its fresh snapshot, waits behind an
active connect, restore or cleanup, and is consumed only when an account is
available. Duplicate events coalesce into one pass. Logout, pause or a new epoch
retires it; a committed or restored identity clears it.

`MobileSessionCoordinator.refresh` now publishes Health authorization and the
authenticated event after push cleanup and runtime invalidation, immediately before
returning the signed-in state. The existing generation check prevents a refresh
superseded by logout or an account change during that cleanup from authenticating
Health. Paths without an await keep their meaning; no account transaction or rotation
changes.

## Recovery follow-up (2026-11-18)

Review of the integrated head found three recovery gaps. All three are closed by
the existing owners; no persistent field, protocol, account transaction or new
background wake is added.

**Network continuation.** After local upload accounting, one coordinator-owned
continuation sends pending results and then drains the outbox. The two steps fail
independently, so a transient result failure no longer skips the next payload.
Authority is checked again after every await and after error handling: a 401,
revoked or ineligible device stops locally; 422/426 suspends the connection, and
a suspended connection sends neither results nor uploads; a paused, replaced or
retired epoch takes no further action. Transient failures (network errors and
HTTP 403, 408, 429 and 5xx, the existing non-terminal set) schedule one coalesced
foreground retry of the same continuation. That retry also resends results when
the outbox is empty. Its delay is at least five seconds, waits for Retry-After
when one is set, and defaults to 30 seconds otherwise; device requests are still
refused before Retry-After. An upload error retires a terminal payload, then runs
the same continuation; a transient upload error schedules the same retry. 409
`stale_seq` is still never an ACK or success. In the background no timer is
added: recovery waits for the next HealthKit delivery, background-session event,
foreground activation, claim or authentication.

**Cleanup of undecodable storage.** An explicit cleanup (logout, account change,
rollback, confirmed corruption or revoked device) removes the Health sync
directory without decoding its state, after credentials are cleared and
background delivery is disabled. Cleanup completes only when the directory is
removed or the file system reports it missing; any other failure keeps cleanup
pending with the existing notice. Only a `DecodingError` makes resume start a
cleanup; protected or otherwise unreadable storage without cleanup intent is left
in place. Across restarts the existing finite failure domain applies: a writable
store retries its cleanup marker, a corrupt store is detected again, and a store
whose marker could not be written relies on the existing credential and binding
gates. This is not a new durable deletion guarantee.

**Foreground state.** The launch and background URLSession resumes read the
application state when they run, as bootstrap does, instead of passing `false`.
A late launch resume therefore cannot stop foreground polling that bootstrap
started. Foreground entry still passes `true` explicitly, because the
application state can still read as background during that notification. The
coordinator behavior is covered by tests; actual system event ordering is not
reproduced in XCTest and remains unverified.

## Foreground liveness follow-up (2026-11-18)

Review of the recovery head found that foreground polling only started when a
one-shot sequence (restore, connect or authenticated restoration) finished, so a
single transient failure could leave a foreground session without polling, and
that launch navigation waited for Health restoration. This replaces the network
continuation described above with one in-memory convergence owner. No protocol,
persistent field, account transaction or background wake is added.

One convergence pass runs at a time per epoch; requests made during a pass
coalesce into at most one more pass, and a pass from a retired epoch is cancelled
and never reused. The pass checks authority, starts foreground polling, sends the
manifest when it is due, completes unready reservations, sends results and drains
the outbox. Lost authority (epoch change, pause, account, binding, cleanup or
suspension) stops the pass. A transient failure is recorded, later steps still
run, and one retry is scheduled at the end through the single foreground retry
slot (at least five seconds, Retry-After when set, otherwise 30 seconds). The
manifest is due again for every epoch, every resume entry and every change in
whether background delivery is enabled. A successful send clears it only in the
same epoch, when nothing made it due again meanwhile and the background delivery
it reported is still current; the pairing setup manifest does not clear it.

Entries: resume restores the identity, marks the manifest due, joins the pass and
then performs one activation refresh only when foreground; a resume that meets a
restoration already in progress marks the manifest due and requests a pass; a pass never queries
for freshness, so an upload ACK cannot trigger another query. ACK success, a
retired terminal payload, a successful HealthKit delivery and a committed
connection (whatever its first sync did) request a pass. Transient upload,
delivery, polling and pass failures only schedule the retry. An authenticated
event for a present identity requests a pass only while the manifest is due or
foreground polling is missing, so a converged identity makes no request.

`foreground` is written only by the lifecycle: resume entries and entering the
background. Cleanup, revocation and suspension cancel polling and the retry
without rewriting it, so reconnecting in the same foreground session polls again.
In the background there is no polling, retry timer, refresh or new wake; a pass
started by a system event or already running when the app leaves the foreground
may finish its idempotent steps. HealthKit and URLSession completion timing is
unchanged, and upload completion still waits only for local accounting. Results
may wait behind a held manifest within the same pass.

Bootstrap now delivers navigation requested before it completed (notification
taps and selected-instance links) before awaiting Health restoration.

Task handles are generation-matched. The poll loop records its own identity: a
loop retired by entering the background, suspension, cleanup or a pause can no
longer clear the handle of a newer loop, and it handles no error, schedules no
retry and sends no further request; checks after every await in claim
processing enforce this. Synchronization and convergence tasks already match
their own identity or epoch. An upload wait stays bounded at 40 seconds rather
than cancellable, so durable enqueue work is never abandoned. Each upload ID has
at most one waiter, because the only wait is for the payload its own
synchronization has just enqueued.

Existing limits remain: `sendResults` stops at its first failing result, and a
request the instance purged (404 `request_not_found`) keeps failing on later
passes. Real system event ordering, background session redelivery timing and
HealthKit observer timing are reviewed in code only. After a two-factor pause,
`cancelAll` does not wait for cancelled transfers; if the user reauthenticates
quickly, a cancelled transfer's late completion can schedule one spurious retry
and delay that payload by up to about 30 seconds, without a loop or data loss.
