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
checks. No historical search is needed. Synthetic read scopes
are excluded from the existing write accessor; capture classification is unchanged.

### Review-discovered creator binding correction

PR #2023's first review exposed a normal CLI override: changing
`AVIBE_SESSION_ID` could stamp another active IM user's owner. Session equality
alone is not authentication. The orchestrator approved this bounded correction:

- Shared `caller_env_for_platform_payload` issues `AVIBE_CALLER_SESSION_PROOF`,
  an HMAC-SHA256 of canonical `[session_id, platform, user_id]` using one
  random controller-process key (strengthened after the third review).
  The deterministic proof is stable for Claude's session-cached environment.
- Definition creation sends the proof and same target Session to the existing
  Unix transport's `/internal/memory/delegated-owner` accessor. Constant-time
  verification binds the current exact-Delivery owner candidate before returning
  that same owner object. There is no signing API.
  Missing/bad proof or unavailable controller leaves ordinary definitions usable
  but grants no delegated Memory identity. In-process event-loop creation does
  not call its own socket synchronously and likewise leaves identity absent.
- Only owner facts persist in definitions and scheduled Delivery provenance.
  The key stays in memory. Proofs travel in existing execution transport (including
  Codex shell scripts and OpenCode binding files); they are excluded from caller
  metadata and OpenCode durable processing snapshots. OpenCode restores a fresh
  proof from its host-owned poll Session and exact logical Turn identity. No registry, schema, per-task
  credential, or backend-specific authorization policy is introduced.
- Threat boundary: documented CLI locator overrides are untrusted; a same-UID
  process editing host SQLite, transport files, or process memory is outside this
  focused correction. Existing resource and Memory revocation checks still apply.
- Public queued Delivery and accepted Message projections strip the internal
  owner both directly and under the known scheduled provenance shape, preserving
  raw stored values for continuation. Task/Watch public API and CLI projections
  hide the new owner marker using an explicit display-only store option; default
  runtime store reads preserve owner and existing resource context. Pause/resume
  reads raw stored metadata, never the redacted display projection.

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

- First-review correction evidence: `MEMORY-SEARCH-025` exercises the actual
  client/accessor/verifier/creation boundary for valid, missing, tampered and
  another Session's proof. `026` checks public definition output, SQLite runtime
  store reload and public pause/resume; queued/accepted Message consumer tests
  cover nested provenance redaction. OpenCode transport/snapshot/restore and
  Claude stable environment contracts reuse existing backend fixtures.
- Final first-review focused run: 1,073 passed (1,060 existing cases and 13 new
  continuation/proof/projection cases). Public API list/page/pause/resume tests
  and SQLite runtime reload checks pass. Changed Python Ruff passes. Definitions
  retain their existing public resource-context shape; only the new owner marker
  is hidden there. Message/Delivery projections retain their prior private-field
  filtering and additionally hide the owner at both known locations.

### Second-review class closure (orchestrator decision, 2026-09-17)

The public-projection class recurred at `606de20`: the first correction missed
Run output even though definitions copy their metadata into executions. This is
an incomplete consumer inventory, not a defect requiring another ownership model.
One narrow projection normalizes optional non-object metadata and removes only
`delegated_memory_owner` at the root and the known scheduled provenance path.
Message/Delivery retain their existing other-private-field filtering; Harness
records retain their legacy resource-context output. Internal reads remain raw.

| Record | Producer / runtime consumer | Public boundary |
| --- | --- | --- |
| Task | definition store -> scheduled enqueue -> runtime reload | display-only `_enrich_definitions`; CLI fallback |
| Watch | definition store -> hook request -> runtime reload | display-only `_enrich_definitions`; CLI fallback |
| Run | task enqueue / watch hook -> request store execution | display-only `_enrich_runs`; full CLI `_run_payload` and audited direct outputs |
| queued Delivery | scheduled submission -> durable turn recovery | `public_delivery_payload` |
| accepted Message | native acceptance -> exact Delivery FK lookup | `_row_to_payload` public metadata projection |

The OpenCode initial bind and ordinary retry must share one merged caller/skills
environment snapshot. Restored initial/retry already share the refreshed host
proof. No proof, token, authorization, or persistence mechanism is added.
The circuit breaker paused edits after the repeated class; the orchestrator
approved this finite closure. Any third findings-bearing head after the proof
change stops edits/push for another complete inventory, even for a new class.

