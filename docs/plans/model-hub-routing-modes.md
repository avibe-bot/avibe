# Model Hub Routing Modes: Implementation Contract

Status: owner-approved design, orchestrator-frozen implementation contract, 2026-09-06.
Implementation base: `1e21561fa374a95005aa9d2776719bb9fc5ac8a0`.

This contract replaces the former one-time matching and stored-route-only policy.
The feature PR must update the existing behavior specification, schemas, mirror
registry, API, UI, and consuming tests together. Those existing documents must not
retain contradictory current-policy claims when this implementation ships.

## Example and Intent

For backend defaults `[A, B]`, model M automatically uses B if B lists M and A does
not. A is not prepended as a speculative candidate. If neither lists M, the chain
is `[A/M, B/M]`, with M forwarded unchanged. A saved manual chain replaces this
generated chain completely, including an explicitly empty manual chain.

The upstream inventory is evidence for matching, not an invocation whitelist.
Credentials, protocol and channel compatibility, backend model admission, source
membership, explicit retirement, and live health remain separate boundaries.

### Owner Scope Decision

The owner selected API-key passthrough for this delivery on 2026-09-06.
Unknown-model passthrough is available only to eligible Hub sources whose
`kind` is `api_key`. Subscription sources, including Hub-held OAuth and native
CLI subscriptions, retain their existing model admission and exact invocation
capabilities. They still participate in automatic matching and manual routes;
they are not speculative unknown-model candidates. An empty subscription-only
default set for an unmatched model is Unconfigured, never falsely Passthrough.

For manual writes, API-key targets may be absent from inventory; subscription
targets keep the existing membership/retirement admission and stale-hop retention
rules. Preserve subscription compatibility and normal errors, and do not imply
that changing route labels upgrades the underlying engine's model support.
Source inventory and reasoning metadata stay truthful for both kinds. All broad
inventory-independence and unknown-target statements below apply to API-key
sources under this explicit scope; the same shared route owner handles both.
No underlying CPA extension, engine upgrade, OAuth alias substitution, or
alternate subscription transport is part of this delivery.

## Persistent Intent and Compatibility

Existing `model_hub.agents[backend].sources.order` is the sole backend-default
membership and priority owner. Existing `routes` becomes a sparse map of manual
overrides, with unchanged value shape:

```ts
type RouteHop = { source_id: string; model_id: string };
type ManualRouteOverride = { hops: RouteHop[] };
type RouteOrigin = "automatic" | "manual" | "passthrough" | null;
type ManualRouteOverrides = Record<string, ManualRouteOverride>;
```

Key absence means automatic. Key presence means explicit override, including an
empty array. Saving a model route always creates an override even when identical
to the generated result. Restoring automatic deletes the key. There is no second
persisted policy flag and no inference from array equality.

Preserve existing override entries and exact arrays losslessly, including empty,
stale, and dormant OpenCode entries. Historical discriminator-free records cannot
reveal whether an old empty array was generated or deliberately cleared. Retained
entries are frozen routes, not a claim of known historical human authorship. The
UI can restore them explicitly. Do not globally reinterpret old empty arrays as
automatic or clear existing config during regression deployment.

New catalog rows and missing fixed-menu rows receive no route key. Stop default
seeding and source-add appends to persisted routes. Older supported config shapes
continue through the existing safe loader; use explicit historical intent when
available, never infer it from a backend source order. The blanket pre-release
no-migration prose must not override the persisted-shape compatibility rule.

## One Route Owner

Extend `core/handlers/model_hub/resolver.py` with one pure effective-route owner.
It produces `{manual_override, route_origin, hops}` from current configuration,
backend, and canonical requested model. Preview may call it with an isolated
draft configuration; no public sentinel or duplicate matching algorithm is needed.

1. A manual key returns the exact saved hops and order.
2. Otherwise scan configuration-eligible sources in backend default order for
   matching evidence. Include all accepted matching pairs in that order.
3. If there are no matching pairs, use each eligible API-key default source with
   the exact requested id. Do not append speculative candidates to a matched tier
   or include subscription sources as unknown-model passthrough candidates.
