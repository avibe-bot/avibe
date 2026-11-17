# I3 Native persistence boundary

Baseline: `fdd4d315ff6a044e82322bf279d0cfa378bb2cea`. This isolated change
implements the G1/G2 persistence handoff accepted by main on 2026-11-17.
The initial store change targets `SystemSchedule.swift` and `AppModel.swift`.
Main's 2026-11-17 route B decision additionally permits the existing
`MobileAccount.swift` logout notification and the bundled mobile error mapping
and en/zh catalogs. The H3 repair also changes the model and the unpublished
0090 migration's ordinary sticky composite index, including terminal_state in its equality prefix without changing the migration prover, revision, epoch or index name.

## Contract

- A fixed Application Support envelope contains installation identity, owner,
  records, and immutable v1 migration provenance. Identity becomes public only
  after bootstrap commits. Preferences are legacy evidence, never authority.
- The existing file store owns the committed snapshot, pending candidate, and
  typed commit error. All owner, detach, record, terminal and replay paths use
  this boundary. Execution slots retain their existing effect/waiter ownership.
- Forwarder holds both `local_state_write_failed` and `local_state_unavailable` out of terminal receipt delivery for create/cancel, preserving the device-bound sticky lease until Native can replay its authoritative result.
- Logout revokes immediately and decides retention from the last committed
  lifecycle. An uncommitted promotion cannot turn an in-flight item into a
  retained item.
- Account logout completes independently of schedule detach IO. `beginReset`
  still revokes Runtime/confirmation authority and notifies the schedule store
  before the first await. The existing account invalidation, transport logout
  and account-store replacement sequence remains in charge of the account.
  A failed schedule commit cannot leave a selectable cached account, including
  the existing completed-deletion fallback.
- The store blocks every owner while a process-local detach intent is pending.
  With no readable image, it remembers the unresolved logout, binds it to the
  first recovered and verified active owner, and durably detaches before
  granting either the same account or another account an epoch. A consumed
  intent never detaches a later account.
- Cold open validates provenance and re-establishes file/directory durability
  before adopting visible bytes. Readability alone does not acknowledge a
  failed rename/directory-sync transaction.
- A cold image is classified only after that barrier succeeds. Completed OS
  items become permanently detached and retained; known same-process failed
  promotions and in-flight effects keep the existing slot cleanup path.
  Across processes, the barrier-confirmed canonical is authoritative even if
  the preceding process reported a failed promotion. This does not retroactively
  acknowledge its failed receipt.
- Process-local logout intent cannot survive a process ending before detach
  commits. After such an interruption, a new process may restore the old epoch
  only after storage recovery and actual same-account authentication. An
  unauthenticated, second-factor or failed discovery grants no authority;
  a different authenticated account must durably detach the departing owner.
  Once detach commits, later errors, lost receipts or process restarts cannot
  restore that epoch. Account logout success alone is not proof that schedule
  detach committed during this combined storage-fault/process-loss boundary.
- Exactly one UUID-named v1 source can migrate, preserving its bytes, UUID,
  records, owners and OS identifiers. The source remains untouched; canonical
  provenance fingerprints it. Ambiguous, changed, corrupt, future or unreadable
  evidence fails closed.
- `initialize_without_local_history` is allowed only after successful checks
  find no canonical, no supported legacy namespace evidence, and no old identity
  preference. This policy cannot distinguish first use from erasure of every
  local clue. It neither discovers nor adopts unknown OS items, and does not
  change Runtime device bindings. That all-clues-lost case is outside recovery
  guarantees, as explicitly decided by main on 2026-11-17 at 15:12 +08:00.

## Validation and delivery

The preserved frozen-source G1/G2 probes and four no-effect counterexamples
provide deterministic red evidence. Add focused store/bootstrap and consumer
regressions, isolate every AppModel test store, then run the complete relevant
Native suites, Python cross-layer/migration tests, Forwarder/UI checks, lint,
and unsigned Simulator/device/fixture builds. Keep real file IO, injected
failures, process reopen and physical power-loss claims distinct.

The existing recovery surface maps local-record errors to the approved generic
alarm/reminder-record message, with its existing recovery Retry. It adds no
logout-failure page, sheet state transfer or logout Retry. Deletion completion
source/lease issues remain a separate matter; this change does not alter
AvibePage, the bridge, RootView or MobileOnboardingContainer.

No bridge authorization, effect-slot algorithm, Runtime/Web protocol, OS
adapter, signing or permission changes. No real-user migration or OS
operations. Stop at one clean local candidate and report exact-head evidence
to main for independent review; do not push, open a PR or dispatch review.

## Work

- [x] Verify isolated branch, frozen source and independent review hashes.
- [x] Resolve bootstrap indistinguishability with main's finite-fault policy.
- [x] Implement canonical bootstrap/migration and common transaction owner.
- [x] Cover every synchronous terminal and replay/error exit.
- [x] Exercise focused coverage for all eight handoff rows and separate-process reopen.
- [x] Prepare a clean candidate for full relevant gates and main-owned review.
- [x] Close H1/H2/H3 and route B against focused consumer regressions.
- [x] Prepare the replacement candidate for complete gates and main-owned review.

Full gate execution and the delivery decision are recorded against the fixed
candidate SHA in the matter handoff, outside the frozen source tree. Focused
coverage includes every AppModel no-effect entry, bootstrap writer stages,
actual file/directory permission denial, strict migration and changed-source
retry, owner/detach ordering, and existing effect-slot regression cases.
The earlier interrupted-logout red assertions and completed-deletion
counterexample remain preserved in the matter. Route B supersedes the prior
requirement that schedule IO failure aborts the account transaction; the
restart test now explicitly checks the finite durable/undurable boundary
through real AppModel authentication, rather than claiming a memory intent
survives process loss.