CLI bypass audit: `cmd_task_run`, hook-send, and asynchronous Agent Run replies
build explicit field lists without metadata. Synchronous Agent Run and run-cancel
outputs use `_run_payload`; run lists use its brief field list. There is no
separate `cmd_watch_run` command. Harness status projects Task/Watch/Run through
explicit field lists in `core/services/harness_status.py`, with no metadata.
The run lifecycle store and raw `_run_from_row` must not redact upstream.

Finite evidence enumeration: delegated-read tests cover public Delivery/Message
and SQLite Task/Watch reload; definition-to-run tests exercise real scheduled
enqueue and watch hook, full Run API/CLI output, and request-store reload.
Nonempty list/string plus empty list metadata stays readable without rewriting
stored JSON. Existing OpenCode process tests fail the initial bind once, let its
ordinary retry carry the proof, then create both Task and Watch through the real
verified accessor. Restored retry tests check a fresh proof stays out of snapshots.

Second-review focused validation: 377 passed (363 existing cases, 14 new PR
cases in this selection); changed Python Ruff and diff checks pass. This selection
covers the five families, malformed optional metadata, public Run output, native
binding retry/restore, CLI Task/Watch, and existing Harness run/status projections.
Scenario IDs `MEMORY-SEARCH-027/028` identify Run-copy and binding-retry evidence.
Run lifecycle SSE events also use a fixed field list without metadata.

### Third-review owner-binding correction (orchestrator decision, 2026-09-17)

The retained Session-only proof could authorize a later Workbench owner's
Delivery when the caller changed an unsigned resource-context subject. A hermetic
creation/dispatch/HTTP-read reproduction confirmed unchanged Alice context was
denied, but replacing its ordinary JSON subject with Bob yielded Bob's scope.
The third-head circuit breaker stopped edits; the orchestrator authorized only
strengthening the existing HMAC over canonical `[session_id, platform, user_id]`.
Same-owner later turns may reuse the proof: per-turn freshness is not required.

Host issuance reuses the one Delivery-owner extractor with the context's exact
`turn_token` when present. OpenCode restoration uses its stored logical Turn and
target Session, never another current turn's owner. A missing/wrong-Session exact
Turn has no fallback. The accessor reads one current owner candidate and verifies
against that same object before returning it. Caller JSON and routing author IDs
cannot supply signed ownership. No nonce, registry, token payload, cache, or new
endpoint is added. Claude's existing env reconciliation changes clients only
when the signed owner changes; ordinary same-owner turns remain stable.

Persisted owner facts and current authorization remain unchanged; restart rotates
the process key and can reissue from the exact stored Delivery. Any further
findings-bearing head requires another PM reassessment before edits/push.

Third-review focused validation: 234 cases passed, including the new full
create/dispatch/read cross-owner replay contract (`MEMORY-SEARCH-029`), exact-Turn
missing/mismatched Session denial, and restored old-owner OpenCode poll coverage.
The selection retains Claude cached-client, Task/Watch persistence/key rotation,
OpenCode retry/snapshot, and public projection tests. Changed Python Ruff passes.
Unavailable durable storage omits the optional proof so ordinary Agent launch
remains usable without granting delegated Memory. Real Incus acceptance remains
unverified for the previously documented missing dedicated seed credentials.

### Fourth-review execution-authority boundaries (PM decision, 2026-09-17)

The fourth findings-bearing head stopped edits for whole-invariant diagnosis.
The owner marker is execution authority: it must originate in host scheduling,
have a usable shape, and remain constant across a merged Turn. Owner-bound HMAC
continues to protect creation; no further credential mechanism is needed.

Use one owner parser (nonempty string platform/user_id, normalized is_dm) and one
source-aware known scheduled-provenance reader. Shared user snapshot intake strips
reserved owner and scheduled provenance before persistence; hydration also strips
old user-row injections. Hydration transports immutable Delivery.source as
`delivery_source`, excluded from captured/restored metadata overlays. Delegated
admission requires that host source and a scheduled trigger. Provenance presence
alone never converts ordinary input into scheduled execution.

One canonical authority component in `message_merge_identity` compares owner and
resource_user_context at root and the known scheduled nested path. The existing
queue collector and immutable acceptance both consume it: differing/absent
identity separates Turns while equal authority remains batchable. No arbitrary
metadata comparison, new permission database, or backend-specific policy is added.

