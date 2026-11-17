# I3 Native persistence boundary

Baseline: `fdd4d315ff6a044e82322bf279d0cfa378bb2cea`. This isolated change
implements the G1/G2 persistence handoff accepted by main on 2026-11-17.
Only `SystemSchedule.swift` and `AppModel.swift` are product write targets.

## Contract

- A fixed Application Support envelope contains installation identity, owner,
  records, and immutable v1 migration provenance. Identity becomes public only
  after bootstrap commits. Preferences are legacy evidence, never authority.
- The existing file store owns the committed snapshot, pending candidate, and
  typed commit error. All owner, detach, record, terminal and replay paths use
  this boundary. Execution slots retain their existing effect/waiter ownership.
- Logout revokes immediately and decides retention from the last committed
  lifecycle. An uncommitted promotion cannot turn an in-flight item into a
  retained item.
- Cold open validates provenance and re-establishes file/directory durability
  before adopting visible bytes. Readability alone does not acknowledge a
  failed rename/directory-sync transaction.
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

No bridge authorization, effect-slot algorithm, Runtime/Web protocol, OS
adapter, copy, signing or permission changes. No real-user migration or OS
operations. Stop at one clean local candidate and report exact-head evidence
to main for independent review; do not push, open a PR or dispatch review.

## Work

- [x] Verify isolated branch, frozen source and independent review hashes.
- [x] Resolve bootstrap indistinguishability with main's finite-fault policy.
- [x] Implement canonical bootstrap/migration and common transaction owner.
- [x] Cover every synchronous terminal and replay/error exit.
- [x] Exercise focused coverage for all eight handoff rows and separate-process reopen.
- [x] Prepare a clean candidate for full relevant gates and main-owned review.

Full gate execution and the delivery decision are recorded against the fixed
candidate SHA in the matter handoff, outside the frozen source tree. Focused
coverage includes every AppModel no-effect entry, bootstrap writer stages,
actual file/directory permission denial, strict migration and changed-source
retry, owner/detach ordering, and existing effect-slot regression cases.
