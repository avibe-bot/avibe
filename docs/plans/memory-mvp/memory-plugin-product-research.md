# Memory Provider Research and MVP Decision

> Status: MVP reset, 2026-07-21
>
> Decision: `everos==1.1.3` is the first provider to evaluate in an isolated
> phase-0 POC. It is not yet selected for production integration.
>
> Related documents:
> - EverOS deep dive: `docs/plans/memory-mvp/everos-1.1.3-deep-dive.md`
> - Product scope: `docs/plans/memory-mvp/memory-plugin-everos-phase1.md`
> - Provider POC: `docs/plans/memory-mvp/memory-poc-everos.md`
> - Technical design: `docs/plans/memory-mvp/memory-plugin-everos-phase1-tech.md`

## 1. Product question

Avibe needs to learn whether a local-first memory system can create a useful,
durable personal profile and timeline from the owner's conversations without
making the desktop runtime too heavy or the product impossible to govern.

The first decision is not "which provider has the most features?" It is:

1. Does the generated memory help with realistic Chinese and mixed-language
   queries?
2. Can it run on an ordinary personal computer within Avibe's install model?
3. Can users understand where their data goes and clear the local memory?
4. Is the provider interface stable enough for a deliberately small MVP?

Exact chat-history search remains the provider-independent baseline. The memory
feature must add profile and episodic recall rather than duplicate message
search with a vector index.

## 2. The MVP contract drives provider selection

The MVP intentionally makes three narrower product choices:

- **One personal pool.** One Avibe install has one provider principal and one
  global personal memory. Bound, enabled administrator identities in private IM
  are explicit co-owners of that pool; this is not per-user isolation or a
  general sharing model. There is no workspace partitioning.
- **Whole-memory governance.** The MVP supports disable and full local clear.
  Selective item deletion and profile rebuild are later capabilities.
- **At-least-once ingestion.** Avibe deduplicates its own queue by a keyed digest
  of the source-qualified message id, but a timeout or crash around a provider
  write can produce a duplicate derived memory. The MVP reports failed work and
  does not claim exactly-once provider delivery.

These are product semantics, not waivers from a stronger phase-1 promise. A
later requirement for selective deletion, exactly-once ingestion, workspace
isolation, or shared memory reopens the provider decision before that capability
ships.

The MVP also excludes automatic recall, all agent-facing Memory tools,
write-capable command/CLI operations, group and network access, non-administrator
DM access, export/import, and foresight. It exposes the same bounded
profile/search/status reads through Workbench `/memory`, authorized private-IM
`/memory`, and the local CLI. Those entry adapters must not add provider
capabilities or influence the first provider interface.

## 3. Candidate summary

The research completed before this reset remains useful, but only the evidence
needed for the MVP is carried forward.

| Candidate | Product strength | Desktop / ownership cost | MVP position |
|---|---|---|---|
| EverOS 1.1.3 | Profile, episodes, facts, inspectable Markdown; lightest memory-system runtime surveyed | Missing item deletion, weak write acknowledgement/idempotency, fixed internal formats | First POC candidate |
| Memobase 0.0.40 | Structured profile slots and event timeline; predictable extraction shape | Postgres and Redis; upstream in maintenance mode | Revisit if EverOS quality or ownership fails |
| MemOS 2.0.x | Richest memory model and governance | Neo4j, Qdrant, Docker/JVM footprint; redistribution review | Not a default desktop MVP candidate |
| Mem0 OSS 2.0.x | Mature flat-fact CRUD and light integration | Does not provide the profile/episode product by itself | Engineering fallback |
| MemU | Useful simple storage/retrieval reference | Distillation is primarily agent-owned | Design reference only |

The detailed ecosystem comparison from the prior revisions should be retained
in Git history rather than copied into the implementation contract. Candidate
versions, licenses, dependencies, and upstream health must be rechecked when a
candidate is reconsidered.

## 4. Why EverOS is evaluated first

EverOS is the best first experiment because it most directly tests the product
thesis:

- it produces a user profile and episode timeline rather than only flat facts;
- its distilled state is inspectable as Markdown;
- it has no external database dependency;
- its buffer and flush model fits an asynchronous desktop worker;
- it is small enough to measure on a normal Avibe development machine.

The POC uses official `everos==1.1.3` without a fork so the experiment measures
the upstream product. A production decision may still require a small Avibe fork
or a different provider. "No fork" is not a permanent architecture constraint.

## 5. Known EverOS risks

The POC must confirm or price these risks before implementation starts.

### 5.1 Data lifecycle

EverOS retains unflushed messages and extracted raw MemCells in hidden local
SQLite state in addition to the visible Markdown tree. The MVP must disclose
that second local copy. Full clear must remove the dedicated provider root.

EverOS 1.1.3 does not provide supported item-level deletion or a reliable way to
rebuild the profile after deleting one source. The MVP therefore does not expose
`forget`.

### 5.2 Delivery semantics

EverOS does not expose a stable caller-supplied idempotency key or a write receipt
that proves every derived artifact was materialized. The MVP accepts
at-least-once ingestion and a bounded retry policy. It does not inspect EverOS's
private SQLite/Markdown state to manufacture an exactly-once protocol.

