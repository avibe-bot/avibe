# Bounded Vibey runtime transcript loading

## Contract

A run loads the latest checkpoint's live context and the older rows it still
needs, rather than deserializing the Session's lifetime at each startup. The
model's messages and system-prefix policy, context-budget anchor, pinned inputs,
checkpoint memo/skills, hook state, replay refusals, and pending-call recovery
remain unchanged. Historical `load()`, fork-point resolution, and read-only
request previews retain full-history semantics.

The runtime bound is **active suffix + necessary dependency closure + live
scalar facts**, not a fixed number of rows. An uncompacted Session still needs
its full context. A checkpoint-v4 memo needs older inputs until its first v5
consolidation. An anchor before the first checkpoint can require its original
large request prefix until a newer eligible response replaces it.

## Derived checkpoint metadata

`SQLiteTranscriptStore.load_for_run()` returns an explicit `RunTranscript`:
canonical retained entries, maximum response-end timestamps per provider/model,
latest measured response cache records, and context-change sequence facts.
Request-start audit clocks are not response-end clocks; own audit snapshots are
merged only by the running Session and are never inherited in a fork capsule.

The storage reader finds the latest checkpoint through
`ix_agent_events_session_type_context_seq`. It uses one read transaction across
both canonical tables and the effective, inclusively capped fork ancestry.

A versioned `vibey_runtime` object in the checkpoint's extensible `metadata_json`
certifies its immutable prefix. It contains original row references and scalar
facts, not copied message bodies. Its checksum binds the checkpoint identity,
canonical checkpoint content, and capped owner ancestry. Missing, malformed,
or unsupported metadata is rebuilt from canonical rows; canonical corruption
still fails validation. A short compare-and-swap publication preserves unrelated
metadata and cannot overwrite a concurrent repair. Canonical `content_json` and
all model-visible checkpoint text remain untouched.

Released checkpoints pay a one-time full-prefix certification cost. Subsequent
loads resolve retained references by primary key and read the post-checkpoint
suffix by sequence range. A new checkpoint derives its metadata from its
immediate predecessor's certificate plus the intervening suffix, when available.
This avoids making each new checkpoint another lifetime reload.

## Dependency closure

The pure closure builder retains:

- the checkpoint's `first_kept_seq` window and explicitly pinned inputs;
- the latest hook state and lifetime opaque replay refusals;
- original tool-result bodies/details, chronological relevant edits, and complete
  call/result batches associated by ordered call instance (not reused call ID);
- pending calls even when their owners were summarized;
- inputs the next checkpoint memo is still entitled to offer;
- the latest eligible response's original pre-response context for budget anchors;
- the actual final context row, keeping sequence/snapshot and fork-own-row facts.

Base references and optional old-anchor references are separate. Once an
eligible retained response in the suffix replaces the old anchor, the reader
does not fetch the obsolete anchor prefix. A later checkpoint drops those
references entirely. An unexpected cross-boundary edit triggers canonical
rehydration and repairs the prefix dependency references once, rather than
repeating the same full load on every run. A checkpoint that moves retention or
memo bounds backwards also requires canonical rehydration.

All hot runtime consumers use this boundary: Agent startup, prompt preparation,
input environment restoration, and both adapter/core recovery reads. Read-only
preview avoids publishing even optional metadata.

## Compatibility and validation

Revision `20261112_0084` adds only a non-unique composite index; the compatibility epoch stays
at 2. The index deliberately uses the migration policy's proven additive operation
subset, rather than expanding its allowlist for a partial-index optimization. Older readers ignore the new metadata and retain the canonical transcript.
The migration test covers additive upgrade, structural index rollback in a
hermetic fixture, re-upgrade, and unchanged stored rows. Product rollback remains
selection of a compatible older binary, not an Alembic downgrade.

Pure differential tests compare full and retained projections, budget anchors,
next memo/skill inputs, and recovery. Storage tests cover cold certification,
warm decoder exclusion, incremental new checkpoints, capped nested forks,
metadata damage, repeated dependency repair, cache clocks/ties, and publication
races. Query-plan tests require the checkpoint index for latest and predecessor
seeks, with no history-wide temporary sort. Existing loop, recovery, compaction,
adapter, transcript, and preview suites verify the consuming paths.

Performance acceptance uses a fresh local Incus environment and synthetic large
Session, never the owner's live desktop. Report first certification separately
from warm loads, compare serialized model-context hashes, and distinguish probe
RSS from service RSS. Record load/projection times, resident memory and heartbeat
stalls over two seconds. Functional test results are not a substitute for the
before/after performance run.

### Measured acceptance (2026-11-12)

Fresh local Incus worktree, 2 CPUs / 3 GiB, Python 3.12, no credentials or copied
user data. One streaming-generated Session has 14,153 context rows containing
165,638,858 bytes of canonical JSON. The model sees 152 messages (887,659 serialized
bytes). Three separate processes per baseline/warm group; medians below. Schema
migration is outside the timed load; first prefix certification is reported
separately. These are **probe-process**, not controller-service, RSS figures.

| Metric | Full historical loader | Warm runtime loader |
| --- | ---: | ---: |
| Decoded/returned context rows | 14,153 | 152 |
| Load latency | 479.7 ms (447.3–676.1) | 25.2 ms (24.2–26.6) |
| Subsequent projection latency | 21.4 ms | 3.5 ms |
| RSS after load | 224.1 MB | 72.0 MB |
| RSS added by load | 168.4 MB | 5.2 MB |
| Process peak RSS | 227.3 MB | 74.0 MB |
| Heartbeat stalls >=2 seconds | 0 / 3 probes | 0 / 3 probes |

First certification took 897.1 ms and reached 239.2 MB peak RSS; it is a real
one-time compatibility cost, not included in the warm result. Every successful
baseline, first-certification and warm probe produced the same model-context
SHA-256: `27311216ce6c395b95f579b0964cdb51d488251904e2467d1dba2f9b5570972b`.

Baseline runtime was `4f5538d19`, candidate runtime `62010eae1`. The subsequent
migration-only fix uses the proven additive `create_index` operation and a
non-unique composite index instead of raw partial-index SQL; it changes neither
the loader nor this context-only fixture's indexed entries. Focused migration
and query-plan tests verify the final index.

These short probes establish the load/RSS improvement and context equivalence,
not disappearance of production controller stalls. Neither baseline nor candidate
reproduced a two-second stall; the separate P2 GC observation under normal
workload remains necessary. No owner-desktop synthetic load or service restart
was performed.
