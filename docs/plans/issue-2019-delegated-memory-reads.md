# Issue 2019: delegated Memory reads — simplified proposal

Status: simplified implementation authorized by the owner on 2026-09-17. One implementation lane; no merge or deployment authorization.

## Problem and outcome

A task the user delegated cannot search their Memory when a Task or Watch resumes it, even though a direct human turn can. After an upgrade/restart this forces the user to send another message before verification can use Memory.

Desired behavior: the same authorized delegation keeps the same user's Memory read access during managed continuation, including after restart. Synthetic notifications remain synthetic input.

Evidence baseline: GitHub master `0e5a672ad183ac56f5ee8df7bcb770754840b3d9`; see `docs/investigations/issue-2019-background-memory-analysis.md`.

## Smallest complete solution

1. **Carry the existing user identity into continuation.** Reuse the owner identity already validated by the shared Harness/session admission path. Connect it to the existing Memory scope calculation. Keep it separate from the event's author: a background `author_id` can be a Task ID. If the existing persisted execution context drops the validated identity, preserve only that missing fact in its existing metadata and restore it through the ordinary restart path.
2. **Remove the extra human-trigger requirement for reads.** A Task/Watch trigger must not invalidate an otherwise authorized user's Memory read scope. Keep the existing user/project isolation and existing binding/access checks; do not duplicate them inside Memory.
3. **Keep capture and write semantics intact.** Do not relabel background messages as human input. The shared read/write scope helper needs a focused adjustment so this read fix does not accidentally expand `remember` or administrative capabilities. Prefer a small read-specific helper or local call-site distinction if needed; do not introduce a permissions model, operation matrix, or separate grant store.

The implementation should be ordinary identity propagation plus removal of one redundant condition, with the minimum read/write call-site adjustment warranted by the current shared helper.

## One implementation question still to resolve

Identify the existing host-validated owner value used by the Task/Watch execution path and its persistence location. Source inspection confirms related caller/resource/admission records exist, but does not establish that one currently supplies all the facts Memory needs.

Resolve this concrete connection before editing the affected path. Do not turn it into a prerequisite to redesign every Harness origin or Session ownership model. Caller-supplied metadata is not made authoritative merely because it is persisted. If the identity really is absent, return the existing access-denied result; do not guess local ownership, use the latest speaker, or build historical recovery machinery.

## Design limits

### Identity source audit (implementation lane, 2026-09-17)

- Direct Workbench identity is `Delivery.author_id`, produced by
  `vibe.ui_server._workbench_author_id()` from strict `memory_ui_user_key()`
  admission. Generic local resource Owner admission is broader (for example,
  LAN setup access) and cannot substitute for this identity.
- Task/Watch creation calls `metadata_with_resource_user_context()`. Remote
  subjects survive in `metadata.resource_user_context` and existing execution
  checks restore/revalidate them. Local contexts are deliberately omitted;
  `instance_owner_context()` has an Owner role but no subject.
- `created_by.caller` records CLI environment provenance, not independently
  validated personal identity. Claude's session-stable caller also omits
  `user_id` and `message_id`. These fields cannot be promoted into owner grants.
- Scheduled dispatch stores/restores `scheduled_provenance` in the immutable
  Delivery snapshot, but its author is the definition ID, not the delegator.
  This is a usable persistence path once a trusted owner has been established.

Decision confirmed by the orchestrator: definition creation resolves the creating
Session's current `active_turn` and `delivery_for_turn` in the existing database
(and its exact accepted Message FK once native acceptance clears the snapshot);
creator and target Session must match. The owner value comes only from that
Delivery's authenticated author or its existing host-stamped continuation
metadata, never caller `user_id`. Store `delegated_memory_owner` (platform,
user ID, private-message fact) in Task/Watch metadata; pass it in scheduled
`message_metadata`, which the ordinary Delivery snapshot restores after restart.
Existing resource metadata and IM bindings remain the current authorization
checks. No historical search or new endpoint is needed. Synthetic read scopes
are excluded from the existing write accessor; capture classification is unchanged.

- First delivery covers the issue's same-Session Task and Watch continuations. Shared code may naturally benefit other managed paths, but arbitrary cross-Session delegation, group Memory, and general Agent delegation redesign are not acceptance requirements for this fix.
- Reuse existing current authorization and revocation checks. Do not recursively traverse execution ancestry on each search, build a parallel authorization service, or add extra per-stage checks for the same fact.
- Reuse existing persisted execution context for restart. No new Memory permission database, task toggle, approval step, long-lived credential, or cached last-successful grant.
- Do not add a standalone compatibility subsystem to infer owners for unresolvable historical tasks.
- Do not modify EverOS, prompt composition, UI, or individual backend authorization rules for this issue.
- Do not add defensive branches for hypothetical inconsistent combinations unless the real call path or a focused reproduction demonstrates the need.

## Focused verification

1. An authorized direct turn, Task continuation, and Watch continuation query the same user's default/named Memory project successfully. Exercise the real shared dispatch-to-read boundary rather than fabricating the missing identity at the final gate.
2. A persisted continuation still works with a fresh controller after restart. This is the reported use case, not an optional edge case.
3. Representative existing isolation/denial coverage still passes: two users remain separate; missing identity and revoked access remain denied. Reuse existing tests for other unaffected authorization behavior rather than multiplying a full platform/backend/queue-state matrix.
4. A synthetic Task/Watch event does not enter automatic user-input capture. Existing Agent-write provenance and access behavior do not expand as a side effect.

Add the relevant continuation cases to the existing Memory scenario catalog. Use one real local Incus acceptance flow with a Task, a Watch, and regression-controller restart. Do not restart the user's running host or copy another turn's credentials for verification.

## Delivery

One focused implementation PR using shared code and the existing repository CI/review requirements. No mandatory new module hierarchy or line-count target: minimize mechanisms while completing the ordinary Task/Watch and restart behavior.

Owner acceptance: delegate a task that needs a known non-sensitive Memory fixture, allow it to resume automatically, and confirm correct retrieval without another human message.

## Implementation evidence

- `MEMORY-SEARCH-021` / `022`: real durable turn admission, definition creation
  and reload, scheduled submission, persisted queue, fresh controller/turn manager
  recovery, internal HTTP search in default/named projects, and Task -> Watch
  identity propagation. Native backend and provider execution are test doubles.
- `MEMORY-SEARCH-023` / `024`: absent/forged identity denial, two remote owners
  querying the same named project without sharing scope, and current binding
  revocation denying registered delegated reads. The continuation tests also
  exercise capture admission (synthetic input is skipped) and remember HTTP 403.
- Local Incus doctor passed after starting the existing Lima VM. Isolated runner
  `up --target worktree --slug memory-reads-2019 --reset-mode none --env-file /dev/null`
  stopped before provisioning because dedicated LLM/IM seed credentials are absent.
  The orchestrator confirmed no dedicated `.env.regression` fixture exists. Real
  Task/Watch/backend/controller-restart acceptance remains unverified; no host
  service restart, production credential borrowing, or master update is authorized.
- Verification before first PR: 4 new continuation contracts passed; 639 existing
  Task/Watch/Caller tests passed (643 including new tests); 106 existing Memory
  internal/capture/disabled tests passed; 23 additional prompt/durable Memory
  checks passed (27 including new tests). Changed Python files pass Ruff.
- Cleanup: runner delete found no project/instance (up never provisioned one);
  runner reconcile confirmed no local worktree regression environments remain.