The POC must characterize duplicates under a process kill or response loss. If
duplicates are common enough to damage memory quality, production integration
requires an upstream fix, an Avibe fork with a real idempotency contract, or a
different provider.

### 5.3 Scope model

EverOS issue #320 describes cross-project index collisions because some row
identity omits project fields. The MVP uses exactly one owner and one fixed
project, so it does not create the defect's second-project precondition and
makes no workspace-isolation claim. The MVP POC does not spend a release gate on
that deferred topology. Any future second project or workspace requirement must
reproduce the issue and reopen the provider decision first.

### 5.4 Processing and resource use

EverOS needs separate OpenAI-compatible LLM and embedding endpoints. Avibe's
Claude/Codex/OpenCode subscription or OAuth backends are not processing
endpoints. Captured owner text can leave the machine when either configured
Memory endpoint is remote.

The POC must measure install size, idle and peak RSS, provider-root growth,
provider call counts, write-to-searchable latency, and query latency. Estimates
are not sufficient for a production decision.

### 5.5 Upstream interface stability

The integration will pin one exact version. Production may use only documented
HTTP behavior plus the configured provider root as an opaque, sentinel-owned
lifecycle unit. It may verify and delete that root, but it does not open
Markdown or SQLite files to infer delivery success. Core Avibe workers never
understand MemCells, cascade tables, internal evidence states, or private
database schemas.

If the MVP cannot be implemented without those internals, the provider has
failed the integration-shape gate.

## 6. Phase-0 POC gates

The POC is deliberately provider-only and runs before production migrations,
workers, UI, or backend changes exist. Its canonical pass criteria live only in
`docs/plans/memory-mvp/memory-poc-everos.md` section 6 so thresholds cannot drift between
documents.

Those criteria cover product quality, temporal behavior, direct-query latency,
write-to-searchable latency, desktop resource use, lifecycle/retention honesty,
recorded egress, restart/full-clear behavior, and the ability to integrate
without core code reading private EverOS databases.

A failed criterion is a decision result, not an invitation to design a
compensating production subsystem before the provider choice is revisited.

## 7. Decision after the POC

The POC report must recommend exactly one of these outcomes:

### A. Integrate official EverOS as an experimental MVP

Choose this only when quality and desktop gates pass, ordinary behavior is
stable, duplicate risk is acceptable for a private MVP, and the adapter can stay
thin. Pin the exact version and expose the at-least-once/full-clear limitations.

### B. Maintain a small Avibe fork

Choose this when EverOS is a strong product fit but production requires a narrow
stable contract such as caller-supplied idempotency, explicit write receipts,
or Unix-domain-socket-only binding. The fork must remove more Avibe-side
complexity than it creates.

### C. Stop and evaluate the fallback

Choose this when quality, footprint, egress, lifecycle behavior, or adapter
shape fails. Memobase is the next memory-system candidate; Mem0 is the simplest
flat-fact fallback if profile/episode generation moves into Avibe.

## 8. Integration constraints after a go decision

The following constraints are stable across outcomes A and B:

- one controller-owned `MemoryModule` presents a small product-level interface;
- the Memory page, Workbench `/memory`, authorized private-IM `/memory`, and local
  `vibe memory` reuse the same `profile`, `search`, and `status` operations; only
  automatic capture writes and only the UI owns Clear all;
- provider behavior stays behind one internal port used by the real provider and
  a test fake;
- one fixed `app_id=avibe`, `project_id=personal`, and local owner principal are
  used for the MVP; bound, enabled IM administrators are co-owners of that one
  pool rather than separate provider principals;
- the provider runs in a dedicated version-pinned environment and owner-only
  root; Avibe packages that environment as one managed `memory-runtime`
  dependency, so users do not install Python or individual transitive packages;
- `memory-runtime` appears under Settings -> Dependencies and reuses the shared
  background install/status/repair flow; it is optional while Memory is disabled
  and required while enabled;
- its artifact implementation specializes Avibe's existing managed-runtime
  Module, while the EverOS child-process lifecycle remains private to Memory;
- dependency installation and Memory enablement remain separate explicit
  actions; no pending enable intent survives the request;
- capture never blocks ordinary chat on provider I/O;
- every explicit-read result bypasses agent backends and Memory capture;
  Workbench results bypass ordinary Avibe transcript storage, while private-IM
  results are sent as bounded inert replies and remain subject to that platform's
  chat-history and notification retention;
- processing destinations and hidden raw retention are disclosed before enable;
- full clear stops the sidecar and removes only a sentinel-owned Memory root;
- no future provider capability is added to the interface before a real second
  implementation or product requirement exists.

## 9. Deferred capability questions

These questions belong to later capability decisions, not the MVP:

- selective source deletion and profile rebuild convergence;
- workspace partitioning and controlled sharing;
- independent per-user pools, non-administrator DMs, group IM, and cross-platform
  human-identity linking;
- automatic recall quality and prompt-injection handling;
- agent-facing Memory tools, including MCP transport, turn/session binding, and
  backend-specific registration;
- export/import as a provider-neutral format;
- editable memory with durable index convergence;
- foresight and agent-skill tracks;
- provider migration between different memory models.

Each later capability gets its own POC and interface change. It must not be
prepaid in the first implementation.
