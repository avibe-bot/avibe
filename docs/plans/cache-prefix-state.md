# Cache prefix storage: reader-first bridge

## Change contract (3.2.6)

Revision `20261112_0085` adds the independent `session_cache_state` table:
`(session_id, provider, model)` is its primary key; `record_json` contains one
version-1 cache record, including its bounded prefix fingerprint. The existing
record's `started_at` orders state against legacy observations. The table has no
foreign key, trigger, backfill, or mutation of existing rows. It is additive in
compatibility epoch 2; the separate 3.2.6 reader bridge accepts a future epoch-3
marker without enabling epoch-3 writers.

**This is a reader bridge, not the growth-reduction activation.** All production
response and audit writers remain unchanged, including inline `cache.prefix`,
`cache_state`, and nested side-turn `cache_attempts`. The new table stays empty
under this release's writers. Historical data is never rewritten or vacuumed.
The approximately 16 MB/day saving estimated in the investigation belongs to
the later writer switch, not this PR.

The SQLite transcript reader merges:

- the latest legacy `model_attempt` and `fork_turn` snapshots and observations,
  using the existing two Session/type index seeks;
- the Session's state rows, using a primary-key prefix search;
- response cache observations already loaded with the transcript, via
  `CacheTracker.restore` in the existing loop.

Only records with reported input usage advance the clock. The newest
`started_at` wins per served provider/model; at an equal clock a full prefix
wins over a prefixless observation. State JSON must match its provider/model
key. Malformed optional JSON/records are ignored rather than failing a Turn.
The read runs on the existing worker, without an audit-history scan or sort.

A valid prefixless observation is retained as a clock and supersedes older
inline/state prefixes. Without a matching full state record, the next comparison
uses `expected_read_basis="missing_prefix"` and zero reusable/expected prefix
tokens; it must not invent a cache hit or provider miss. Request-start timing,
reported previous input, route changes, and explicit context-change facts remain
available. Derived historical diagnostic views do not require the prefix and
retain the same values. This does not affect C-9's response-end clearing clock,
canonical usage, model context, or conversation replay.

## Forks, rollback, and the activation boundary

State is read only for the requested Session. A historical fork inherits cache
observations from its immutable context prefix, never the parent's mutable
future state. If its latest inherited observation is prefixless, its first
comparison explicitly has a missing-prefix baseline. A child's own full state
restores normal comparisons thereafter. Older inline fork histories remain
readable as before.

An intervening legacy writer may leave the new table stale. Its newer inline
response/audit observation wins over that table on return to this reader.

**Pre-bridge binaries do not reliably reset a missing baseline.** Their tracker
skips prefixless records and can revive a stale pre-upgrade inline prefix. Until
the first billed request on each route, that can misclassify new diagnostics
(e.g. tool changes or provider misses). It does not corrupt conversation data or
change clearing behavior, but is not an acceptable rollback claim. Therefore
3.2.6 keeps all old writer bytes and epoch-2 production behavior unchanged,
while its coordinated reader bridge accepts a future epoch-3 database only after
all three representation readers are present. A coordinated 3.2.7 epoch-3
activation, owned by the integration lane, still owns the writer switch across
cache, Vault audit, and Skill catalog consumers.

The future cache writer must atomically upsert latest usage-bearing state with
each response/attempt/side-turn commit, never overwrite newer state with older
ledger attempts, and leave derived facts inline. It also owns state teardown.
No production table-writing API or suppression flag is introduced here.

## Evidence and remaining check

`tests/test_session_cache_state.py` exercises new and mixed representations via
the real SQLite store and fresh trackers: provider/model switches, stale state
following an old writer, prefixless supersession, attempts and side turns,
historical forks, malformed state, derived-metric equality, and query-plan
bounds. A fixed pre-bridge response/audit shape asserts byte-identical writes;
upgrade/reopen assertions preserve populated legacy rows and leave the state
table empty at epoch 2. Existing tracker, transcript-contract, CLI/report,
compatibility, and no-copy suites remain applicable.

The shared historical bridge fault fixture omits only the marker-only epoch-2
transition while retaining later additive schema, so current required tables
exist in both parent and crash-test subprocesses. This changes test fixtures,
not production rollback behavior.

No live database, secrets, service, or Incus environment is accessed. Once the
lead releases the serialized local Incus slot, verify an ordinary Vibey reply,
a process restart, a provider/model round trip, and a Session fork; confirm
legacy prefix writes persist and `session_cache_state` remains empty. No load or
stress test is required for this reader-only bridge.
