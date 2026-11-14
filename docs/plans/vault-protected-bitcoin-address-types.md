# Protected Bitcoin ECDSA address types

## Change contract

Add protected ordinary and HD P2PKH and native P2WPKH signing with the existing
browser custody boundary, one-input/version-2/locktime-0 spending grammar,
SIGHASH_ALL, one or two supported outputs and bounded explicit review. Standard
signing and the shipped protected Taproot input path retain their existing
bytes, defaults and refusals. There is no fallback to raw ECDSA or standard
custody, migration, reseal, key backfill, automatic fund movement or broadcast.

The new `bitcoin-p2pkh-v1` / `bitcoin-p2wpkh-v1` typed contexts select
`ecdsa-secp256k1-der`. The former includes a complete stripped parent transaction;
the latter forbids parent proof fields. Both bind the selected compressed point,
exact script and digest. Legacy independently validates the parent txid/outpoint,
selected output and amount before signing: its sighash alone does not directly
commit to previous-output amount. Native BIP143 binds the amount and outputs for
a valid spend, not independent chain/locking-program provenance.

### Independent work and dependency boundary

The funding parser, both sighashes, early bounds, CLI file transport, ordinary
request identity/atomic completion, source-owned support projection and list
summary work can be implemented against the default branch independently.

Protected HD integration depends on the protected-HD implementation reaching
the default branch. Reuse that owner's typed seed/envelope and authenticated
20-row validation, raw-envelope fingerprint, selected-account material hydration,
signed release shape, protected HD dispatch and completion/CAS ownership.
Extend those seams for purpose44/84/86 at the same receiving index; do not copy
an unmerged implementation or introduce a parallel protected-HD owner. Creation,
response-loss reconciliation and its canonical contract are unchanged.

## Required boundaries

- A full Legacy parent is lowercase non-witness hex, at most1,048,576 decoded
  bytes,1..128 inputs/outputs and scripts<=10,000 bytes. CompactSize is minimal,
  integers are exact/safe, all amounts and their sum are money-range checked,
  and parsing consumes all bytes. Funding version/sequence/locktime/scripts are
  opaque; zero/nonstandard nonselected outputs remain valid serialized data.
- The two new typed contexts preserve the one-input narrow spend grammar and
  existing dust floors. Native uses the length-prefixed25-byte P2PKH scriptCode,
  not the witness program. Results contain low-S DER only, not its sighash byte.
- Before generic traversal, all effective proof copies are bounded. One context
  is<=2,162,688 canonical UTF-8 bytes; a full operation has at most two equal
  Legacy proofs and<=4,718,592 canonical bytes, with non-proof contribution
  <=524,288. A list collection is not one full operation's tree/byte budget.
- Add exclusive `--signing-context-file PATH` / `--signing-context-json JSON`.
  File raw bytes<=2,162,688 inclusive, limit+1 bounded read and fstat on the same
  regular-file descriptor, strict UTF-8 and bounded preparse. No stdin or
  environment-body transport, path evaluation or lower parent cap.
- Initial storage captures saved ID/classification/P/raw envelope/cohort before
  release attachment. Completion rechecks that capture and verifies DER before
  pending CAS, persisted signature, original requester use and success audit
  settle atomically, including caught exceptions and outer rollback.
- The complete trusted packaged pin selects support from a reviewed source map;
  unknown pins do not gain types from stored points. Pin/release promotion is
  separate from this source implementation. Receiving addresses are filtered
  by honest supported lists. New ordinary signing always needs fresh
  content-bound UV; HD verifies actual selected BTC sibling points in custody.
- Return classification for the new inputs is same scalar (ordinary) or the
  same shared BTC receiving index (HD), across P2PKH/P2WPKH/BIP86. More than one
  return output refuses. Existing strict fee thresholds remain unchanged.
- Preserve the four-field sandbox RPC and existing result envelopes. Persisted
  release/key/type/signature are authoritative after response loss or later
  secret changes. The ordinary policy v1 shape stays, with a protected-only
  internal cohort binding and existing stale-state refusal.

## Request collection and selected detail

Only the two new kinds emit a proof-free list summary: generated ID, sign type,
status/timestamps, and null name/requester/delivery/message/card/session. Each
summary is<=1,024 serialized UTF-8 bytes. Metadata-first storage includes
session precedence, resource ACL and expiry; do not hydrate proof blobs to then
throw them away. Old-kind contribution and all existing limit/order/filter/count
semantics stay, including0/negative/>100 and the distinction between SQL LIMIT
and post-filter Python slicing. No new count rule or pagination mechanism.

Aggregate new contribution is the sum of emitted summary bytes plus separately
measured wrapper syntax. A compact all-new response with<=100 rows fits128KiB;
custom counts and mixed old rows do not acquire a universal128KiB promise.
Canonical accounting is not a claim about arbitrary wire serialization.

The existing UI-audience per-ID GET hydrates one selected full same-ID request
before protection/passkey/browser-handoff classification or approval controls.
A known focused ID goes directly to GET, not through first-page membership.
Shared global/chat/floating/focus/pet consumers discard stale loads on close or
auth/session changes and never prefetch the proof corpus. Old null-card clients
refuse approval. Browsing an unknown row beyond the existing first-page behavior
is not added by this change.

## Validation and delivery

Each primitive has focused public-vector/mutation coverage at its owner. The
external CLI max-proof case must run with all home/config/state/socket paths
redirected and bounded output capture, and reach normal persisted readback.
Collections cover multiple large proofs, old limit branches, scoped visibility,
selected GET failure/staleness and known focus outside the first page. No mock
or primitive test is relabeled browser custody, physical UV, chain, funds or
runtime acceptance. Build/review/CI, composed runtime, release/pin promotion and
funded exercises remain separately identifiable gates.

## Implementation status

- [x] Independent Legacy parent parser and both sighash owners.
- [x] Shared early context/copy budgets and bounded CLI file input, including a
  real isolated short-argv CLI process with a legal maximum parent and persisted
  pending readback (test-only capability fixture, no service or custody).
- [x] Ordinary saved identity/cohort, atomic verification/completion and frozen
  HTTP POST/GET, CLI await and callback-read results.
- [x] Truthful source-owned ordinary receiving projection and guarded policy
  identity; the packaged legacy pin remains unchanged and cannot admit new kinds.
- [x] Metadata-first new-kind list summaries with original limits, per-row
  expiry, session/ACL filtering and measured 100-row HTTP responses over pending
  requests that include two legal maximum-parent requests. The summaries omit
  their proof bodies.
- [ ] Protected-HD extensions after the owning implementation is on default.
- [x] Focused hermetic ordinary/shared/parser/transport conformance (source-only).
- [ ] Independent source review and exact-head CI for the complete change.
