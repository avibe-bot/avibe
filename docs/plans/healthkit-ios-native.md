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
