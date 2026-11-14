# Epoch-3 upgrade and growth measurements

This is an acceptance procedure, **not a performance result**. The coordinated
writer plan requires measurements before release; small unit tests and the
published-predecessor compatibility probe do not replace this run.

## Environment and provenance

Use the repository regression runner to provision an isolated **local Incus
worktree target**, with no owner data, credentials or live service traffic.
The exercise owner must approve its fixed data-filesystem quota, inspect host-pool and
guest capacity before/after, hold the target for the entire run, and delete the
target afterwards. Never generate a multi-GB fixture or fill disk on the desktop
or shared storage pool. Never reset an existing regression target.

Install the actual published 3.2.6 wheel into an isolated interpreter. Verify
its published SHA256 (the wheel used for the reader proof was
`59fa0184ecfd97fffa77dc74c99b2560284c0a9fad4cd2fccdb5616a09569197`) and retain that
artifact/installation evidence with the report. The harness also verifies
installed version and import path; those checks alone do not attest an arbitrary
locally modified installation. The candidate checkout must be clean at the exact
reviewed commit. The runner's source sync intentionally omits `.git`; use a
separate shallow checkout of that exact commit inside the disposable guest and
reuse its dependency interpreter, rather than fabricating Git metadata or
weakening the clean-head check. No source substitution into the predecessor is allowed.

`scripts/sqlite_epoch3_measure.py` refuses large work outside a Linux Incus
container or when guest filesystem capacity does not reflect the provisioned
12–16 GiB quota on the filesystem containing the test root. A bounded data
volume attached at the test state directory also satisfies this requirement;
container-root quota labels without an enforced filesystem limit do not. If the
guest reports shared-pool capacity instead, stop and fix
provisioning with the exercise owner; do not bypass the guard or fill the pool.
The driver neither changes quotas nor starts/stops services. Every worker uses
an isolated HOME/Avibe/XDG/temp tree and only synthetic state.

## Run four phases

In the pinned candidate checkout inside the approved guest, invoke:

```bash
python -m scripts.sqlite_epoch3_measure \
  --root "$NEW_TEST_ROOT" --previous-python "$PUBLISHED_326_PYTHON" \
  --quota-bytes "$VERIFIED_ROOT_QUOTA_BYTES" --phase prepare
```

Repeat the same command and arguments with `--phase refusal`, `--phase upgrade`
and `--phase growth`, in that order. Defaults are 4,500,000,000 logical database
bytes and 1,000 operations per writer group. Use managed Watches or another
lead-owned re-checkable execution path, not a detached shell. Each phase writes
started/completed markers, logs and JSON receipts. Intent markers are fsynced
before mutation; results are atomically published and fsynced before completion.
Every worker also checks the existing DB/WAL/journal size, so a small requested
workload cannot bypass the large-fixture guest guard. A started phase cannot be
replayed: inspect its evidence and use a fresh isolated exercise after an
interruption. The script never silently resumes partially written workloads.

1. **Prepare:** the real predecessor creates the schema, Session, conversations,
   cache writes, Vault audit rows and Skill observations. Bulk synthetic message
   rows clone a real API-written message shape until the main database exceeds
   4.5 GB. This tests upgrade of populated history, not message ingestion speed.
   A streamed history digest and row counts provide preservation evidence.
2. **Refusal:** an exclusively created test-owned filler brings guest filesystem
   usage to approximately 91%. Record actual available bytes, not just percent.
   Candidate admission must refuse with required reserve and actionable deficit;
   DB/WAL/backup/lifecycle hashes must remain unchanged. The filler is removed in
   `finally`, and no other file is deleted. A conservative full-copy reserve is
   expected to refuse at this occupancy; do not describe it as a successful
   near-full upgrade.
3. **Upgrade:** with the filler removed, run the candidate's real migration and
   rollback-backup path. Record total seconds, backup seconds, before/after bytes
   and a 200 ms free-space series high-water observation. Verify history digest,
   row counts and quick-check. The actual predecessor reads the upgraded history;
   a separate small extended contract exercises old writes/clear/ACL and candidate
   return. Compatibility rollback means selecting the supported reader binary,
   never restamping or Alembic downgrade.
4. **Growth:** each group gets two independent predecessor-created databases.
   Only the candidate half is upgraded. Run identical API workloads: 1,000 cache
   turns across four provider/model states with 32 valid cumulative prefix
   boundaries (the producer's bound), 1,000 proxied requests sharing
   one full historical access snapshot, and 1,000 distinct receipts for one
   25-entry Skill catalog page. Report logical file growth, used-page growth and
   per-table/index pages before/after, including reference/state table overhead.
   These are specified synthetic workloads, not a claim about every user's daily
   workload. No writer is dropped on assumed low benefit.

## Report and release gate

Report the exact candidate, predecessor wheel identity, quota/pool evidence,
fixture logical and allocated bytes, row counts, durations in minutes, available
space before/at the sampled minimum, conservative reserve and refusal deficit
in GB, and per-group bytes per 1,000 operations. Keep these distinctions explicit:

- **Conservative required reserve:** policy used for admission, not measured peak.
- **Sampled peak additional usage:** baseline free minus minimum sampled free;
  includes incidental guest writes and can miss a spike between samples.
- **Minimum observed free:** the actual free-space low-water reading during this
  successful run, not a proof of the minimum capacity for all upgrades.

Retain success/failure and cleanup receipts outside the disposable target before
the exercise owner deletes it. Product evaluates the measured benefit and gives
its go on the exact head; the lead owns merge and release. This procedure does
not authorize any deployment, live database access or service restart.

### Release-note draft (fill measured values before publication)

Normal cache behavior, Skill use and Vault access/audit history remain unchanged.
Future repeated state consumes less database space; existing history is retained
and the database is not vacuumed. The first upgrade needs a rollback copy and
migration workspace; insufficient space is rejected with the additional GB to
free. Publish the measured 4.5 GB upgrade/backup duration and conservative free-
space reserve from the approved run here—do not invent a universal downtime
promise. Returning to the verified 3.2.6 reader preserves current data. Unsupported
older readers refuse the new epoch; recovery to them requires an explicitly
verified backup restore with all writers stopped, which discards later writes.
