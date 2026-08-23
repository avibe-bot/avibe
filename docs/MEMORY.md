# Memory

## Processing preflight and rebuild recovery

Confirmed embedding identity changes and retained rebuild retries validate the
candidate with one bounded chat request and one bounded embeddings request
before the active sidecar is quiesced. When the optional reranking endpoint is
configured, preflight also makes one bounded reranking request. Provider
failures identify the side, HTTP status, provider code, and a sanitized
message; the durable candidate and rebuild intent remain available for Retry.
Ordinary runtime restart keeps its existing behavior.

Avibe Memory distills eligible Workbench and private-IM messages into a
per-user profile, episodes, and facts. Open **Settings > Memory** to inspect its
Processing Record, current profile, search results, and settings.

## User and Agent memory ownership

Automatic capture of a user's messages continues to write to that user's Memory
owner. A successful `vibe memory remember` records the fact under a separate,
Avibe-derived Agent owner for the same caller. The derived owner ends in
`-agent`; callers cannot choose it or use another user's owner. User and Agent
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

The Processing Record combines a direct, bounded EverOS health projection with
source availability, confirmed pipeline anomalies, clear recovery, and the
existing processing timeline. It has an explicit Refresh action rather than a
composite status poll. A section may be marked stale or unavailable while the
other independently sourced sections continue to render, including while
Memory is disabled.

Memory processing fault events are written only to the main Avibe
service log, including the fault kind and occurrence time. Volatile-capture
loss is not an administrator message. They are not sent as direct messages on Slack,
Discord, Telegram, Lark, WeChat, or other IM transports.

The timeline is the installation operator's view across every valid project and
Memory owner. Each row and its detail view visibly includes the full **Project
ID** and **User ID**; a derived `-agent` value identifies the caller's Agent
owner. The timeline then follows the path from message capture through memcell
creation, processing strategies, profile triggers, and current indexing state.
The UI routes grant this broad read scope to an authenticated local or Avibe
Cloud Memory session; profile and search reads remain caller-scoped while
covering that caller's user and Agent owners.

Provider calls are attached only when Avibe can prove their exact provenance to
the displayed project and Memory owner. Capture delivery has no durable
provenance table; missing evidence is unavailable and never widens access.

## Provider payload logging

Provider payload diagnostics are always recorded while Memory is enabled. The
legacy `memory.diagnostics.log_provider_calls` field is still accepted when an
older config file is loaded, but its value is normalized to `true` and there is
no settings switch to turn recording off.

Avibe retains bounded, scrubbed LLM, multimodal-LLM, embedding, and reranking
request and response fields used during Memory processing. It does not store
embedding vectors, attachment bytes, raw sidecar stdout/stderr, or unrelated
Search and Get calls. Secrets, configured provider URLs, and absolute local
paths are scrubbed, and large fields are truncated or replaced with omission
markers. Diagnostic payloads can still contain sensitive conversation text;
treat access to the operator log accordingly.

Rows remain readable until normal expiry or Clear Memory Data. Retention runs while
Memory is disabled as well: rows expire after 14 days and only the newest 5,000
calls are kept.

## Recovery ladder

Use these actions in order, from least to most destructive:

Avibe refuses conflicting Memory maintenance requests. Let the current action
finish before starting another one.

1. **Restart engine**: Use it for a temporary recorder or engine failure.
   Memory must be enabled. This restarts the Memory engine without changing
   settings, rebuilding indexes, or deleting retained data. If the call-log
   database is corrupt and recording remains degraded after a restart, use
   **Clear Memory Data** before escalating further.
2. **Repair index**: Use it when restarting does not clear index health
   warnings or pending work. The **Repair index** action is shown only when
   Memory is enabled and `repair_available` is true (the installed Memory
   artifact must advertise repair capability and no rebuild or factory-reset
   marker may be pending). With a loaded health snapshot it appears beside
   **Processing queue**; an unavailable snapshot moves it beside **Engine
   status** rather than hiding it. Running Repair also requires the live Memory
   Runtime and sidecar to be available.
   Requests while Memory is disabled are refused, and an unavailable runtime or
   sidecar causes Repair to fail. Repair rescans Markdown memory and drains
   pending work while keeping the engine available; it preserves the existing
   index and may use Embedding API quota. **Memory index repair completed with
   health warnings.** means the repair finished but the returned health is still
   unhealthy. Address the reported condition, then select **Repair index**
   again; a failed repair can be retried the same way.
