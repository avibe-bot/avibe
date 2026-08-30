# Memory

This is the canonical product and architecture contract for the current Memory
system. Superseded Memory design plans are available in Git history only; they
must not be treated as current behavior.

Avibe Memory distills eligible Workbench and private-IM messages into scoped
User and Agent profiles, episodes, and facts. Open **Settings > Memory** to
inspect its Processing Record, current profiles, search results, and settings.

## Design philosophy

The refactored Memory system follows one priority: **keep Avibe and its Agent
available, then preserve Memory data on a best-effort basis**. EverOS runs as an
isolated child service that Avibe can wake, stop, replace, or explicitly repair.
Memory intake may be lost when the process crashes, restarts, is replaced, or is
overloaded; the design aims to retain data in ordinary operation, not to provide
zero-loss or exactly-once delivery.

The following rules are architectural invariants:

1. **Memory is optional to the main product path.** Memory latency or failure
   must not make chat, the Agent, `/new`, archive, replacement, or shutdown
   unavailable. Lifecycle offers and barriers therefore stay bounded and
   non-blocking.
2. **Acceptance is not persistence.** An accepted capture has entered bounded,
   process-local work. It does not prove EverOS received or persisted it. Avibe
   keeps no durable outbox, replay ledger, or per-call delivery workflow.
3. **Resources and retries are bounded.** Admission, attachment preparation,
   provider calls, pending flushes, and restart attempts all have finite limits.
   At capacity, Memory may drop work while the primary product continues.
4. **EverOS native data is the only Memory content truth.** Search, profiles,
   episodes, facts, and the Processing Record are projections of retained
   native data. Avibe does not maintain a parallel Provider Call Log,
   correlation ledger, or reconstructed history to make incomplete evidence
   look complete.
5. **Each lifecycle responsibility has one owner.** Admission belongs to
   `CaptureAdmission` and `MemoryModule`; product policy belongs to
   `MemoryRuntime`; volatile delivery belongs to `BestEffortMemoryWriter`;
   child ownership and restart budgeting belong to `EverOSSupervisor`; one
   launch attempt belongs to `EverOSProcess`. Callers do not borrow their
   internals.
6. **Wake is the one non-destructive availability path.** Initial startup,
   manual retry, service restart, and crash recovery all reuse bounded Wake.
   Wake never deletes Memory data and never starts a replacement before the old
   owned process is proved stopped.
7. **Data loss requires explicit authority.** Repair, Delete data, and
   identity-invalidating configuration require exact `confirm_loss: true`,
   stop proof, and confined deletion. Ambiguity fails closed instead of widening
   the delete surface or silently falling back.
8. **Identity and diagnostic truth stay explicit.** User and Agent owners are
   server-derived and isolated. Partial or missing native evidence is reported
   as `partial` or `unavailable`; a possibly submitted provider result is never
   replayed.

## Current architecture

Platform adapters classify native events but do not own Memory business logic.
They normalize each event into `InboundTurnFacts`; `CaptureAdmission` treats
those facts as untrusted and rechecks identity, platform, event shape, and
settings before admitting a capture. `MemoryModule` owns the admitted product
operation and its scoped read behavior.

| Component | Single responsibility |
| --- | --- |
| Platform adapters | Classify native events and normalize transport-specific facts without deciding Memory business eligibility. |
| `CaptureAdmission` | Revalidate untrusted inbound facts and make the single capture-admission decision. |
| `MemoryModule` | Derive owner/project scope and expose admitted capture and read semantics without exposing storage internals. |
| `BestEffortMemoryWriter` | Owns bounded, ordered, volatile capture delivery and flush attempts. |
| `MemoryRuntime` | Owns public state, configuration policy, operation exclusion, and destructive-operation admission. |
| `EverOSSupervisor` | Exclusively owns the current child, readiness, bounded Wake/restart recovery, stop proof, and released-orphan reconciliation. |
| `EverOSProcess` | Adapts one private EverOS launch attempt, its process identity, UDS readiness, resource bounds, and termination. |
| Native readers | Read caller-authorized EverOS profiles, episodes, facts, runs, and indexing state without creating another source of truth. |

