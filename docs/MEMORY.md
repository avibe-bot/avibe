# Memory

Avibe Memory distills eligible Workbench and private-IM messages into a
per-user profile, episodes, and facts. Open **Settings > Memory** to inspect its
status, current profile, search results, settings, and processing log.

## Processing log

The Log tab shows the owner-scoped path from message capture through memcell
creation, processing strategies, profile triggers, and current indexing state.
This timeline remains available when provider payload logging is off. A section
may be marked unavailable when its source database is missing, busy, malformed,
or its attribution tombstone has expired; the other sections still render.

Provider calls are attached only when Avibe can prove that the call belongs to
the signed-in principal. An authenticated Avibe Cloud session may read that
principal's own timeline and retained calls, but cannot read another user's
rows.

## Provider payload logging

Provider payload logging is an optional installation-wide diagnostic setting
and is off by default. Only an administrator connected directly to the Avibe
installation can enable or disable it. Enabling it affects every principal who
uses that installation, so review the disclosure in Settings before turning it
on.

When enabled, Avibe retains bounded, scrubbed LLM, multimodal-LLM, and embedding
request and response fields used during Memory processing. It does not store
embedding vectors, attachment bytes, raw sidecar stdout/stderr, or unrelated
Search and Get calls. Secrets, configured provider URLs, and absolute local
paths are scrubbed, and large fields are truncated or replaced with omission
markers. Diagnostic payloads can still contain sensitive conversation text;
treat access to them accordingly.

Turning payload logging off stops new rows but does not delete retained rows.
They remain readable until normal expiry or Clear all. Retention continues while
Memory or diagnostic capture is disabled: rows expire after 14 days and only the
newest 5,000 calls are kept.

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
