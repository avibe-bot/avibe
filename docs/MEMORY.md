# Memory

Avibe Memory distills eligible Workbench and private-IM messages into a
per-user profile, episodes, and facts. Open **Settings > Memory** to inspect its
status, current profile, search results, settings, and processing log.

## Processing log

The Log tab is the installation operator's view across every valid project and
user. Each row and its detail view visibly includes the full **Project ID** and
**User ID**, followed by the path from message capture through memcell creation,
processing strategies, profile triggers, and current indexing state. The UI log
routes currently grant this broad read scope to an authenticated local or Avibe
Cloud Memory session; profile and search reads remain user-scoped. A section may
be marked unavailable when its source database is missing, busy, malformed, or
its attribution tombstone has expired; the other sections still render.

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

## Recovery and Clear

While Memory is enabled, **Restart engine** replaces only the managed sidecar;
it does not change Memory settings or delete retained data. Use it when the
recorder reports a transient failure. If the call-log database is corrupt,
recording remains degraded across restarts; use **Clear all** to remove the
corrupt owned files before recording can resume.

Clear all removes the dedicated local Memory data, processing queue, indexes,
and retained provider-call diagnostics owned by this installation. It does not
remove original Avibe chats, copies already sent to a provider, user-created
snapshots, general logs, crash reports, backups, or data outside the dedicated
Memory directory. It is not a secure wipe of the storage device.

## Capture outbox foundation

Each captured row stores the canonical provider session reference
`(principal_id, epoch, project_ref, session_id)` alongside the durable capture
queue state. Duplicate source identities remain idempotent within the active
epoch, and a claimed row is either settled successfully or retained with its
payload for recovery.

An incomplete, malformed, or otherwise ambiguous provider add is terminal for
automatic delivery: the row is retained and its provider session is fenced as
`manual_required`. Boot recovery applies the same fence to any abandoned
`processing` row, so it never silently replays an add whose provider outcome is
unknown. The status surface reports an active manual fence as degraded. Flush
coordination, generation routing, and per-generation settlement projection are
owned by the later flush coordinator.
