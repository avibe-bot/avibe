# Coherent Vault delivery capture (3.2.7)

## Change contract

An opaque delivered envelope, immutable secret identity, field, historical ACL
and fetch host/auth policy must come from one real SQLite snapshot. No delivery
may pair an old envelope with a replacement secret's audit identity or policy.
Fetch, run and inject all carry captured evidence through handoff; bookkeeping
never resolves the name again. A change before grant resolution fails closed;
a change after capture cannot relabel the actual delivered envelope.

This is a producer fix, independent of epoch-3 storage activation. It changes no
persisted schema, custody wire format, historical reader/tombstone semantics,
grant selection policy, one-shot consumption policy or external replay behavior.
It must merge only after 3.2.6 stable, by the lead, not this lane.

## Implementation

- Capture requested record envelopes and immutable identities under a SQLite
  savepoint, including when sqlite3's legacy mode would not BEGIN for SELECT.
  Construct each ACL snapshot from the resolved row's ID, not a second name lookup.
- Batch run/inject preflight carries those captures. Each existing grant-writing
  transaction reserves the writer before checking the captured version/ACL and
  selecting or reserving grants. Changed records fail closed. This keeps existing
  transaction and approval/error cleanup boundaries without holding a lock over
  publication, approval waits or external delivery.
- Single-record fetch resolves access and captures its result in the same writer
  transaction. Its final request policy uses that paired capture, not a fresh
  name lookup. The pre-approval host check remains effect-free.
- Only the existing name/envelope/target fields cross the avault boundary. Audit
  identities remain in Python as non-secret bookkeeping evidence. A bookkeeping
  failure after handoff never retries the external effect.

## Validation contract

Existing tests do not interleave secret replacement with capture or handoff.
Add deterministic synthetic two-connection cases for replacement during capture
(A) and after resolution before handoff (B). Assert the actually passed envelope,
audit ACL/resource identity and usage bookkeeping agree. Cover fetch/run/inject,
standard/protected, common/one-shot grants, multiple fields and pre-handoff
failure cleanup. Existing Vault tests retain approval-wait and no-replay coverage.

## Evidence

- 448 targeted tests passed across capture, service, fetch, CLI, inject, audit
  snapshots and resource ACLs; 27 control-request/query-denylist tests passed.
- All twelve mode/tier/grant replacement cases fail against the untouched base
  production modules at the audit resource-ID assertion, while the actual
  envelope handoff succeeds. The paired final fetch-policy case also fails on
  the base. The fault probes run in-process with the repository's isolated test
  state; no worktree file is overwritten to perform the negative check.
- Changed-file Ruff and `git diff --check` pass.

## Known by design

Capture is not a revocation lease across external handoff. A subsequent deletion
may make protected custody refuse its old grant; if custody accepts an already
prepared envelope, the audit still describes that original identity and policy.
No DB lock spans custody. Approval publication and one-shot cleanup remain at
existing transaction boundaries; batch preflight compares actual identity,
envelope, kind, protection, fetch policy and historical ACL under the writer
reservation, rather than trusting timestamps or reusing the name alone.

All validation uses test-owned state and mocked custody. No live DB, credentials,
real avault, egress, service, Incus or load operation is authorized. Exact-head
code and Codex security review plus CI remain required before lead merge.