4. An empty resulting chain has null origin. A nonempty chain has its chosen
   origin regardless of current health or native process readiness.

Reuse native Claude alias/version/date matching, including its existing parser.
Other backends and API sources match literal canonical ids; do not add vendor or
general fuzzy matching. Use complete non-retired inventory consistently. A
`retired:true` source/model tombstone excludes that exact pair from matching,
passthrough, and invocation. Missing inventory alone does not. Removing a manual
inventory row removes matching evidence, not an upstream capability; it must not
silently delete an explicit route. Source deletion still removes references
atomically with existing guarded integrity rules.

Choose the matching tier before applying an invocation transport restriction.
A Hub-only HTTP request must not invent a different passthrough tier because the
selected matching tier contains a native hop. Native singleton/backend binding,
CLI readiness, direct-mode bypass, credential custody and origin restrictions
remain in force. Do not broaden backend catalog admission or OpenCode native
protocol declaration as a side effect of relaxing source inventory checks.

All reads, previews, summaries, probes, launches, request execution, adoption
reports, and mutation guards consume this same effective plan. Live inspection
annotates runnability/current/health; it never changes tier or membership. The
existing executor owns failover, credential recovery, and streaming cutoffs.
Unknown model errors remain request-nonfallback and do not change source health.
Exact reasoning effort is forwarded only if declared by the exact source model;
unknown passthrough models do not acquire invented capabilities or reasoning tiers.

## API Shapes and Responsibilities

Advance the terminal in-request contract to `contract_version` 9 on the complete
feature head. Retain readable historical TurnProvenance versions through 9.

The server adds the following required fields to `AgentChain`:

```ts
manual_override: ManualRouteOverride | null;
route_origin: RouteOrigin;
```

`manual_override` reports actual persisted key presence. `chain` is the effective
ordered plan with existing live annotations, not the persisted map. Existing
`current`, `supply_state`, health, reason, and retry fields retain their meaning.
For a saved empty override, `manual_override` is `{hops:[]}`, `chain` is empty and
`route_origin` is null. Preview reports the draft override rather than saved intent.

`AgentSupply.routes` contains only manual overrides. Add required `route_origin`
to each `model_supply` summary and derive `chain_length` from the effective plan.
Frontend labels consume these explicit fields. No UI inventory matching or
array-equality heuristic is permitted. `added_to`, `adopted_by`,
`in_current_model_chain`, and supply guards use effective routes. Inventory
candidate metadata remains inventory evidence and must not be fabricated for a
passthrough target.

| Endpoint | Exact request and responsibility |
| --- | --- |
| GET `.../agents/<backend>/sources` | Existing AgentSupply success envelope; server projects backend defaults. |
| PUT `.../agents/<backend>/sources` | `{order, force?, would_remove_hops?, would_interrupt?}`; atomically replace default membership/order and preserve all manual overrides. Guard effective supply/hop removal using the existing exact-plan confirmation protocol. Pure reordering without removal needs no guard. |
| GET `.../agents/<backend>/chain?model=<id>` | Existing chain success envelope, extended AgentChain. |
| GET `.../agents/<backend>/chains` | Existing chain-list success envelope, same owner and fields for every routeable model. |
| PUT `.../agents/<backend>/chain?model=<id>` | `{hops, force?, would_remove_hops?, would_interrupt?}`; persist explicit override, including equal and empty arrays. Return existing chain-mutation success envelope. |
| DELETE `.../agents/<backend>/chain?model=<id>` | Optional `{force?, would_remove_hops?, would_interrupt?}`; remove override, recompute defaults, return the same mutation envelope. Idempotent if absent. Guard actual effective removal/supply loss. |
| POST `.../agents/<backend>/chain/preview?model=<id>` | `{manual_override:null \| {hops:RouteHop[]}}`; return chain success envelope for draft replacement/removal. No persistence, events, engine startup/sync, credentials, or upstream egress. |
| POST `.../agents/<backend>/chains/reorder` | Existing optional order plus optional guard fields; default-order compatibility entry point with the same effective guards. Never reorder manual arrays. With no order, return the current projection. New UI uses Sources PUT. |
| PUT `.../agents/<backend>/models` | Preserve existing baseline/concurrency/expected-suppliers/guard contract. New rows automatic, retained rows preserve intent, removal cleans intent through existing guarded mutation. |