The data and lifecycle paths remain separate and short:

`eligible input -> CaptureAdmission -> MemoryModule -> bounded writer -> private EverOS UDS`

`MemoryRuntime -> EverOSSupervisor -> one EverOSProcess attempt`

Reads use the same private EverOS service and its active native root.
`memory.sqlite` retains stable Avibe identity and project-catalog facts plus a
bounded metadata row for timestamp, capture-outcome, and processing-fault
diagnostics. It contains no Memory payload and is not a delivery queue or
recovery state machine. The later sections define the product behavior at these
seams.

## Optional package and upgrades

The implementation is the optional `avibe-memory` distribution. Avibe loads it
in-process only when Memory is required; a missing, incompatible, or failed
package degrades Memory without making Avibe or its Agent unavailable.

Forward upgrades preserve the Memory package when Memory is enabled or the
package is already present. A successful upgrade schedules the ordinary Avibe
restart when the runtime is active. Package resolution, installation, upgrade,
or restart failures are structured terminal results, and a later explicit
attempt is allowed. Avibe performs no automatic package rollback and keeps no
rollback plan, lifecycle reservation, quarantine, Gate 5 lifecycle verifier, or
recovery bootstrap.

Published Memory Runtime artifacts retain their manifest, hash, fetch, verify,
and backup availability safeguards. Those checks protect immutable release
assets and manual recovery inputs; they do not compensate for or reverse an
installed-package transition.

## User and Agent memory ownership

Automatic capture of a user's messages continues to target that user's Memory
owner. An accepted `vibe memory remember` request is offered for best-effort,
process-local delivery under a separate, Avibe-derived Agent owner for the same
caller; acceptance does not guarantee provider delivery or persistence. The
derived owner ends in `-agent`; callers cannot choose it or use another user's
owner. User and Agent
captures use disjoint provider sessions, so their episodes, facts, and profiles
cannot be distilled into the same Memory cell.

Search reads both owners. Results are labeled **User**, **Agent**, or **Both**;
an exact text match held by both owners is returned once with the **Both** label.
Profile reads show separate labeled blocks rather than combining the two
profiles. The Settings episode browser has an explicit **User memory** /
**Agent memory** selector; `vibe memory list` remains user-owner-only.

Existing Memory data is not moved. Agent-recorded facts written before this
split remain under the user owner and are still searchable there. A newer
installation preserves the released four-field provider-session shape, but
delivery remains process-local; pending Agent-owner work may be lost across restart,
or runtime replacement and is never replayed from an older queue. Already
submitted provider outcomes are never replayed by this path.

## IM attachment capture

Memory can extract supported attachments from bound one-to-one conversations on
Slack, Discord, Telegram, Lark, and WeChat. Capture becomes available only when
Memory is enabled and the Multimodal LLM endpoint under **Settings > Memory** is
fully configured. It does not change the files delivered to the Agent.

Only direct, ordinary files shared by a human are eligible. Bot, system,
forwarded, edited, quoted, rich, and unrecognized native message shapes are
excluded. Avibe then validates each eligible file against one shared format and
content policy. Supported formats are plain text, Markdown, CSV/TSV, VTT, PDF,
bitmap images, audio, HTML, EML, and Office / iWork / ODF / RTF documents when
LibreOffice is installed. Native Windows currently skips these Office formats;
Linux and macOS hosts can enable them with a sidecar-visible LibreOffice install.
SVG and video stay excluded. Unsupported or malformed
files are skipped independently, so eligible text and valid siblings can still
be captured.

Each turn is limited to 8 captured attachments, 25 MiB per attachment, and
100 MiB in total. Admitted files are copied into private Avibe storage until the
process-local Memory delivery settles. Their extracted content can be sent to the
configured multimodal provider, so configure that endpoint according to your
data-handling requirements. Clearing Memory Data removes retained local
attachment bundles but cannot remove copies already accepted by a provider.

## Processed episode listing

