# Protected Bitcoin ECDSA address types

## Change contract

Deliver protected **ordinary-key** P2PKH and native P2WPKH signing with the existing
browser custody boundary, one-input/version-2/locktime-0 spending grammar,
SIGHASH_ALL, one or two supported outputs and bounded explicit review. The HD
extension is a separate follow-on, not a prerequisite for this ordinary backend.
Standard signing and the shipped protected Taproot input path retain their existing
bytes, defaults and refusals. There is no fallback to raw ECDSA or standard
custody, migration, reseal, key backfill, automatic fund movement or broadcast.

The new `bitcoin-p2pkh-v1` / `bitcoin-p2wpkh-v1` typed contexts select
`ecdsa-secp256k1-der`. The former includes a complete stripped parent transaction;
the latter forbids parent proof fields. Both bind the selected compressed point,
exact script and digest. Legacy independently validates the parent txid/outpoint,
selected output and amount before signing: its sighash alone does not directly
commit to previous-output amount. Native BIP143 binds the amount and outputs for
a valid spend, not independent chain/locking-program provenance.

### Independent ordinary delivery and HD follow-on

The funding parser, both sighashes, early bounds, CLI file transport, ordinary
request identity/atomic completion, source-owned support projection and list
summaries form the independently deliverable ordinary backend. They do not import
or require the protected-HD implementation. New typed contexts for a protected HD
row are explicitly refused in this build; an ordinary success never grants HD
support.

The packaged pin remains the existing Taproot-only cohort. Enabling Legacy/Native
requires a separately reviewed exact retained sandbox pin and its complete-pin
capability entry, plus the matching Web selected-detail consumer. Source tests
use an explicit ordinary-capable, HD-incapable registry fixture; they do not enable
a released cohort, prove browser custody, or authorize promotion.

Protected HD integration belongs to a separate branch/worktree and consumer PR.
It may be developed against the frozen owning interface, but its merge requires
that owning implementation on the default branch; do not stack or copy its
unmerged source into the ordinary PR. Reuse that owner's typed seed/envelope and
authenticated 20-row validation, raw-envelope fingerprint, selected-account
material hydration, signed release shape, protected HD dispatch and completion/CAS
ownership. Extend those seams for purpose44/84/86 at the same receiving index;
do not introduce a parallel protected-HD owner. Creation, response-loss
reconciliation and its canonical contract are unchanged.

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

Only the two new kinds emit a proof-free list summary. It carries the generated ID,
sign type, status/timestamps, the saved `secret_name`, a top-level
`protection: "protected" | null`, and null requester/delivery/message/card/session.
`secret_name` is the exact string persisted on the request row when it was created.
`protection` is `"protected"` only when the persisted
`delivery.bitcoin_identity.protection` scalar is exactly `"protected"`, and `null`
otherwise. It comes from a fixed SQL `CASE` over that trusted persisted capture.
It never comes from live secret metadata, the current name, the caller, a card or
the proof. Null in either field means only that the summary does not supply it. It
does not mean standard, unprotected, missing or renamed. No other field, key/type
badge, signature or proof material is added. Old-kind items and full
POST/GET/await requests are unchanged.

Each emitted summary is<=1,024 UTF-8 bytes in the actual endpoint's compact
serialization, with escaping counted, and in the shared comparison serialization.
Depth is<=2 and nodes are<=32.

Secret names currently have no length limit. The full stored name is emitted only
when the whole compact UTF-8 summary carrying it is<=1,024 bytes. Otherwise the
summary emits `secret_name:null` and keeps every other field. The name is never
truncated or elided, the row is never dropped, and no count is clamped. No global
name schema or maximum is introduced.

Metadata-first storage includes session precedence, resource ACL and expiry; do
not hydrate proof blobs to then throw them away. Old-kind contribution and all
existing limit/order/filter/count semantics stay, including0/negative/>100 and
the distinction between SQL LIMIT and post-filter Python slicing. No new count
rule or pagination mechanism.

Aggregate new contribution is the sum of emitted summary bytes plus separately
measured wrapper syntax. A compact all-new response with<=100 rows fits128KiB;
custom counts and mixed old rows do not acquire a universal128KiB promise.
Canonical accounting is not a claim about arbitrary wire serialization.

Before the full GET, only the summary name and a Protected chip may be shown, and
both are display-only. A null `protection` shows no chip and no standard label.
Approval, every protection decision and any action still require the full same-ID
GET. The existing UI-audience per-ID GET hydrates one
selected full same-ID request before protection/passkey/browser-handoff
classification or approval controls. Those decisions are recomputed from that
full request, never from the summary's name or protection, a cached row or a
current secret lookup. A known focused ID goes directly to GET, not through
first-page membership. Shared global/chat/floating/focus/pet consumers discard
stale loads on close or auth/session changes and never prefetch the proof corpus.
Old null-card clients still refuse approval and may show the name only. Browsing
an unknown row beyond the existing first-page behavior is not added by this change.

**Amendment record (2026-11-16).** This section amends §4.2 and §4.3 of frozen
contract rev0.4. The frozen record (sha256
`62c96696f928757ba42f8c9894d41aec6e70f268058214c2e402054543f94af7`) stays
untouched, and no new revision artifact is created.
- Reason: the owner could not recognise the new-kind inbox rows, because they
  showed only a generic signing request and an ID.
- §4.2: the exact all-null shape and `secret_name:null` give way to the saved name
  under the size rule above and to the captured `protection` scalar. The
  no-protected-badge clause gives way to the display-only chip.
- §4.3: before the full GET, only the display name and Protected chip are allowed.
  Approval, protection/passkey/handoff decisions and any action still require the
  full same-ID GET, and the old fallback is preserved.
- Re-ACK: Product `eae7c82940cd`.

The four-field RPC, typed contexts, signing, release/grant/policy, pin cohort and
full-request formats do not change. This amendment claims no Web implementation
and no runtime acceptance.

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
- [ ] Independent source review and exact-head CI for the current ordinary head.
- [ ] Separate reviewed capable pin/registry and matching Web selected-detail
  integration, followed by authorized composed/runtime acceptance.

The HD item above is follow-on scope, not an ordinary PR merge prerequisite.
Review/CI, capability enablement, runtime acceptance and funded exercises remain
separate judgments.