All paths above use `/api/models` as their prefix. Existing success/error envelope
field names and exact guard plan structures are reused, not renamed. Chain writes
validate canonical nonempty identifiers, source existence/eligibility, and explicit
retirement, but do not require source inventory membership. Manual override hops
may use an eligible source outside the backend default subset.

Identity normalization and new-identifier admission are distinct. An existing
catalog identifier or retained exact target may exceed a later admission length
bound without losing edit, preview, restore, or provenance-read capability. Use
the persisted normalized identity and existing catalog/target evidence for those
operations; apply admission bounds to newly introduced identifiers. This does not
admit new padded aliases or bypass source, channel, or retirement validation.

Server handlers, RPC, UI server, CLI/client wrappers, endpoint tables, response
schemas and UI client must expose the same operations. Preview is read-only even
when runtime is stopped. Mutation responses are authoritative after Save; failed
or ambiguous writes follow the existing reconciliation and guard mechanisms.

## Interface and Interaction

Approved design: `avibe-docs/design.pen`, frames `bmi25` (dark), `ziils` (light),
`NuxyR` (help/touch), `jCs2A` (manual/restore), `ztAos` (defaults), `P6Zi8k`
(automatic/error/empty), reusable row `tFl3R`. Exact pixels remain owned there.

- Show Automatic, Manual, Passthrough with distinct existing mint, new manual blue,
  and amber tokens. Null origin shows Unconfigured, not a success label.
- Hover/focus explains each origin; touch tap opens the same help without opening
  the row dialog. Escape and outside interaction dismiss help. Avoid nested buttons.
- Rename Adjust priority to Default routing. It edits one backend's default
  membership/order; manual routes are independent. Show affected inherited/manual
  counts without implying historical authorship or runtime health.
- Route dialog opens inherited state with Edit route. Manual editing preserves
  add/edit/remove/reorder and allows exact model ids missing from the source list.
- Restore automatic lives in the dialog footer and changes draft only. Call preview
  with null override, show its actual target and origin, which may be Passthrough.
  Undo restore reinstates the prior manual draft. Cancel/close never writes.
  Save uses DELETE for restored automatic, PUT for manual draft, consumes the full
  canonical mutation result, and preserves existing guarded confirmation handling.
- Preserve the draft on failed Save, avoid stale preview overwriting newer edits,
  and disable duplicate submissions. Default configuration changes must not leak
  between backend groups.
- No eligible defaults show Unconfigured and a Configure default routing action.
  An explicit empty override stays empty and offers Restore automatic in the dialog.
- Request errors are separate from route origin. Model-not-found keeps Passthrough
  and shows the recorded-turn detail surface below; do not mark the whole source failed.
- Apply complete EN/ZH localization and keyboard/touch behavior. Documentation
  remains English; public user documentation has matching English/Chinese pages.

### Recorded Error Detail Closure

The approved error panel must use real history. The existing bounded
`BoundedProvenanceStore` is the sole retained outcome owner; its records describe
exactly attributed, settled turns, not every upstream request. Keep its existing
500-entry retention, atomic writes, and exclusion of ambiguous attribution. Do not
introduce a second history store or turn request failures into Source health events.

Add GET `/api/models/agents/<backend>/provenance?model=<id>` through the existing
service, RPC, client, and UI-server path. Validate the backend and its canonical
catalog model identifier. Reuse the existing envelope:
`{ok:true,contract_version:9,provenance:TurnProvenance|null}`
for the most recently persisted retained record matching both backend and requested
model, regardless of outcome; no record means null, not a fabricated success.
This is a read-only, on-demand dialog read and never starts or syncs the engine.
The existing GET `/api/models/turns/<turn_id>/provenance` remains unchanged.
Do not add history to the pure effective-route owner or every chain-list response.

