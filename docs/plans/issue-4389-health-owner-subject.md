# Health owner identity and diagnostics (#4389)

An owner changing their email must retain their Health connection and data. An
account acquiring the same email must not inherit that connection. Health now
uses the exact `access.owner.user_id` from the live paired Cloud permissions
response; email stays as provenance. Instance kind and sharing eligibility,
metrics, retention, consent expansion and upload contracts remain unchanged.

## Identity and migration

The private store advances to schema 2 and adds nullable `owner_subject` to
devices, pairings and requests. Released schema-1 binaries refuse the database
at their existing version check. There is no downgrade migration. Upgrade all
runtime writers together; an old binary must not be used with the upgraded store.

A legacy enrollment is promoted only by a trusted registry link for its exact
Health device and current pairing binding. The link must have the live owner
subject, an attributed device, a link timestamp and no pending cleanup. Related
requests inherit that device proof; pending initial requests and pairing codes
can instead use their exact registry code proof. Matching email is never proof.
All live records must be proven before any promotion or automatic retirement;
changes to all three tables share one transaction. Unknown, mismatched or
unlinked evidence pauses access and retains data. Recovery requires the user's
agreement to delete retained data, followed by instance-side revoke and connect;
no automatic data replacement is offered.

Network I/O holds no local locks. The observed active device, current config
pairing epoch and device binding are checked after the reply. Lock order is
config, registry, private store. The existing binding-reset operation explicitly
revokes private Health state when switching paired installations. Subjects are
interpreted within that paired issuer/instance boundary.

Migration selects active legacy devices, nonterminal legacy requests and
unexpired legacy pairing codes through private-schema indexes. Registry proof
is fetched by exact device or code key while retaining the same lock order;
there is no binding-wide history read. An active device's related requests and
pairings are promoted once using indexed provenance keys. Unrelated terminal
requests and revoked devices remain provenance only. Repeated owner checks do
not revisit this history. Retirement deletes only still-valid foreign pairing
codes; expired codes remain ordinary retention-cleanup work.

Once subject-bound, a verified different subject retains the existing automatic
revocation/purge behavior. A missing subject, cached answer or unavailable Cloud
never becomes an ownership change. Explicit revoke and device sign-out keep their
existing purpose; a deferred host sign-out may use its exact saved registry link
to verify a legacy enrollment against the live subject.

## Compatibility

| Instance | Cloud | Behavior |
| --- | --- | --- |
| Released | Released | Existing email gate and retirement behavior |
| Released | New | Additive user_id is accepted and ignored by the released Health gate; nullable email limitations remain |
| New | Released | Email compatibility only while no stored subject exists; no production email backfill |
| New | New | Subject identity; proven legacy migration; pause/preserve when continuity is unknown |

A missing field after any stored subject reports `cloud_owner_subject_unsupported`;
there is no downgrade to email. Explicit malformed/null subjects report
`owner_record_missing`. The general permissions parser remains additive; the
Health boundary validates the exact nonblank subject before using it.

## Consumers and diagnostics

All Health owner comparisons move together: request selection, pairing, bearer
authentication, consent manifests, notification targets, browser request cards,
binding tickets, Devices projection/revocation, read-audit projection and deferred
sign-out. General Devices notification/scheduling access rules stay unchanged.
Devices presentation verifies/migrates continuity without automatic retirement.
Local-host Health attribution and revoke use the proven subject too; notification
and schedule permissions retain their existing email rule. A deferred sign-out
keeps its scheduled retry when a subject-bound store receives an older Cloud
answer without a subject. Expired requests are settled before migration so an
unswept legacy request cannot require continuity proof after its lifetime.

Legacy enrollments made without a registry link cannot be promoted automatically,
including enrollments approved from an unattributed local browser. They remain
paused and preserved until the user agrees to instance-side revoke and connect.
No production enrollment inventory or backfill is part of this change.

`health status` performs a store-free live-owner preflight. If unavailable it
returns `read_allowed: false`, `sync_state: unknown`, and a localized diagnostic,
without device identity, metric freshness or values. After preflight, the ordinary
observed-device check runs before returning verified sync metadata. This incurs a
second live check on status to preserve the concurrent-enrollment fence without
opening the private database for basic unavailable diagnostics. It does not need
an earlier health read. Read, connect, refresh and upload remain fail-closed.

The stable machine code is `instance_eligibility_unavailable`. Four reason keys
separate Cloud availability, old Cloud, missing/malformed owner and unproven legacy
continuity. English/Chinese messages state pause/preserve behavior and a specific
next action. CLI errors and status diagnostics always render the English catalog
entry, independent of UI language; HTTP errors keep UI localization. CLI
`legacy_owner_unproven` diagnostics expose `next_action: "revoke_then_connect"`.
The Skill requires explicit agreement to clear retained data before running
`vibe mobile health revoke` followed by `vibe mobile health connect`. The new
connection backfills the last 30 days still available from HealthKit.

Known iOS limitation: its disconnect clears local credentials even when the
server DELETE fails. Unproven ownership still blocks that DELETE, so iPhone-only
disconnect does not replace the instance-side recovery. This change does not
relax device revocation authority or modify iOS cleanup behavior.

## Validation

`tests/test_mobile_health_owner_subject.py` covers email changes/null email,
same-email subject transfer, transaction rollback/reopening, all three owner
tables, linked/pending/absent/mismatched/cleanup proofs, schema downgrade refusal,
concurrent enrollment, stale connect scope, unavailable and confirmed status,
localized CLI/HTTP errors, notifications, ticket redemption, Devices and read
audits, and deferred sign-out. Its end-to-end synthetic case runs the real CLI,
browser approval, phone pair/upload and Devices projection with null Cloud email,
then changes email and transfers subject. No real Health data is used.

The compatibility matrix executes the frozen released owner gate from
`febd71b7c8` and the new gate. The unchanged released projection parser accepts the
additive subject with null email. The separate schema-fence test confirms an old
version cannot reopen an upgraded store; the matrix does not claim binary rollback.
Existing Health, Devices, permissions and binding-reset suites cover surrounding
behavior. Initial synthetic tests failed before implementation; targeted failures
also preceded the ticket, notification, deferred-cleanup and stale-connect fixes.
The query-cost regression compares 0 and 3,000 historical records, counts SQLite
VM instructions during the actual owner check, and asserts indexed SEARCH plans
without history scans or temporary sort trees. It also checks exact registry
proof lookup and zero registry lookups once migration has completed.