Fourth-review validation: 518 focused cases pass (503 cases already present on
`abcb0147b`, plus 15 new parameter cases). `MEMORY-SEARCH-030` covers malformed
raw nested metadata and owner fields through durable dispatch and host env
issuance. `031` covers both Task and Watch batches: differing owner, absent owner,
or resource snapshot separates execution; compatible authority still merges,
normal native acceptance checks immutable identity, and HTTP reads retain the
correct scope or denial. `032` drives actual UI POST through shared snapshot
persistence and durable dispatch with strict author None, plus a valid historical
user-row injection: both remain human and never call the Memory provider.
Existing authenticated UI, stable proof/replay, persisted restart, OpenCode
retry/restore, Claude caching, and public projections remain in the selection.
Changed Python Ruff passes. Real Incus acceptance remains unverified.

### CI lifecycle compatibility correction (PM decision, 2026-09-17)

The clean-reviewed head `cef490d11` failed an existing scheduled-gate lifecycle
contract: explicit `submit_scheduled` without a task trigger still means scheduled
execution. Separate execution classification from Memory eligibility. The shared
provenance reader requires host harness source and structured provenance only;
owner extraction/signing and delegated admission retain the nonempty string
trigger requirement. Keep the existing lifecycle assertion unchanged and prove
no-trigger scheduled execution grants neither proof nor Memory reads, even with
owner metadata. No new mechanism or identity exception is introduced.

Validation: all 410 cases in full `test_internal_server.py`, durable lifecycle
`test_session_delivery_fsm.py`, and delegated contracts pass. The original failing
scheduled lifecycle test is unchanged. New `MEMORY-SEARCH-033` exercises the real
shared scheduled gate with owner metadata but no trigger: dispatch remains
scheduled, host issuance omits proof, and HTTP reads deny without provider calls.
Changed Python Ruff passes. A new head requires fresh Codex review and CI.

### Active lifecycle closure (PM decision, 2026-09-17)

At `a8a79660e`, the required pause preceded five hermetic diagnostic cases:
authenticated same-Session creation produced an accepted delegated Task Turn;
local, different-owner, and ownerless Workbench P1 inputs all steered under its
scope; internal rebinding removed its owner; active OpenCode revival reissued a
valid proof but fresh-controller HTTP reads denied. The latter two are host
continuations, while different-owner steering is new authority.

| Lifecycle | Existing seam | Required authority behavior |
| --- | --- | --- |
| Definition creation / CLI edit | owner-bound proof accessor | same-Session re-admission; no caller grants |
| Host same-definition rebind | guarded store binding-only write | preserve owner/resource metadata and CAS/deletion/repoint guards |
| Queued/new Turn | durable hydration and batch identity | existing admission; one authority per batch |
| Active OpenCode revival | exact Session + live logical Turn/initial Delivery or accepted Message | shared context hydration + current admission before binding; no fallback to another Turn |
| P1 / pending P1 / send-now / restored steering | shared pre-native `_dispatch_steer_batch` | compare effective human/delegated owner/resource identity; refuse to existing queue fallback on mismatch |

Same-owner human continuation remains steerable. Comparison ignores resource
credential-refresh timestamps, not role/membership/instance identity. Human-only
steering remains outside this delegated boundary change. Ordinary CLI edits still
re-admit; no public trust flag, credential registry, or cross-Session creation is
introduced. Watch internal runtime writes already use separate guarded methods;
there is no automatic update_watch rebind caller. Only OpenCode revives active
native polls; Claude/Codex use fresh durable dispatch after restart.

Active-lifecycle evidence: the new contract file has 18 cases. `034` revives actual
Task/Watch polls with fresh controllers and HTTP reads, and denies missing,
wrong-Session, terminal, and currently revoked identity before publishing a proof.
While the revived poll remains active, incompatible P1 input queues without a
native write. `035` creates and authentically updates a task to `create_once`,
performs host binding recovery, reloads it, and dispatches an HTTP read under its
retained owner; ordinary proofless edits still remove delegation. Existing full
scheduled-task tests retain concurrent SQLite reclaim/CAS evidence. `036` covers
direct P1, pending P1, and queued send-now: different/no owner waits then receives
fresh scope/denial; the same local owner and refreshed remote owner still steer.
An ineligible IM group remains denied Memory and keeps its prior steering policy.
A previously established scope continues fencing input even if its binding is
revoked temporarily; current read checks still enforce revocation.

Validation layers: 1,026 combined focused cases passed before the final small
steering eligibility adjustment; 431 affected lifecycle/internal/restore cases
passed after the admission check, and final 223 delegated-lifecycle/durable-FSM
cases passed after retaining the established-scope fence (18 new cases, 205
existing FSM cases). Changed Python Ruff passes. These counts overlap and are
not additive. No real Incus/backend acceptance is claimed.