For newly observed terminal errors, retain optional `http_status` (valid HTTP
status integer or null) and `upstream_error_code` (known machine code or null)
inside the existing `terminal_error` object. Derive the latter only from observed
codes recognized by the existing `UPSTREAM_MACHINE_ERROR_CODES` authority. For an
observed `model_not_found` whose classified result is `upstream_request_invalid`,
retain that specific code instead of a co-occurring generic `invalid_request_error`
type. Otherwise use the existing machine-code specificity order. This is diagnostic
selection only and must not rerank or change the classifier's decisions;
never persist arbitrary upstream strings, raw bodies, messages, headers, or
credentials for this panel. Preserve exact observed `model_not_found` when present,
without reconstructing it from the broader `invalid_parameter` classification.
Historical records may omit both fields and must remain readable unchanged.
These observations do not alter classification, fallback, or source health.

The route dialog loads this projection independently of its route draft. Show an
error panel only if the latest retained turn has a terminal error; a later served,
canceled, or other non-terminal-error record must not leave an older error looking
current. Label it "Latest recorded turn" / "最近已记录回合", with the recorded
timestamp and exact historical source/model. The details action opens that same
record's structured fields; do not infer a source from today's route. Missing
machine codes get generic reason-based copy, never invented model-not-found copy.
Keep route origin unchanged. Read failures retain an explicit retry state; changing
models or closing the dialog invalidates pending reads. Cancel/Restore/Save never
write or rewrite history. Deleted sources remain historical identifiers.

Contract and consuming tests must cover old records without optional metadata,
model/backend isolation, latest-success clearing, bounded/absent history, unsafe
upstream text exclusion, read-only retrieval, and real model-not-found display.

### Joined Turn Finalization

A backend may finish its native turn as soon as it reads a terminal protocol
frame, before the gateway has settled the corresponding transport outcome. FSM
completion alone must not destroy an exactly attributed trace with owned gateway
requests still settling. Correlation and the existing gateway terminalizer join
those two lifecycles; only observed service outcomes can establish Hub success.

At FSM completion, close new attribution admission for that turn while allowing
already-owned request identities to finish. Finalize the retained record once,
after both boundaries are terminal, preserving the existing stopped, backend
failure, ambiguity, and native-channel precedence. Prefer a bounded per-turn
drain integrated with the existing Session completion/admission owner and gateway
teardown deadline. Do not wait under a service mutation lock or a global lock,
and do not block unrelated Sessions. The next turn in the same Session must not
be attributed to the departing trace or have its newer record replaced by a
late finalization of that trace. Preserve route credential reuse and independent
request identities across retries and concurrent requests within one turn.

Cancellation, process-scope retirement, gateway close and teardown timeout must
release owned state without inventing success or silently dropping committed
terminal facts. A terminal frame is not permission to bypass settlement. No
second history store, delayed lookup retry, new wire/version field, arbitrary
sleep, or per-request engine registration is part of this correction. OpenCode's
existing shared-process untracked boundary remains unchanged.

Consuming regression must exercise real loopback HTTP completion before gateway
settlement, the opposite ordering, first-error/second-success on a reused process
credential, multiple requests and retries, stopped/backend-error/retired scopes,
bounded cancellation and timeout, and independent Session progress. Live
acceptance must retain the actual second request's source/model capture and
distinct turn identity before claiming that a later success clears an old error.

### Atomic Invocation Admission

Transport draining alone does not protect a request that resolved an old target
but has not entered the adapter when a configuration Save removes that target.
Close this boundary for ordinary resolution, fallback, the bounded credential
refresh retry, and selected-model probing through the same service admission path.

