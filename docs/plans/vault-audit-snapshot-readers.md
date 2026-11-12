# Vault audit snapshot references: reader-first

## Change contract

This is the reader-only preparation for snapshot deduplication. It does **not**
roll up audit events or reduce write volume yet. Every proxied request retains
its own row, ID, exact timestamp, secret name, requester, delivery summary
(including host, method, status and field), request ID and grant ID. All existing
rows and every writer, including every non-proxied event, remain unchanged.

The additive migration creates only an empty independent
`vault_audit_access_snapshots` table:

- `id`: SQLite integer primary key, used as a compact reference.
- `snapshot`: non-null, unique text containing the complete immutable version-1
  access snapshot, including every resource identity and historical ACL.

No `vault_audit` column changes, history scans, backfills, rewrites, deletion,
retention changes, vacuum or runtime deduplication writer are included.

## Reader and authorization boundary

The private `delivery._resource_access_snapshot` value may be either:

1. The existing inline `{ "version": 1, "resources": [...] }` snapshot.
2. Exactly `{ "ref": N }`, where `N` is a positive signed-64-bit integer (not a
   boolean, floating point value or string), identifying one snapshot row.

The ACL-filtered audit reader resolves distinct references once per existing
keyset batch (at most 200 rows), using a primary-key lookup. It evaluates the
same historical snapshot policy as inline history. Later ACL edits, deletion
and recreation of a secret name cannot replace that evidence. Missing IDs,
malformed references, malformed payloads and reference chains fail closed;
they never fall back to a current ACL. The snapshot envelope requires a JSON
integer `version: 1` (not a boolean or floating-point value). A resolved target
must not contain `ref`, even alongside an otherwise valid inline envelope.
The deletion-tombstone reader uses the same decoder. Inline-only batches do not
touch the new table. These checks do not rewrite valid inline history, change
historical resource-policy evaluation, or alter the administrator fast path.

The existing Owner and validated remote Editor fast path remains unchanged:
it returns the complete event stream without using resource ACLs. Both paths
strip the private snapshot field before returning audit events. No reference or
normalized ACL payload appears in the Vault API/UI. `vibe data query` denies the
new internal table, including joins, subqueries and views; existing raw
`vault_audit` queries remain unchanged. There is currently no `vibe vault audit`
subcommand; no new command or UI redesign is part of this change.

## Compatibility and rollout

All production writes remain byte-identical and inline. The new table remains
empty in ordinary operation. A supported older binary can open this additive
same-epoch schema and continue reading/writing its unchanged audit rows. Select
that binary without Alembic downgrade or restamping, per
[SQLite compatibility](../maintenance/sqlite-compatibility.md). The migration's
structural downgrade drops only the new table, but is not the supported product
rollback procedure.

The supported reader epochs now include the coordinated future epoch 3, while
the current compatibility epoch and all production writes remain unchanged.
This reader bridge must not enable references solely because the table exists:
the later coordinated release owns activation of snapshot-reference writes and
its compatibility boundary. Older binaries that do not understand references
must never be used on activated epoch-3 state. That writer must store an
immutable snapshot and its audit reference in one transaction, never
overwrite/reuse an ID, and preserve all per-request facts.

## Validation

`tests/test_vault_audit_snapshots.py` owns the new contract:

- Mixed inline/reference history produces identical public event fields through
  the real audit API, including after deletion/recreation and ACL changes.
- Missing/malformed references and corrupt payloads deny filtered readers while
  retaining the existing administrator stream and redaction.
- Repeated references use one bounded, deduplicated primary-key query per batch;
  the query plan is pinned and Owner reads require no snapshot lookup.
- Exact inline audit bytes are unchanged for proxied and non-proxied writers,
  with and without the new table; ordinary writes never populate it.
- Upgrade preserves existing audit bytes and epoch, creates no snapshot data,
  permits old named-column writes and is mechanically classified additive.

The existing Vault service, ACL, schema and query-denylist tests remain in force.
All local validation uses synthetic test-owned SQLite state. No live Vault,
credentials, service restart, Incus operation or load test is needed. The
serialized Incus regression/API/UI smoke remains with the lead after approval.