3. **Rebuild index**: Use it after changing the Embedding endpoint or model,
   or to recover a pending rebuild. Confirming **Save and rebuild** saves the
   new settings before rebuilding the local vector index and preserves Markdown
   memory. If rebuilding fails before settlement, the confirmed change remains
   saved, the recovery intent and rebuild warning remain, and **Restart engine**
   stays unavailable. An Embedding endpoint or model correction changes the
   vector-space identity, so edit it and reconfirm **Save and rebuild**; do not
   apply that correction through **Retry rebuild**. While the rebuild marker is
   pending, LLM endpoint or model corrections may be saved normally before
   selecting **Retry rebuild**; API-key-only corrections for either provider
   may also be saved under the marker without touching the fenced runtime,
   after which select **Retry rebuild**. If rebuilding completes but the later
   engine or sidecar activation fails, the recovery intent may already be
   cleared. Fix the runtime problem, then select **Restart engine**;
   **Retry rebuild** may no longer be offered.

### Clear Memory Data

Before Reinitialize Memory, use **Clear Memory Data** when retained Memory data or the
call-log database is corrupt. Clear Memory Data records a durable intent marker, then
removes only the queue, provider data, call log, and pinned attachments through their
idempotent deletion primitives. Clear is irreversible; it does not delete
the `memory` or `state/memory` roots themselves, original Avibe chats, copies
already sent to providers, or data outside those surfaces (including logs or
user-created snapshots); it is not a secure wipe.

Clear Memory Data is also the explicit discard path for a timed-out or otherwise
unknown provider add. It removes the retained `manual_required` queue evidence
and pinned attachment bundle, clears that session's local fence, and never
replays the ambiguous add. Because the provider outcome is unknown, Clear cannot
remove a copy that may already have reached the provider.

If Clear Memory Data is interrupted, the marker remains durable and Processing Record
shows its operation, deleting/failed state, timestamp, and error code. Boot automatically
retries the four idempotent deletion primitives; Memory remains fenced until the marker
is removed after all surfaces finish. A failed marker can be retried by Clear Memory Data.
Unreadable markers fail closed without preventing service startup. A retired Clear journal
is never semantically migrated: an open row or failed bounded probe creates a fresh failed
marker that asks the user to run Clear again, while terminal residue is removed best effort.
Retired backup and snapshot residue is also cleaned best effort and never blocks Memory.

4. **Reinitialize Memory**: Use it only as a last resort when the earlier actions
   cannot recover Memory. It is available on the Memory Runtime card under
   **Settings > Dependencies** when the
   pinned, installed Memory artifact is valid. It permanently deletes local
   Memory data and related operational state from the mixed-purpose `memory`
   and `state/memory` storage locations. The former may include profiles,
   facts, indexes, call diagnostics, and runtime files; the latter may include
   processing queues, recovery progress, and coordination state. Only a
   successful cutover starts fresh, usable Memory. It
   preserves Memory settings and credentials, the pinned, installed Memory
   artifact, original Avibe chats, and data outside those two locations. If engine
   or sidecar activation fails after deletion, the old contents under `memory`
   and `state/memory` stay deleted, but construction of the fresh runtime may
   have recreated empty or partial directories. Reinitialize Memory reports a
   visible status for each storage location independently: **deleted**, **partially
   deleted**, **absent**, or **retained**. Settings also shows a generic failure
   status, not a per-location error or reason; read each location's status independently
   rather than treating the result as a clean reset. If a root is **retained** or
   **partially deleted**, inspect the service logs and filesystem permissions
   for that root and correct the deletion failure. If deletion completed but
   engine or sidecar activation failed because a persisted LLM or Embedding
   endpoint, model, or credential is invalid, correct the corresponding
   processing settings under **Settings > Memory** while the factory-reset
   recovery intent is pending. Endpoint repair does not fix a retained or
   partially deleted root. After correcting the applicable cause, select
   **Retry initialization** to continue recovery.
   Memory stays fenced and unavailable while the factory-reset recovery intent
   is pending.
   Retry is idempotent: it continues any remaining deletion while preserving
   the truthful outcome reported for each storage location.

### Reinitialize Memory

When Memory state is corrupt beyond rebuild, use **Reinitialize Memory** beside Repair on the Memory Runtime card in Settings > Dependencies. The action remains visible but unavailable until the pinned `memory-runtime` artifact is installed and ready; repair that artifact first when it is invalid. The confirmation pauses for five seconds and explains that it will delete local Memory data and related operational state before attempting to start a brand-new Memory engine. It also warns that startup can fail after deletion and the old data will not be restored. The mixed-purpose storage locations are labeled **Primary Memory storage** and **Memory state storage**; their technical paths, `<effective_home>/memory` and `<effective_home>/state/memory`, remain available as secondary details.

Reinitialize Memory keeps Memory settings, credentials, and the installed artifact. The request waits for its final result and reports the two storage locations independently, so a partial deletion is shown as partial rather than claimed as a clean success. A durable internal `factory_reset` recovery intent makes retry idempotent after a crash; while that intent is pending, other Memory controls remain disabled and the action is labeled **Retry initialization**. Reinitialize Memory is not a secure wipe and does not remove original chats or copies already sent to remote endpoints.

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

Processing Record and Provider Call Log remain independent, authorization-scoped,
best-effort diagnostics. Missing evidence is reported as unavailable and never
widens access.

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