`vibe memory list` reads only valid, active processed episodes for the current
scoped principal and project. It does not include profiles, agent memories,
atomic facts, unprocessed messages, or superseded episodes. Single-project
reads use EverOS's 1-based pages and fixed newest-first order. The verified
Settings UI may aggregate the same principal's projects through a bounded,
versioned Avibe cursor; the Agent CLI cannot request `all`. JSON preserves the
provider's opaque entry id as a future inspection handle. Listing does not add
Search/Get payloads to provider diagnostics and does not invoke LLM, embedding,
or reranking providers.

In **Settings > Memory > Search**, leave the query empty to browse these
episodes newest first. Choose **User memory** or **Agent memory**, then choose
one project or **All my projects**. Use the page controls below the episode
excerpts, then select a row to open its full detail. The **Entry ID** chip copies
the provider's opaque identifier. Entering any non-empty query switches the
same tab back to relevance-ordered search across both owners.

## Optional reranking endpoint

The third processing endpoint in **Settings > Memory** is optional. Choose one
EverOS rerank provider (`deepinfra`, `vllm`, or `dashscope`) and configure that
provider's Base URL, model, and API key together. Changing a configured
endpoint is admitted by a provider-specific bounded preflight before it is
saved. Older configs without `provider` keep the DeepInfra probe and sidecar
protocol, except an omitted-provider Bailian workspace host
(`*.maas.aliyuncs.com`) is inferred as DashScope. Leave the rerank fields empty
to keep the standard Memory search tier. Removing the saved reranking endpoint
clears the provider and the three endpoint values and does not rebuild the
embedding index. DashScope currently accepts only `gte-rerank-v2`; the Base URL
is either `https://dashscope.aliyuncs.com` or a Bailian workspace host.

## Processing Record

The Processing Record is a bounded, caller-scoped view of native EverOS data for
the selected project and the caller's user and Agent owners. A detail can show
the authorized source payload, retained native runs, linked Episodes and Atomic
Facts, and the current profile and indexing state. Current state is explicitly
unattributed; it is not reconstructed historical state for that run.

Each source is read independently. Missing, busy, malformed, or retained-away
data is shown as unavailable while other sections continue to render. This is a
best-effort diagnostic: Avibe keeps no durable per-call observer, replay queue,
or Provider Call Log, and records may be incomplete or lost. Released config
files containing `memory.diagnostics.log_provider_calls` still load, but the
field is ignored and is not emitted by new config or API serialization.

Processing faults are written only to the main Avibe service log. Capture loss
does not generate administrator messages on IM transports.

## Runtime status and recovery

The API, CLI, and Settings UI share one runtime state and one short reason. The
state is one of `disabled`, `starting`, `running`, `degraded`, or
`needs_repair`. Memory failure never makes Avibe, its Agent, or chat unavailable.
Conflicting Memory lifecycle operations are rejected by one process-level lease.

Internally, **Wake** is the ordinary, non-destructive availability path. It
validates the admitted `memory-runtime` artifact, reinstalls it when needed,
proves the old owned process stopped, and starts the same EverOS root. Startup
and unexpected child exit use the same path. Readiness uses bounded backoff and
the native EverOS health path. Wake never deletes or recreates Memory data.

Settings names this path by user intent: `degraded` shows **Retry startup**,
while `running` keeps **Restart Memory service** under **More actions**. Both use
the same non-destructive Wake path.

Provider, credential, disk, and permission faults produce `degraded` with a
sanitized reason. Correct the external condition and choose **Retry startup**.
These faults never enable or route to destructive Repair.

**Repair** is offered only in `needs_repair`, when the local native data root is
unusable or incompatible. Every UI, public API, internal API, client, and
Controller boundary requires the exact `confirm_loss: true` field. Repair then:

1. acquires the Memory operation lease and proves the old owned process tree is
   stopped;
2. preserves Memory settings, credentials, stable scope identity, and the
   project catalog while rotating the provider data generation;
3. removes only the confined `<effective_home>/memory` root and narrowly named
   retired recovery residue;
4. reuses the non-destructive startup path and reports success only after native
   EverOS readiness succeeds.

