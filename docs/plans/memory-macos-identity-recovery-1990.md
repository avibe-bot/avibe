# Memory subprocess lifecycle: remove continuous identity authentication

Status: design reviewed, review clarifications incorporated, 2026-09-15. The owner selected
this direction and authorized implementation. Implementation evidence is tracked
in [the acceptance report](../investigations/issue-1990/implementation-acceptance.md);
installed runtime operations remain outside this task. Issue: [#1990](https://github.com/avibe-bot/avibe/issues/1990).

## Decision and outcome

Manage the owned direct child through its subprocess lifecycle. Remove repeated
creation-time comparison as a condition for keeping Memory running. Preserve
public-library reuse checks at process-control boundaries and keep historical
orphan reconciliation as a separate exceptional path.

A healthy child whose public creation time changes continues processing. Actual
child exit is delivered by `process.wait()`. Shutdown stops the owned execution
and confirms cleanup before a replacement can access the same data root.

This replaces the earlier private psutil getter, Darwin ABI adapter, new record
schema, boot UUID, identity-reconfirmation task and supplemental restart
proposals. None is part of this design.

## Evidence

The [industry comparison](../investigations/issue-1990/industry-supervision.md)
and [incident investigation](../investigations/issue-1990/README.md) explain the
model and its boundaries. Source assessed:
`199cccf958349881209c00b20124b82726f3ce0c`, cross-checked against incident source
`7f3070863440bcccf0bffb2694f4d6c9d3abd098`. Reconcile with latest master before
implementation.

A further [native probe](../investigations/issue-1990/public_process_probe.py)
uses public psutil Process equality, is_running and terminate against a private
test child. With a probe-local +1-second boot-time correction, identity remains
equal and terminate/wait succeeds. [Recorded result](../investigations/issue-1990/public-process-result.json).
Only the fault injection uses a psutil internal test hook; the proposed product
implementation uses public interfaces. No real clock or Memory state changed.
The recorded probe covers Darwin arm64 with psutil 7.2.2 only. Its private
boot-time injection is version-bound test machinery, not a product dependency
or evidence that every supported version has been exercised.

## Ownership by boundary

| Boundary | Authority and behavior |
| --- | --- |
| Spawn | Retain the asyncio child, its isolated process group and a public psutil Process reference captured for this execution |
| Normal runtime | Await the exact asyncio child; use existing readiness/health behavior for service availability |
| Safety inspection | Preserve TCP-listener checks and descendant observation; no display-time equality admission gate |
| Stop | Use retained public process references for identity-aware signals and existing process-group safety policy |
| Replacement | Require direct-child reaping plus proven tree cleanup before new startup |
| Across controller restart | Existing released-record ownership classifier establishes authority before capturing fresh live references |
| Processing health probe | Capture its own child/tree references and use the same termination helpers on timeout or completion |

The asyncio child is the lifecycle reference, not an atomic kernel handle on
macOS. Retained psutil references supply ordinary PID-reuse protection for
process-control operations. Do not replace a retained reference by constructing
`psutil.Process(pid)` at signal time and assuming it is the original child.

## Process-host implementation contract

Keep platform process details behind `_ProcessHost`, which remains stateless.
Keep references in `EverOSProcess._owned_processes`, replacing its float values
with opaque live references (public psutil Process objects in production).
Reapers and the one-shot health probe retain their own execution-local maps.
Pass these maps and the existing host through cleanup helpers, including
`_wait_for_owned_exit`; remove its fresh `_SystemProcessHost()` construction.
Do not expose psutil through `EverOSSupervisorPort`, serialize Process objects,
create another supervisor, or retain parallel live timestamp authority.

Capture the root reference immediately after subprocess creation and before
admitting readiness. If the child exits during capture, converge through the
existing startup/exit cleanup. The existing spawn/record failure handling must
retain cleanup authority for any surviving child; inability to capture does
not authorize a new child or disposal of a retained tree.

For descendants, observe them through the captured parent's public children
API and existing group discovery. Bind newly observed members only while the
originating parent/group authority is established. Preserve captured references
when a process later reparents or leaves the original group; do not refresh a
PID entry into a different process generation. Unknown current group members
remain unknown, not owned. This is bookkeeping for cleanup and the existing
listener policy, not a separate runtime authentication test.

Use `None` for an unresolved observed member instead of the `-1.0` sentinel.
A member is unresolved when its reference cannot be captured/validated or its
ownership cannot be established; permission/read errors are not absence.
Unresolved presence blocks group signals and proof of cleanup. It may be
resolved by a later attributable capture or positive absence. An unreadable
previously captured member keeps its original reference.

A captured reference that reports gone/reused is terminal for this execution:
never replace it by recapturing that PID or signal it again. Retain that entry
until the execution is retired so a later scan cannot silently adopt the reused
PID. This settles only the original target, not the whole tree; any current
unattributed group member still prevents group signaling and cleanup proof.

An observed exit during a read-only scan is handled as an ordinary lifecycle
race; it must not become an authentication-failure recovery event. If the scan
cannot attribute a replacement PID to the child, do not inspect or signal that
replacement as if it were Memory. Await the child's exit path. Preserve the
existing policy for an actual TCP listener or inability to establish the
required listener safety check; this design does not weaken that independent
rule. A mere public timestamp change must not reach either failure branch.

The exact private helper signature changes are implementation-local. Retain
one live-reference representation produced by the host and one released-record
representation at persistence; do not convert between them by private psutil
fields or by hashing Process objects into numeric identity stamps.

## Normal-running changes

`_watch_child()` remains the authoritative direct-child exit path. In
`_wait_for_ready()`, readiness follows child exit and the existing private
socket/health checks, not repeated birth-time matching.

In `_monitor_child()`, remove the independent 0.2-second identity-only checks.
Preserve the existing one-second descendant/listener inspection cadence where
needed for its actual safety purpose. Decouple `_assert_no_tcp_listener()` and
tree bookkeeping from `_host_identity_is_live()`/adjusted-time comparison so
the same problem is not merely moved to the slower scan.

Do not introduce a new health polling loop. Preserve current health and
readiness policy, including its actual failure handling. Changes to the
`running` property's starting/down projection are outside this issue.

## Stop and tree cleanup

Use retained public Process.send_signal/terminate/kill for individual processes.
These methods use psutil's own identity behavior. Distinguish an exited or
reused process from an unreadable one using existing cleanup outcomes. Do not
signal an unreadable/reused target and do not treat unknown presence as proof
that cleanup finished.

Keep process-group signaling only when the current members are confirmed
members of the captured owned execution and the group is not Avibe's own group.
If that cannot be established, target confirmed individual retained references;
unknown survivors keep cleanup incomplete. A numeric pgid by itself is never
authority, including after the leader exits.

Continue existing TERM, bounded wait, KILL and final wait behavior. The direct
child's `wait()` result is insufficient if known descendants or an unresolved
owned group remain. Do not retire the record, discard references, remove a
still-used socket or start a replacement while cleanup is unproved.

Use existing lifecycle locks, child-object identity and supervisor generation
checks to serialize stop, natural exit, manual Wake and automatic recovery.
Exactly one path owns final cleanup; late callbacks cannot clean a newer child.
Library checks reduce the numeric-signal race; this design does not claim an
atomic macOS pidfd equivalent.

## Historical records and orphan recovery

Keep released JSON schemas and diagnostic creation-time semantics unchanged.
The existing command/UID/role/interpreter/provider-root/socket checks stay in
`SidecarOwnership` and `ReleasedEverOSOrphanReconciler`, reached at startup or
explicit recovery rather than as continuous running-state authentication.

When a candidate passes that classifier, retain a fresh public Process reference
and bracket identity-bearing reads with public identity checks before acting.
Never compare its historical adjusted timestamp to a raw value for routine
live-child control. Never treat a matching PID or socket health response alone
as sufficient ownership. Parent/helper records require the same separation.

Specifically, `_terminate_orphan_tree`, `_terminate_claimed_processes`,
`_reap_unidentified_child` and `_wait_for_identities_exit` must use the references
captured during successful classification, throughout signaling and waiting.
`_snapshot_process_group` captures references for newly attributable members
and preserves unresolved members. A timestamp shift between classification
and waiting must never produce a false exit, retire a live orphan's record or
permit overlapping writers. Capture before the classifier's identity-bearing
reads and validate the same reference afterward; do not classify one generation
and then unconditionally capture whichever generation currently has its PID.

Do not move this path wholesale to `core/process_isolation.py`: that module
also compares adjusted public creation times and has different group-marker
semantics. Reuse compatible low-level pieces only after checking their contract;
there is no repo-wide process management rewrite in this task.

## Recovery scope

The reported display-time discrepancy no longer marks a healthy child down,
pauses claims, consumes restart attempts, or needs reconfirmation recovery.
Actual exits and safety failures continue through the existing bounded Wake
policy. Wake stops retained execution, reuses the same data root, proves
startup readiness, and only then resumes claims and the writer.

After an unrelated failure exhausts automatic attempts, an explicit Wake can
retry once ownership/cleanup is possible. There is no new indefinite background
reconfirmation or automatic-retry promise. This intentionally supersedes that
part of the earlier design: the approved architectural direction removes the
false failure trigger rather than adding recovery for it. Existing installed
processes do not change behavior until a separately authorized update/restart;
no live hot-patching is proposed.
Troubleshooting should state that `degraded` with `memory_wake_failed` after
the automatic budget is exhausted requires a manual Wake attempt.

## Scope and acceptance

Primary scope: `avibe_memory/process.py`, focused process/Wake/supervisor tests,
dependency declarations, Memory scenario metadata and troubleshooting docs.
Change supervisor/runtime code only where required to preserve cleanup
contracts; retain their public interface and retry policy. No sidecar IPC,
platform service registration, lock-lifetime redesign or UI change is needed.

Required behavioral evidence:

1. On isolated macOS, change the public creation-time observation while keeping
   the real child alive. Run through several full listener/tree scan cycles:
   no rejection, no signals, no Wake, and new processing remains possible.
2. Stop that same child during the adjustment through public process operations:
   stop/reap succeeds, old data remains, explicit Wake resumes processing.
3. Exit/reuse between scan and stop never signals the replacement. Include a
   delayed asyncio exit callback and retained-reference tests with a genuinely
   different generation, not only a NoSuchProcess stub.
4. Child/grandchild retention, group membership uncertainty, permission denial
   and leader exit keep no-overlap/no-mis-signal behavior. Preserve TCP-listener
   safety regression coverage, including a real prohibited listener case.
   Cover `processing_healthy()` probe timeout during a timestamp shift: signals
   still reach the captured probe and cleanup proves its exit.
5. Released records, missing records, mismatched UID/root/role and stale helpers
   preserve startup compatibility and no authorization from PID alone.
   Shift the timestamp between orphan classification and exit waiting: a live
   orphan never appears cleared and its record remains until actual cleanup.
6. Manual Wake, close and natural exit races leave no duplicate cleanup or
   late restart; automatic recovery retains its current bounded budget.
   After exhaustion, the failure remains visible and a later manual Wake can
   restore processing once cleanup is possible.
7. Full runtime scenario: old memories remain readable and a new test-owned
   input is processed and recallable after successful Wake. Health alone is
   not the acceptance result.

Extend existing memory_repair scenarios (especially MEMORY-WAKE-001 and
MEMORY-WAKE-202) and allocate any new IDs after checking latest master. Use
isolated macOS for native behavior and local Incus for shared runtime scenarios.
The macOS tests must validate the version-bound injection hook before using it;
Incus evidence does not replace native macOS execution.
If provisioning a new Incus target requires external IM/model credentials,
use a hermetic native test for the processing scenario: actual pinned EverOS
child, production Wake/claims/writer paths, test-owned storage, and a loopback
model/embedding test server. Only the external provider response is simulated;
processing and recall must execute through the actual runtime, not mocked
storage or direct database insertion. This demonstrates lifecycle and data
continuity, not external provider quality or four-platform deployment. Record
the blocked Incus layer separately and retain Linux CI coverage.

## Dependency contract

Require `psutil>=7.1.0` in both the root `pyproject.toml` and
`packaging/avibe-memory/pyproject.toml`. The companion package currently accepts
older Avibe hosts, so its own dependency must also enforce the floor. Update
lock metadata as needed and verify the constraints in built package metadata.
The current lockfile's 7.2.2 does not enforce the floor for user installations.

psutil 7.1.0 introduced monotonic identity on Linux and macOS; older Linux
identity uses wall time. Retaining older references would reintroduce clock
changes that permanently invalidate them. See the primary source comparison:
[7.1.0 identity](https://github.com/giampaolo/psutil/blob/release-7.1.0/psutil/__init__.py#L336-L367)
and [7.0.0 source](https://github.com/giampaolo/psutil/blob/release-7.0.0/psutil/__init__.py).
Validate public identity behavior at the declared floor and the resolved
version; the single-host 7.2.2 probe is not a substitute.

## Independent design review

The owner-requested PM review returned **PASS WITH CHANGES, no blockers**.
The [original review](../investigations/issue-1990/pm-design-review.md) assessed
the preceding revision. All six requested clarifications are incorporated:
dependency floor, orphan helper consumers, unresolved-member representation,
execution-owned references with a stateless host, terminal-reference semantics,
and processing-health probe coverage. The unrelated running-projection change
was removed, and manual-Wake troubleshooting and acceptance were made explicit.
This records incorporation, not a second reviewer verdict on this revision.

## Delivery

One coherent implementation lane should own the process-host refactor and its
consuming tests. First preserve the tests for stop/reap and legacy recovery,
then remove live timestamp-authentication helpers made obsolete by public
process references. A successful change reduces the live-path responsibilities,
not just the number of lines in one helper. Apply normal repository Codex/CI
review gates before integration; no merge, installation, or release is authorized.
