# Memory

Avibe Memory distills eligible Workbench and private-IM messages into a
per-user profile, episodes, and facts. Open **Settings > Memory** to inspect its
Processing Record, current profile, search results, and settings.

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
profiles. Processed episode listing remains user-owner-only, so use Search or
Profile to inspect Agent-owned memory.

Existing Memory data is not moved. Agent-recorded facts written before this
split remain under the user owner and are still searchable there. A newer
installation preserves the released four-field provider-session shape, but
delivery remains process-local; pending Agent-owner work may be lost across restart,
rollback, or runtime replacement and is never replayed from an older queue.
On rollback, already-delivered Agent-owner memories remain stored but are hidden
from an older reader until Avibe is upgraded again. Already submitted provider
outcomes are never replayed by this path.

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
episodes newest first. Choose one project or **All my projects**, use the page
controls below the episode excerpts, then select a row to open its full detail.
The **Entry ID** chip copies the provider's opaque identifier. Entering any
non-empty query switches the same tab back to relevance-ordered search.

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

**Wake** is the ordinary, non-destructive availability operation. It validates
the admitted `memory-runtime` artifact, reinstalls it when needed, proves the old
owned process stopped, and starts the same EverOS root. Startup and unexpected
child exit use the same path. Readiness uses bounded backoff and the native
EverOS health path. Wake never deletes or recreates Memory data.

Provider, credential, disk, and permission faults produce `degraded` with a
sanitized reason. Correct the external condition and use Wake again. These faults
never enable or route to destructive Repair.

**Repair** is offered only in `needs_repair`, when the local native data root is
unusable or incompatible. Every UI, public API, internal API, client, and
Controller boundary requires the exact `confirm_loss: true` field. Repair then:

1. acquires the Memory operation lease and proves the old owned process tree is
   stopped;
2. preserves Memory settings, credentials, stable scope identity, and the
   project catalog while rotating the provider data generation;
3. removes only the confined `<effective_home>/memory` root and narrowly named
   retired recovery residue;
4. reuses Wake and reports success only after native EverOS readiness succeeds.

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

Released `recovery_intent`, `embedding_change_pending`, cloud transition, and
Clear-state shapes are compatibility input only. Their retired stages are not
serialized or resumed. Unsafe compatibility evidence collapses into an internal
`repair_required` fence that ordinary saves preserve until a successful
destructive Repair clears it.

## Best-effort capture

Capture is deliberately volatile and best effort. A process-local writer admits
at most 256 captures across attachment pinning, queued work, provider calls, and
terminal cleanup. It keeps one 256-entry source-message LRU, one ordered worker,
and a bounded pending-flush tracker (256 sessions, 100 message IDs per session).
Idle, age, and count thresholds are fixed at five minutes, thirty minutes, and
100 acknowledgements.

Admission writes only stable identity facts to `memory.sqlite`: the install
scope key, epoch, provider timestamp watermark, and project catalog. After v4
migration the only application tables are `memory_meta` and `memory_projects`;
released queue, lease, settlement, attachment-reference, and recovery tables
are discarded without provider I/O. Older v0-v3 shapes preserve identity and
project facts, deriving legacy project rows from their former capture data.

Lifecycle offers and barriers are non-blocking. `/new`, archive, runtime
replacement, and shutdown never wait for capture delivery. Work still preparing
or belonging to a stale authority generation may be missed or invalidated.
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
