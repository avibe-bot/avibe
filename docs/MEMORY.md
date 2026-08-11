# Memory

Avibe Memory distills eligible Workbench and private-IM messages into a
per-user profile, episodes, and facts. Open **Settings > Memory** to inspect its
Processing Record, current profile, search results, and settings.

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

Avibe retains bounded, scrubbed LLM, multimodal-LLM, and embedding request and
response fields used during Memory processing. It does not store embedding
vectors, attachment bytes, raw sidecar stdout/stderr, or unrelated Search and
Get calls. Secrets, configured provider URLs, and absolute local paths are
scrubbed, and large fields are truncated or replaced with omission markers.
Diagnostic payloads can still contain sensitive conversation text; treat access
to the operator log accordingly.

Rows remain readable until normal expiry or Clear all. Retention runs while
Memory is disabled as well: rows expire after 14 days and only the newest 5,000
calls are kept.

## Recovery ladder

Use these actions in order, from least to most destructive:

1. **Restart engine**: Use it for a temporary recorder or engine failure.
   It is available while Memory is enabled and no other Memory maintenance
   action is running. It restarts the Memory engine without changing settings,
   rebuilding indexes, or deleting retained data.
2. **Repair index**: Use it when restarting does not clear index health
   warnings or pending work. It appears under **Processing Record > Runtime and
   capabilities** only when the installed Memory Runtime supports repair, and
   it can remain available when the current health snapshot is unavailable.
   Repair rescans Markdown memory and drains pending work while keeping the
   engine available; it preserves the existing index and may use Embedding API
   quota. **Memory index repair completed with health warnings.** means the
   repair finished but the returned health is still unhealthy. Address the
   reported condition, then select **Repair index** again; a failed repair can
   be retried the same way.
3. **Rebuild index**: Use it after changing the Embedding endpoint or model,
   or to recover a pending rebuild. Confirming **Save and rebuild** saves the
   new settings before rebuilding the local vector index and preserves Markdown
   memory. If rebuilding fails, the confirmed change remains saved, the rebuild
   warning remains visible, and **Restart engine** stays unavailable. Correct
   the endpoint or API key as needed, then select **Retry rebuild**.
4. **Factory reset**: Use it only as a last resort when the earlier actions
   cannot recover Memory. It is available under **Settings > Memory** only when
   the pinned, installed Memory artifact is valid and no other Memory action is
   running. It permanently deletes exactly the installed Memory root
   (`memory`) and the mutable Memory state root (`state/memory`), then starts
   fresh Memory state. It preserves Memory settings and credentials, the
   pinned, installed Memory artifact, original Avibe chats, and data outside
   those two roots. If a reset only partly succeeds, review the per-root result
   and select **Retry factory reset**.

### Factory reset

When Memory state is corrupt beyond rebuild, use **Factory reset** in Settings > Memory. The action is available only while the pinned `memory-runtime` artifact is installed and ready; repair that artifact from Settings > Dependencies first when it is invalid. The confirmation pauses for five seconds and names the exact two mutable roots that will be removed: `<effective_home>/memory` and `<effective_home>/state/memory`.

Factory reset keeps Memory settings, credentials, and the installed artifact. The request waits for its final result and reports each root independently, so a partial deletion is shown as partial rather than claimed as a clean success. A durable `factory_reset` recovery intent makes Retry idempotent after a crash; while that intent is pending, other Memory controls remain disabled and the action is labeled **Retry factory reset**. Factory reset is not a secure wipe and does not remove original chats or copies already sent to remote endpoints.

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
progressing.

Workbench attachments are copied into a private durable bundle before capture
is accepted. Queue payloads store only bundle-relative metadata. Confirmed
delivery or deterministic rejection releases the bundle; pending and
`manual_required` captures retain it for recovery.

This implementation initializes a clean Avibe-owned schema; migration or
preservation of earlier Memory databases is intentionally unsupported while the
feature remains unused.

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

EverOS 1.2.3 does not enforce model-call and token ceilings, so agentic recall is
currently reported as unavailable even when its required budgets are supplied.
Current-session overlay can only use the trusted caller session supplied by the
runtime; callers cannot provide arbitrary provider filters or session IDs.
