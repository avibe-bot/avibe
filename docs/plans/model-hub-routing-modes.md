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
3. If there are no matching pairs, use each eligible default source with the
   exact requested id. Do not append speculative candidates to a matched tier.
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
  and shows the existing request detail surface; do not mark the whole source failed.
- Apply complete EN/ZH localization and keyboard/touch behavior. Documentation
  remains English; public user documentation has matching English/Chinese pages.

## Engine Proof and Acceptance

Remove inventory cardinality/membership admission gates from the service engine
bindings and runtime registry without changing credential or source isolation.
Keep SourceBinding model ids truthful. The pinned CPA executable must be proven
to invoke the chosen source with an unknown canonical model; changing Python
checks alone is insufficient. Do not fabricate source inventory, silently upgrade
CPA, restart it per request, or let CPA select another source. An engine capability
gap is escalated to the orchestrator before a substitute transport is implemented.

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

## Delivery Ownership

One feature PR is intentional: executable schema/type/mirror closure needs all
consumers on one head. The orchestrator commits this contract before workers edit,
allocates disjoint file scopes in one task worktree, and alone stages/commits/pushes,
owns review-loop diagnosis, merges, and gates final regression acceptance. Workers
report interface gaps instead of changing other lanes' files. No stacked PRs and
no delegated merge approver are introduced.
