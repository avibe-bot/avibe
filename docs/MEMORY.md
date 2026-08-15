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

## Planned Agent Memory beta

This track remains unavailable until the #1424 implementation slices land. The
contract below records the behavior those slices must deliver.

Agent Memory is a separate, off-by-default track for completed Agent Turns. When
the owner explicitly enables it and binds a source workdir to `default` or a
named Memory project, Avibe asynchronously sends the exact Agent dispatch text
and successful final result to a dedicated EverOS agent-mode root. Interactive
and Harness turns use the same admission rule. Failed, canceled, silent,
legacy-Agent, oversized, malformed, callback, maintenance, and unbound turns are
skipped without delaying or changing the Turn result.

Each enable, Clear, and Reinitialize begins at the current completed-Turn high
water. Turns completed before first enable, during a later disabled interval, or
before a destructive reset are not backfilled. The owner's workdir/project
bindings remain in Memory settings across Clear and Reinitialize; the reset
runtime derives fresh opaque internal keys from them.

The existing Personal Memory root remains pinned to chat mode. Agent Memory has
its own provider root, socket, lifecycle/health slot, scanner, and queue. An
ordinary agent-track scanning, processing, retrieval, or reconcile failure
cannot alter user capture or Personal Memory processing. Explicit Clear,
Reinitialize, and embedding-identity rebuild operations intentionally share a
maintenance fence and can pause both roles until the operation converges.
The Agent queue independently caps nonterminal work at 500 rows and requires at
least 512 MiB free before admission; guarded turns are counted without retaining
their text.

Agent cases and skills are available only through explicit, scoped Agent Memory
search/list operations in the CLI or owner Settings UI. Returned skill content
is untrusted text: Avibe does not install or execute it and never injects it into
an Agent prompt. Each displayed skill includes its last-updated timestamp and
maturity score. Memory status reports a non-blocking per-Agent skill-count hint:
8-10 is approaching the upstream prompt limit and more than 10 is the risk zone.
A successful add/flush means the trajectory was processed, not that EverOS
necessarily produced a case or skill.

EverOS 1.2.3 may produce zero output through quality gates. Skill retirement is
not implemented; sanitized skill-name collisions can overwrite a prior file;
and clusters above ten indexed skills retain an upstream stale-index clobber
risk. The track is isolated so these accepted limitations cannot corrupt
Personal Memory. Freshness/maturity metadata and the count hint help the owner
judge possible staleness but do not repair, rename, retire, or rewrite skills.
See
[`memory-agent-track-1424.md`](plans/memory-agent-track-1424.md) for the complete
contract.

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

The third processing endpoint in **Settings > Memory** is optional. Configure
its Base URL, model, and API key together to let the pinned Memory runtime use
its reranking capability; changing a configured endpoint is admitted by the
same bounded preflight before it is saved. Leave all three fields empty to keep
the standard Memory search tier. Removing the saved reranking endpoint clears
all three values and does not rebuild the embedding index.

## Processing Record

The Processing Record combines a direct, bounded EverOS health projection with
source availability, confirmed pipeline anomalies, clear recovery, and the
existing processing timeline. It has an explicit Refresh action rather than a
composite status poll. A section may be marked stale or unavailable while the
other independently sourced sections continue to render, including while
Memory is disabled.

The timeline is the installation operator's view across every valid project and
user. Each row and its detail view visibly includes the full **Project ID** and
**User ID**, followed by the path from message capture through memcell creation,
processing strategies, profile triggers, and current indexing state. The UI
routes grant this broad read scope to an authenticated local or Avibe Cloud
Memory session; profile and search reads remain user-scoped.

Provider calls are attached only when Avibe can prove their exact provenance to
the displayed project and user. The broad UI view does not weaken those joins:
foreign, malformed, or multi-user memcells are omitted and a detail request
derives the row's real scope before reading runs, capture, or calls.

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
removes the Personal and Agent Memory queues, the owned Personal root and any
existing owned Agent root, role-owned call logs, and pinned attachments through
their idempotent deletion primitives. A never-created Agent root is a safe no-op.
Clear is irreversible; it does not delete
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

When Memory state is corrupt beyond rebuild, use **Reinitialize Memory** beside Repair on the Memory Runtime card in Settings > Dependencies. The action remains visible but unavailable until the pinned `memory-runtime` artifact is installed and ready; repair that artifact first when it is invalid. The confirmation pauses for five seconds and explains that it will delete local Memory data and related operational state, including both Personal and Agent Memory roots, before attempting to start brand-new enabled Memory engines. It also warns that startup can fail after deletion and the old data will not be restored. The mixed-purpose storage locations are labeled **Primary Memory storage** and **Memory state storage**; their technical paths, `<effective_home>/memory` and `<effective_home>/state/memory`, remain available as secondary details.

Reinitialize Memory keeps Memory settings, credentials, and the installed artifact. The request waits for its final result and reports the two storage locations independently, so a partial deletion is shown as partial rather than claimed as a clean success. A durable internal `factory_reset` recovery intent makes retry idempotent after a crash; while that intent is pending, other Memory controls remain disabled and the action is labeled **Retry initialization**. Reinitialize Memory is not a secure wipe and does not remove original chats or copies already sent to remote endpoints.

## Capture delivery and flush coordination

Each captured row stores the canonical provider session reference
`(principal_id, epoch, project_ref, session_id)` alongside the durable capture
queue state. Duplicate source identities remain idempotent within the active
epoch, and a claimed row is either settled successfully or retained with its
payload for recovery.

The outbox owns add delivery only. A session-scoped coordinator separately owns
generations, idle/max-age/message-count flush thresholds, exact fences, and
immutable settlements. Natural extraction boundaries settle a generation
without an extra flush. Unknown post-submission outcomes become
`manual_required` and are never replayed automatically; unrelated sessions keep
progressing. **Clear Memory Data** is the operator's terminal disposition for
that retained local evidence; after Clear completes, later captures use the new
epoch and can deliver normally.

Workbench attachments are copied into a private durable bundle before capture
is accepted. Queue payloads store only bundle-relative metadata. Confirmed
delivery or deterministic rejection releases the bundle; pending and
`manual_required` captures retain it for recovery.

The Agent track raises the Avibe-owned Memory store from released schema v3 to
schema v4 through one transactional migration. Existing v3 rows, tables,
indexes, values, and project-catalog entries remain unchanged, and fixtures
cover that shipped shape. A malformed optional Agent-track section degrades by
disabling that track with a sanitized warning; it never makes startup fail or
resets Personal Memory state.

An incomplete, malformed, overlong-receipt, timed-out, or response-disconnected
provider add is terminal for automatic delivery: the row is retained and its
provider session is fenced as `manual_required`. Boot recovery applies the same
fence to any abandoned `processing` row, so it never silently replays an add
whose provider outcome is unknown.

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