If ownership or termination cannot be proved, Repair deletes nothing. If a
confined deletion is partial or unsafe, the response reports the exact remaining
surface and stays `needs_repair`. A failed or interrupted Repair has no durable
stage to resume; the next startup reevaluates the root, and an operator can run a
new explicitly confirmed Repair from the beginning.

**Delete data** is a separate user intent with its own response and confirmation.
It uses the same stop-before-delete and confined reset primitive as Repair, but
does not require a prior `needs_repair` state. It is not a secure wipe and cannot
remove original Avibe chats or copies already accepted by a remote provider.

An Embedding identity change invalidates the native root. Saving such a change
uses the same explicit accepted-loss boundary and unified reset; there is no
candidate config, rebuild marker, retry stage, or fallback to old settings.

When a working custom installation first receives organization-managed Memory
capability, Avibe persists `cloud.transition_notice_pending` as an
acknowledgement fence and keeps the current custom source selected until the
user explicitly confirms the identity-changing reset. This flag records a
pending user decision, not capture delivery or resumable recovery progress, and
does not promise to retain capture while the decision is pending.

Released `recovery_intent`, `embedding_change_pending`,
`transition_rebuild_owned`, and Clear-state shapes are compatibility input only.
Their retired execution stages are not serialized or resumed. Unsafe
compatibility evidence collapses into an internal `repair_required` fence that
ordinary saves preserve until a successful destructive Repair clears it.

## Best-effort capture

Capture is deliberately volatile and best effort. A process-local writer admits
at most 256 captures across attachment pinning, queued work, provider calls, and
terminal cleanup. It keeps one 256-entry source-message LRU, one ordered worker,
and a bounded pending-flush tracker (256 sessions, 100 message IDs per session).
Idle, age, and count thresholds are fixed at five minutes, thirty minutes, and
100 acknowledgements.

Persistence is metadata-only. `memory.sqlite` stores the install scope key,
epoch, provider-root id, provider timestamp watermark, project catalog, capture
summaries (`missed_count`, `last_success_at`, `last_error`, and their
timestamps), and bounded processing-fault diagnostics. These fields summarize
local state; they contain no message payload or per-call delivery workflow.
After v4 migration the only application tables are `memory_meta` and
`memory_projects`; released queue, lease, settlement, attachment-reference, and
recovery tables are discarded without provider I/O. Older v0-v3 shapes preserve
identity and project facts, deriving legacy project rows from their former
capture data.

Lifecycle offers and barriers are non-blocking. `/new`, archive, runtime
replacement, and shutdown never wait for capture delivery. Work still preparing
after its runtime authority is revoked may be missed or invalidated.
Shutdown and replacement intentionally drop volatile work.

The writer gives add and flush operations at most three attempts, and retries
only failures proven to occur before provider execution. A possibly submitted
result is never replayed; the existing owned-sidecar stop/reap path is invoked
and Memory remains fail closed if termination cannot be proved. Attachment
pinning runs under the same bound. A confined pin/cleanup failure disables
attachment intake for that runtime, while a non-empty caption may be offered as
text-only. The single provider rejection that positively proves no attachment
write permits the same caption fallback.

Processing Record is an authorization-scoped, best-effort projection of retained
native data. Missing evidence is reported as unavailable and never widens access.

## Recall policy

Recall accepts one closed policy with `auto`, `keyword`, `vector`, `hybrid`, or
`agentic` mode. `auto` selects hybrid only when the latest trustworthy EverOS
health observation explicitly reports embedding capability; otherwise it uses
keyword. Explicit vector or hybrid requests fail closed when that capability is
missing. A request performs at most one provider search and never retries in a
different mode.

Agentic recall is available only through the CLI when health explicitly reports
embedding, LLM, and rerank capabilities and does not disable `agentic_search`.
It uses one request with a sidecar-owned wall-clock deadline of at most 30
seconds; unavailable capabilities fail closed. EverOS 1.2.3 does not enforce
model-call and token ceilings, so those policy fields remain a declarative
envelope rather than an independently enforced provider budget.
Current-session overlay can only use the trusted caller session supplied by the
runtime; callers cannot provide arbitrary provider filters or session IDs.