Extend the internal `EngineAdapter.invoke` contract with optional keyword
`on_admitted: Callable[[], None] | None = None`. The service revalidates the current
effective candidate, exact source configuration, and synchronized engine projection
under its existing mutation lock. The adapter takes its existing routing lock,
validates source/origin, acquires the active transport lease, then calls
`on_admitted` exactly once before any network wait. This handoff releases service
mutation exclusion and records the actual attempt. Network, buffered response,
first-token wait, and stream consumption remain outside the service mutation lock.

Lock order is always service mutation then adapter routing, matching source sync.
Every pre-admission failure or cancellation releases service exclusion; every
post-admission completion or cancellation releases the transport lease. A callback
failure before network must release the lease too. Do not manufacture an attempt
for a stale plan that never entered transport. Recompute unattempted candidates
from current config while preserving already failed-source exclusions; a credential
refresh may retry only its still-valid exact source/model. Probe uses the same
admission primitive instead of a second direct adapter path.

No per-request engine registration, retained invented model inventory, synthetic
upstream model-not-found, persistent generation store, or network-duration global
lock is allowed. Deterministic tests must cross a concurrent target removal/change,
source change, refresh retry, probing, pre-admission cancellation and callback
failure; concurrent independent HTTP requests must still make progress.

## Engine Proof and Acceptance

Remove inventory cardinality/membership admission gates from the service engine
bindings and runtime registry without changing credential or source isolation.
Keep SourceBinding model ids truthful. The pinned CPA executable must be proven
to invoke the chosen source with an unknown canonical model; changing Python
checks alone is insufficient. Do not fabricate source inventory, silently upgrade
CPA, restart it per request, or let CPA select another source. An engine capability
gap is escalated to the orchestrator before a substitute transport is implemented.

### Engine Registration Amendment

The pinned CPA proof at source `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`
confirmed that unregistered source-prefixed targets are rejected before upstream
egress for all three API protocols. Explicitly registered positive controls
preserved the exact upstream id and selected source. This is a transport registry
constraint, not evidence about upstream model availability.

Add `SourceBinding.route_model_ids: tuple[str, ...] = ()` and the corresponding
internal SourceRecord field, loading its absence as empty. The service produces
this deterministic, deduplicated, sorted set from effective Hub-channel route
targets across all backend catalog models and retained route keys, including
dormant OpenCode overrides that the resolver can still select. It is independent
of health and of backend direct/hub mode, and never changes per request. Every
actual mapped target, including a manual target missing from inventory, must be
represented before use.
Retaining transport coverage does not add a catalog row or expand model admission.

`model_ids` continues to carry truthful non-retired source inventory. The engine
config compiler may register the stable union of inventory ids and route target
ids for transport. The latter never enter Source.models, discovery results,
capability metadata, candidate matching evidence, or user inventory. Only actual
inventory declarations supply reasoning capability information. This is an
engine transport projection of configured route intent, not model discovery.

All relevant config commits/reconciliation and load-time synchronization must
refresh this projection through the existing source-sync owner. Preview remains
side-effect-free, and order-only changes with an unchanged target set do not
restart the engine. Registration updates must preserve in-flight invocations.
Hub OAuth registration is not expanded by this field. Only API-key sources need
unknown route targets; subscriptions keep their existing registration behavior.
No per-request restart, invented inventory, silent engine upgrade, or alternate
transport is authorized by this amendment.

### Registration Synchronization Decision

Pinned-engine experiments proved file/atomic/management configuration hot reload
with unchanged PID and uninterrupted existing streams for all three protocols.
However, neither the management acknowledgment nor `/v1/models` provides an
atomic scheduler/config-generation completion barrier. The feature therefore
retains the existing supervised restart transaction with explicit in-flight
protection instead of introducing an unproven readiness heuristic.

The adapter owns invocation leases and drains existing transport operations
before a configuration-triggered engine restart. New invocations wait behind the
same configuration barrier. Lease release belongs to transport completion,
close, or cancellation, independently of service settlement and its mutation
lock. A synchronization cancellation or exception cannot leak a barrier or
lease. Do not cancel unrelated active requests just to save configuration.
Pure reordering with unchanged registration remains restart-free, and ordinary
invocations never trigger registration changes. Exercise buffered and streaming
completion, early close, cancellation, sync failure/rollback, and concurrent Save
to prove there is no deadlock or partial-output interruption.

