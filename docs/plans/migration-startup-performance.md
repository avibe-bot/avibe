# Migration startup snapshots and readiness

Historical implementation and measurement record for #3365. The later
[SQLite compatibility and verification contract](../maintenance/sqlite-compatibility.md)
is authoritative: proven additive upgrades may omit snapshots; conservative
COW copies defer physical verification until restore, online-backup copies
retain creation-time verification, and neither live post-migration lifecycle
completion performs a full scan. The original decisions and measurements below
are retained as history, not current verification requirements.

## Original contract (#3365)

Migration-bearing upgrades retain a fresh, independent, durable and verified SQLite rollback point before any schema repair/upgrade. On supported filesystems, avoid rewriting every database page: checkpoint WAL and clone under the migration lock **and SQLite writer exclusion**. MigrationFileLock coordinates migrators, not existing ordinary SQL connections; a busy exclusive SQLite lock must fall back to the online backup API, never silently omit committed WAL pages.

Keep manifest compatibility, retention/expiry (#1578/#2252), incompatible-revision preflight (#3342), restore/downgrade behavior, and backups for all migration kinds unchanged. A failed clone must clean its incomplete copy and use the existing online backup path. Both paths retain quick_check, revision readback, fsync and publication ordering.

Service startup must publish active database preparation on the existing service lock record. Restart/upgrade waits distinguish that phase from a hung ordinary startup, give active preparation a generous hard cap, fail when the owner dies, and report migration progress without declaring readiness before controller startup. No new IPC or migration revision.

## Work

- [x] Add native clonefile (APFS) / FICLONE (Linux reflink) with conservative SQLite locking and fallback.
- [x] Cover committed WAL, snapshot independence, concurrent writers/readers, unsupported/error fallback, verification failure and existing restore/retention contracts.
- [x] Extend existing service startup phase and readiness wait; cover successful long migration, dead owner, stale record, hard cap and ordinary startup timeout.
- [x] Measure before/after on a synthetic multi-GB database; inventory remaining full scans; fix only small redundant work without weakening validation.
- [ ] PR, current-head Codex review and required CI; report Merge-ready to orchestrator (do not enqueue/merge).

## Validation notes

Use test-owned state only; never read the owner's database or restart the active service. Benchmark data/output belongs in workspace `_tmp/`. No private machine paths or incident logs belong in the PR.

## Implementation and operational behavior

- APFS uses native `clonefile`; Linux uses `FICLONE` in a short-lived, isolated Python child. A raw file descriptor opened/closed in the SQLite process can release **all of that process's POSIX locks on the database inode**. The child avoids that hazard, including when other local SQLite connections already exist. No new package dependency or persistent process.
- SQLite exclusive locking spans a completed transaction, WAL `TRUNCATE` checkpoint and clone. Busy readers/writers, unsupported filesystems, cross-device destinations and clone errors use SQLite online backup. The normal migration file lock remains held across verification/publication.
- The existing service lock record carries `migrating` while backup, migration, import and validation run. A matching live process start token enables the longer wait. No heartbeat thread or new IPC. Ordinary startup retains its 120-second budget; observed database preparation pauses it, with a 30-minute hard cap on the readiness wait. Finishing preparation returns to `starting`, not `running`; only controller readiness publishes success.
- Restart/upgrade remain asynchronous. Their output points to `vibe status`, which reports migration progress; restart-job status/logs receive the same progress and do not fail just because 120 seconds elapsed in preparation.
- Service preparation omits eight unused diagnostic row counts on already-imported state. It does **not** replace repair with a receipt-only probe or skip the service's own exact-attempt validation.

## Measurements (2026-11-07 UTC)

Hermetic synthetic data, macOS arm64 / Python 3.13 / SQLite 3.51.2 / APFS on an external SSD. The snapshot fixture is **3,272,536,064 bytes**: current Avibe schema plus 98,304 rows of random 32-KiB BLOBs, with ordinary 4-KiB database pages. No live user data. These are elapsed wall times, not a claim about end-to-end service downtime.

| Measurement | Before / online backup | Clone path |
| --- | ---: | ---: |
| Complete backup, first two runs | 10.77 / 11.01 s | 10.23 / 10.23 s |
| Later paired complete backup | 9.15 s | 9.85 s |
| Copy/checkpoint component of paired run | 2.510 s | 0.004 s |
| Destination verification component | 6.628 s | 9.803 s |

The clone itself is consistently **4–7 ms**, but the required `quick_check` on the new inode remains database-sized and cache-sensitive: **about 3.0 seconds per decimal GB** on this fresh-clone workload. This workload **does not establish a material overall backup-latency improvement**, despite avoiding the full data copy and its disk allocation. The backup remains a real independent, full-logical-size file sharing COW extents; later divergence consumes storage, and fallback still allocates a full copy. Do not advertise size-independent/few-second upgrades from these results. Native clone independence and restoration were also tested on actual APFS.

Startup costs and decisions at #3365 (superseded where noted above):

| Work | Measured cost / scope | Decision at #3365 |
| --- | --- | --- |
| Backup `quick_check` | ~9.8 s on a fresh 3.27-GB clone | Retained unchanged. |
| Post-Alembic live validation | 2.35 s on that fixture | Retained: gates `migrated`. |
| Post-import/data-work live validation | 0.39 s in the subsequent warm-cache pass | Retained: distinct mutation boundary, gates `validated`. |
| Standalone source `quick_check` / `integrity_check` | 0.53 / 0.54 s warm in initial run | Full `integrity_check` is initial/missing-background import only, not ordinary restarts. |
| Eight diagnostic counts | 2.63 ms for 1,000,000 terminal runs (666-MB separate fixture); repair 23.02 ms with counts vs 18.64 ms without | Small fix: omit unused counts on service startup; preserve diagnostic defaults. |
| Unchanged-schema repair | 34.94 ms on the 3.27-GB payload fixture before the change | No unconditional whole-DB integrity scan on an ordinary already-ready restart. |

Migration index builds remain data-sized and version-specific (0073–0075 create run/definition indexes). Conditional legacy schema repair/backfills and initial JSON import also traverse data. Revision/schema preflight, lifecycle receipts, readiness and expiry eligibility themselves inspect metadata rather than the whole database.

A read-only controller audit also found duplicate session-state loads, a history scan in legacy ownerless-status reset, and historical message work during Vibey open-tail recovery. Those were not benchmarked or changed here: they are separate query/recovery optimizations, not grounds to weaken rollback validation. Moving validation outside downtime would require a separate lifecycle/consistency design.

## Investigating the reported 111-second backup phase

The reported startup log bounds the pre-migration phase at **110.81 s for an approximately 4.5-GB database**, but the old log combines copying, destination verification, sync/publication and pruning; it cannot identify which dominated. The installed call is `source.backup(destination)` with Python's default `pages=-1`: one all-pages `sqlite3_backup_step`, **not** an incremental backup that repeatedly restarts after commits between steps. A controlled 3.27-GB WAL test using the installed Python 3.15.1 / SQLite 3.53.1 runtime completed the all-pages copy in **2.27 s with 275 concurrent commits, one DONE progress callback and zero restarts**. Deliberately switching the control to 256-page steps produced **261 observed restarts, 6,713 callbacks and 8.45 s** with 262 commits. Python 3.13 reproduced the same distinction. Thus repeated incremental-copy restarts are not evidence for this incident; an initial BUSY/LOCKED wait remains possible but was not measured. The reported volume was 91% used, whereas the synthetic fixture used a different APFS volume with ample space and different row/index/cache layout. Historical telemetry cannot separate disk-pressure cost from `quick_check` cost, and no disk-filling experiment or live-database access was performed. This PR logs copy and verification times separately so the next upgrade can establish that split rather than infer it.

Primary references: SQLite's Online Backup API documentation (`backup.html`, all-pages versus incremental examples) and CPython 3.15.1 `Modules/_sqlite/connection.c` (`pysqlite_connection_backup_impl`, default `pages=-1`). The reproduction used a two-second external writer updating one committed WAL row every 5 ms; reset counts are increases in `sqlite3_backup_remaining` between progress callbacks, not guesses from elapsed time.

## Historical validation-placement options

- Optimize verification I/O while retaining the identical synchronous SQLite check; compare cold-cache behavior, not only copy time.
- Pre-stage and verify a snapshot before downtime, then fence writers and capture every intervening commit; this needs a new consistency design, not reuse of a stale snapshot.
- Defer post-migration checks until after startup while retaining rollback/expiry fencing; this changes readiness and failure semantics and requires an explicit separate decision.

## Verification

- Focused backup, preflight, bootstrap contention, service-lock, restart and upgrade tests: 447 passed, 3 platform skips, 4 subtests passed.
- Additional latest locking/readiness/preflight/expiry/migration-lock pass: 102 passed.
- Native APFS clone/restore, committed-WAL preservation, ordinary-writer exclusion, live-reader fallback, partial-copy errors and failed verification are covered at the backup boundary.
- Slow-migration timeout and both WAL/non-WAL fast-copy regression cases were run against the original implementation: all three fail for the intended behavior (no clone attempted; legitimate migration timed out), and pass with the change.
- Scoped-launch/readiness fixture updates: 41 passed. Independent read-only review found only these fixture changes; no further actionable defect.
- Ruff changed-file checks and `git diff --check` passed. CI/review remain PR gates. No live service restart or regression deployment was performed.