Acceptance states properties, with fixtures covering the supported shapes:

1. Persisted manual intent survives every unrelated configuration, catalog, health
   and process transition; automatic routes follow defaults and matching evidence.
2. Preview, reads, summaries, guards, probes and runtime agree on one effective plan;
   provenance is independent of transient health and transport readiness.
3. Save/Restore/Undo/Cancel remain transactionally honest across UI, API and reload.
4. Real backend launch, correlation/gateway, adapter, pinned CPA and local upstream
   agree on exact model and source, including unknown models and non-ASCII data,
   without fake inventory, private-prefix leakage or engine cross-source fallback.
5. Request-only errors preserve source health and existing failure/streaming policy.
6. Existing persisted shapes, historical turn records and unrelated configuration
   survive compatibility loading; no real user's config is a test fixture.
7. Desktop/light/dark/mobile screenshots match approved frames, labels and controls
   do not overlap, and hover/focus/touch actions have their intended targets.

Run focused unit/contract/scenario and browser evidence on the integrated head,
then exact-head bot review and all required CI gates. Merge only after the entire
PR has zero unresolved threads and is CLEAN. Fast-forward the control checkout
without reverting unrelated edits. Deploy through the LOCAL Incus runner without
resetting master state; coordinate the shared regression instance before update.
Final success requires merged-head end-to-end acceptance, not mocked peer suites.

## Post-Merge Visual Closure

Real local acceptance exposed a persistent mobile footer clipping defect. Route
dialog actions must remain entirely inside the footer, dialog and viewport when
they wrap. The route footer has a minimum height, not a fixed mobile height; its
content determines its height and the existing scrollable body yields space.
Do not hide Restore, shrink action text, remove the dialog's clipping boundary,
or change the global Button component to make this pass.

Route details expose the complete source identity and exact model id as visible
text, including stale/missing source fallback identities. These detail fields
wrap at constrained widths, with long unbroken identifiers allowed to break;
hop rows retain their existing minimum height and grow with their content.
This applies to editable, inherited read-only and restore-preview rows, including
manual sources outside defaults. It does not replace the approved compact
ellipsis behavior in overview/provider cards. Two values sharing a long prefix
must remain distinguishable without requiring hover, title text, or editing.

The existing AgentCard collection owns one active route-origin help across all
its backend/model rows. A new mouse hover, focus or tap activation replaces the
previous help, including a focused or pinned help. The prior close timer may
close only its own help; it cannot close a newly activated one. Preserve the
existing pointer bridge delay, focus behavior, pin/unpin, Escape and outside
dismissal. Do not use a module-global event bus or a second page-level overlay
owner. Noninteractive route-dialog badges do not participate in this state.

Capture helpers must remove their own incidental pointer/focus state and await
the absence of prior help before a new scenario. The target badge's aria-controls
identifies its help, while a retryable global count confirms no second help.
Do not use first-match selection, forced DOM removal, or repeated Escape to hide
a stuck overlay. Separately exercise focus-A/hover-B, pin-A/hover-B, quick A-to-B
movement and movement across the trigger/content gap at the real common owner.

Visual acceptance checks each actionable control against clipping ancestors,
the viewport and actual hit testing, not just the dialog's outer rectangle or
text width. At narrow and short viewports, all footer controls remain reachable
after long-body scrolling and Save performs the intended mutation. Verify EN/ZH,
light/dark and manual/restore-preview states. Existing required case identities,
baseline gaps and screenshot stages do not change; add assertions within them.

## Delivery Ownership

One feature PR is intentional: executable schema/type/mirror closure needs all
consumers on one head. The orchestrator commits this contract before workers edit,
allocates disjoint file scopes in one task worktree, and alone stages/commits/pushes,
owns review-loop diagnosis, merges, and gates final regression acceptance. Workers
report interface gaps instead of changing other lanes' files. No stacked PRs and
no delegated merge approver are introduced.
