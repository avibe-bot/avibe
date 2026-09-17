# Instance Member operations

Status: approved behavior contract, implementation under validation (2026-09-17).
Orchestrator: Avibe Session `sesnps2m743r5`.
Baseline: `06a864947` on `avibe-bot/avibe` master.
Branch: `fix/member-instance-operations`.

## Intent and precedence

An Instance Member can operate and manage the entire current instance but cannot
change access grants, instance identity, or ownership. This applies to Personal
and Organization instances. Organization membership alone grants no instance
authority. The owner explicitly approved implementing this Member contract while
deferring the unified Editor/Viewer redesign.

Example: a Member can already edit a built-in Agent and set it as default, but
the Organization resource-use check hides that same Agent when no policy row
exists and prevents chat. The intended result is one consistent list/detail/
selection/chat path, including existing Projects and sessions.

This contract supersedes the retained Member resource-use and private runtime
record restrictions in `member-management-parity.md` and
`instance-access-role-member.md`. Their access-administration safeguards remain
authoritative. Update those documents to make this precedence explicit.

## Frozen behavior

1. **Current-instance operation scope.** A validated Instance Member receives
   Owner-equivalent operational access to resources and runtime records held by
   this instance. Agent, Project and session list/read/use/edit/execute paths must
   agree. Missing resource policy, another resource owner, restrictive bindings,
   unmatched policy groups, or legacy policy metadata must not narrow Member
   operations within this instance. The grant does not reach another instance
   or create cloud organization-administration rights.
2. **Access and identity remain protected.** Member cannot change authorized
   principals, access-member enabled/admin/role state, binding codes, Project or
   resource access policy, pairing identity, session-signing identity or instance
   ownership. Existing protected mixed writers retain fresh-state, atomic
   comparisons, omission/alias/concurrency handling, and honest rejection.
   Operational writes must not silently rewrite stored ACL or ownership rows.
   Ordinary creation keeps its existing ownership/lifecycle semantics; bulk
   ownership onboarding remains protected.
3. **Keep identity distinct.** `is_instance_owner` remains false for Member.
   Reuse the existing appropriate management capabilities as the owner of
   operational scope; do not mint another role or a redundant permission axis.
   Memory principal isolation, terminal attachment identity, per-user preferences,
   notification subscriptions and protected Vault authorization/proofs still
   apply where they also apply to Owner. Preserve the existing shared
   `messages.read_at` and global Workbench toggle semantics; this repair does not
   introduce per-person read state or merge identity where it already isolates.
   IM binding/admin is a separate identity system and is not elevated by a cloud Instance Member grant.
4. **Preserve other roles.** Editor, Viewer, unauthenticated and invalid-context
   behavior remain unchanged. Existing restrictive resource rules still apply to
   those roles. Preserve CSRF, signed identity validation, resource existence,
   Agent enabled/available checks and other ordinary execution preconditions.
   Preserve shipped Member pre-catalog selection/session fallbacks. Do not expand
   the separate Owner-only missing-name fallback for new Task/Watch bindings:
   those Member bindings still require an existing catalog Agent. Released Member
   deferred snapshots participate in the existing binding migration. A repair of
   a previously completed pass must prove its original pairing and row cutoff;
   it cannot make later unbound work executable.
5. **Effect-based policy, bounded implementation.** Evolve the existing central
   capabilities and route policy. Member operational permission should follow
   that common decision, while access/identity effects retain explicit gates.
   Do not default-allow unknown APIs, redefine Owner, or rebuild the complete
   four-role policy framework. Registered routes must remain deliberately
   classified and covered by the existing complete-router invariant.
6. **Project/session identity and Show policy.** Effective Member roles stay
   `member`, never synthetic `owner`. Project archive is reversible: Member
   can list archived Projects explicitly, read their record context and reopen
   their folder like Owner. Missing Projects still fail; Editor/Viewer keep
   archived-Project denial. Archived sessions remain inert under the existing
   mutation/fork/execution guards. Project bootstrap, Show and deferred execution
   use the same effective-role decision rather than treating archive as an ACL.
   Show's single `/p` publication/link-visibility/limited-email axis already admits Editors.
   Only session-dependent create/get/ensure authorization changes here; existing
   sharing/owner-control guards stay unchanged. Unified publication/role design
   is deferred with Editor/Viewer.

## Required consumers

- Agent discovery, detail, selection/default resolution, new chat, existing chat,
  forks, and Harness bindings/execution must consume the same Member operation
  scope. Default Agent remains advisory for principals still subject to ACL.
- Project and session operations include restricted Projects, sessions without a
  Project, and dependent Show session operations. Lists, details, mutation,
  search/inbox, graph/running records, callback source metadata and notifications
  must agree with the operation scope. Preserve per-user delivery identity and
  read/preferences state.
- Member management event delivery must survive both event-role authorization
  and runtime visibility filtering. Cover real publisher payloads, including
  `vaults.updated`, `definitions.updated`, sessionless `runs.updated`, and
  `remote_access.quality.changed`. Lower-role events remain scoped.
- Skill usage and runtime observations are instance operational diagnostics.
  Audit the `vibe data skill-usage`, `skill_usage_daily` and `agent_events` gates;
  admit Member where the only restriction is Owner operational authority.
  Preserve SQL read-only enforcement, raw-secret denial and principal-scoped data.
- RemoteAccess pairing/re-pair controls must reflect the retained pairing gate
  for paired and unpaired Member views, using existing disabled/read-only and
  localization patterns. Ordinary transport controls stay usable. No redesign.

## Implementation scope

One coherent application lane owns the change and its full PR lifecycle. No
backend repository change is currently justified by the audit.

Allowed production scope: `vibe/authorization.py`, `vibe/ui_server.py`,
`vibe/api.py`, narrow diagnostic guards in `vibe/cli.py` and
`storage/read_only_query.py`, `storage/resource_access_service.py`,
`storage/project_access_service.py`, `storage/workbench_sessions_service.py`,
`storage/messages_service.py`, `core/vibe_agents.py`,
`core/services/session_fork.py`, `core/show_pages.py`,
`core/web_push_notifications.py`, and existing Harness/resource consumer modules
only where a traced Member authorization call path requires alignment.
PM-approved narrow extension (2026-09-17): `vibe/i18n/en.json` and
`vibe/i18n/zh.json`, only the existing `data.skillUsage.ownerRequired` and
`data.skillUsage.helpCommand` values, so diagnostic denial and parser help
accurately name Member or Owner; no new key. The help value was approved during
the second review diagnosis below.
UI scope: `ui/src/components/RemoteAccess.tsx`, its focused tests, and existing
EN/ZH translation files only as necessary. Related focused tests, permission
scenario catalog/harness and concise permission documentation are allowed.

Do not modify unrelated runtime/Memory features, dependency versions, schema,
cloud role aggregation, onboarding design, Editor/Viewer policy, production
state or the primary checkout. Report any required scope extension to the
orchestrator with the concrete call path first.

## Verification invariants

1. Seed each existing resource-policy and session shape in test-owned storage;
   a Member can complete the same current-instance operation through discovery,
   detail, mutation, execution and dependent visibility consumers. Assert the
   original ACL/ownership records remain unchanged by the permission fix.
2. Exercise signed remote HTTP + CSRF through a real Member Agent/session flow,
   including an Organization built-in Agent without a policy. Use real local
   services/storage and stub external providers/host operations/IPC only after
   authorization; verify stored mutations and resulting execution dispatch.
3. Compare existing roles and both instance kinds. Assert retained Member denials
   before protected effects, and unchanged Editor/Viewer scope. Preserve relevant
   #2004 mixed-write, Vault proof and principal-isolation coverage.
4. Consume real SSE generator/broker output for publisher-shaped management and
   scoped events, proving delivery rather than only testing one predicate.
5. Render RemoteAccess under actual capability providers, exercise Member
   pairing controls and normal operations, and assert no forbidden pair request;
   preserve the Owner flow. This is existing UI permission binding, not a visual
   redesign requiring new mockups.
6. Update the existing permissions scenario catalog and executable scenarios with
   stable IDs. Run focused Python/UI tests, Ruff for changed Python, and UI build.
   Record evidence layers and any manual end-to-end gap without claiming a live
   tenant fix. All probes must be hermetic.

## Delivery and acceptance

- Follow `AGENTS.md` and `.agents/skills/pr-delivery-loop/SKILL.md` in the task
  worktree. Open one non-draft PR to current master with the behavior contract,
  scenario IDs, evidence and retained-boundary ledger. No manual Codex trigger:
  current repository policy delegates triggering to cyhhao's automation.
- Require exact-head Codex pass, expected CI green, zero unresolved threads,
  clean merge state and independent orchestrator diff/consumer-test verification.
  Use one durable lane PR/CI Watch and one independent orchestrator gate Watch.
  Enforce review root-cause circuit breaking before another fix push.
- Do not merge, deploy, install, restart the local service, alter production
  permissions, or repair regression pairing as a side effect. Owner approval is
  still required for merge/release. Tests may use the sanctioned local Incus
  runner with test-owned state if needed; preserve all existing regression data.
- Acceptance after authorized integration: an Instance Member opens Agents,
  selects a built-in Agent and completes a conversation; opens existing Project
  and standalone sessions; receives live management/session updates; can operate
  RemoteAccess transport but cannot pair or edit access grants. Editor/Viewer
  remain restricted exactly as before.

## Work status

- [x] Complete and independently verify the three-lane baseline audit.
- [x] Recover the owner discussion and freeze the approved Member semantics.
- [x] Implement and validate in the isolated task worktree.
- [ ] Complete exact-head review and CI; independently verify the final diff.
- [ ] Present the concrete merge-ready PR and residual acceptance steps.

## Implementation and evidence map

The existing management capabilities own current-instance operations. Resource
use, Project/session projection, runtime list/graph/search/inbox/media, callback
source metadata, notification badge visibility and SSE filtering consume that
scope. Owner identity and protected access writers remain independent. The
runtime helper was renamed only at its consumers; bulk onboarding retains an
explicit true-Owner guard. No schema, dependencies or cloud aggregation changed.

- PERMISSIONS-014: Personal/Organization signed HTTP + CSRF Agent/session
  lifecycle, ACL-shape comparison, Project archive/restore and mutation/reuse,
  real Show store, real fork reservation, runtime discovery and persisted Harness
  bindings. Signed HTTP Deliveries also pass the real execution-time
  `SessionTurnManager._remote_delivery_execution_denial` before provider acceptance
  is stubbed; a changed pairing still rejects those persisted Deliveries. Released
  Task/Watch/Run/Delivery fixtures also exercise fresh and completed-marker
  migration through Harness admission and the queued-chat execution recheck,
  preserving principal attributes and recalculating the Delivery snapshot hash.
- PERMISSIONS-015: protected access/pairing/onboarding denials, missing resources,
  disabled Agent, existing lower-role and compatibility checks.
- PERMISSIONS-016: actual SSE generator and broker with publisher-shaped Vault,
  definition, sessionless run, tunnel-quality and session events; unknown events
  stay Owner-only and Editor/Viewer remain scoped.
- PERMISSIONS-017: per-subject Web Push subscription storage and badge/delivery
  filtering; message authorship remains the signed subject. Callback source
  enrichment now follows the same instance operation scope.
- PERMISSIONS-018: RemoteAccess rendered with the actual authorization provider;
  paired/unpaired Member pairing cannot submit, transport works and Owner pairs.
- PERMISSIONS-019: real skill diagnostics/clear storage and read-only SQL including
  CTEs; Member admitted, lower roles denied, SQL writes/raw Vault secrets denied.

Known by design: Show publication policy is unchanged; inbox read markers and
Workbench toggles retain their existing shared semantics. Memory principal,
terminal attachment identity, Vault proofs, IM admin and protected mixed writes
are unchanged. Historical Member selection/session fallbacks remain supported;
new Task/Watch names still resolve the catalog. Local tests stub execution IPC
and provider/host effects after authorization; no tenant or live provider result
is claimed. Post-merge acceptance still needs an authorized real Member browser
conversation and live transport/session updates in the regression instance.

Completed-marker repair uses the existing key and only released versions 1/2
with a valid absolute completion time, exact instance/kind and a matching ready
binding. Only Member records created/submitted and last updated strictly before
that original cutoff qualify. Completion retains the original timestamp and seals
version 3; later rows cannot reopen the opportunity. Missing/corrupt/unknown or
sealed provenance, equal-second/later row clocks, another pairing, contradictory
claims and terminal work remain unchanged. Ambiguous historical work may therefore
remain denied; this repair does not guess identity or authorize recovery by
re-pairing. Other roles do not enter the completed-marker repair.

The cutoff attributes the legacy row and its remote authorization snapshot; it
is not evidence that every byte of the row stayed unchanged. Released user edits
that replace `resource_user_context` advance `updated_at`; timestamped lifecycle
writes and new creation/submission clocks also exclude post-cutoff work.
Diagnostic stamps/clears and stable-Agent reference canonicalization
can preserve both the authorization snapshot and the lifecycle clock. Such rows
remain eligible: under the shared writer reservation, repair preserves their
current metadata/columns and every original principal/claims field, adding only
the missing instance ID/kind to the snapshot. No complete mutation history is
claimed. Soft deletion can also preserve `updated_at`; repair does not revive it.
Execution still applies existing lifecycle, Agent and pairing rules.

First review round: one findings-bearing head (`02b96d475`), one root cause
(released deferred Member compatibility). The repair and stale Member Project/Vault
expectations have 474 focused regression passes, including the negative marker,
row, binding and terminal-work cases. Project/Resource ACL rows and protected
Vault approval requirements remain intact.

Focused local evidence: 493 Python tests across permissions, Agent/Project/session
ACL consumers, messages/notifications, forks and diagnostics; 188 retained
Skills/terminal/Memory-admission/Permissions scenarios; 32 Show sharing cases;
6 RemoteAccess render tests; changed-file Ruff and production UI build. An
additional broad Show run was interrupted during unrelated runtime preparation
network I/O after three cases; it is not counted as passing. Targeted sharing
coverage above replaces that irrelevant preparation path. CI supplies the full
repository gate. All state is test-owned; execution/provider IPC is stubbed.


## Review circuit-breaker diagnosis and resumption

Three findings-bearing heads were independently inventoried by PM:

- `02b96d475`, review `5237360393`, thread `PRRT_kwDOPbFPYs6jaBH1`:
  Member was omitted from legacy deferred binding and previously completed
  markers skipped the omitted rows. Fixed with the conservative proof above.
- `e6987dc74`, review `5237677043`, thread `PRRT_kwDOPbFPYs6jaoeb`:
  cutoff/provenance reads and ID-only updates were not one serialized decision.
  This repeats the deferred migration compatibility/safety class, so the lane
  stopped before editing or pushing. Thread `PRRT_kwDOPbFPYs6jaoeR` on this head
  separately identified stale Owner-only EN/ZH Skill-usage parser help.
- `e943feee7`, review `5238087139`, thread `PRRT_kwDOPbFPYs6jbZHm`:
  released diagnostic writers change Run metadata without advancing `updated_at`.
  This is the third head in the same provenance/safety class; the lane stopped
  before editing/pushing and PM independently diagnosed the proof invariant.

PM reproduced a lost concurrent Task metadata update with two real SQLite/WAL
engines and independently audited the call paths. Importer's migration file lock
only excludes other migrations; runtime writers remain possible, and
`engine.begin()` alone does not reserve SQLite's writer slot. The proof rule was
correct only if the marker, config/binding, row eligibility and seal were evaluated
against one serialized database state.

PM authorized resumption on 2026-09-17 with a shared transaction-boundary fix:
reuse `reserve_write_lock` at `migrate_legacy_deferred_resource_contexts` entry,
before every decision read, holding it through the caller's commit/rollback.
No per-table CAS, retry policy, new role model or migration state machine was
introduced. `_configured_resource_state` remains read-only with
`persist_migrations=False`, preserving config-before-database lock order and
avoiding recursive bootstrap. The terminal/idempotent path also takes this short
writer reservation.

Callers audited: importer data migrations, post-stamp migrations, remote-access
pending migration and authorization bootstrap all use caller-owned transactions.
The initial released schema already supplies `agent_sessions.id` needed by the
existing-transaction no-op reservation. Real initial-schema new/writer/savepoint
fixtures and existing pre-Show/unversioned stamp tests cover that path; a successful
repair inside an existing writer/savepoint remains rollbackable by its caller.

Deterministic real two-connection tests cover each of Task, Watch, Run, Delivery,
binding and marker. A peer attempting writes after migration reads is refused
until the migration transaction completes, and its later legitimate update
survives. A peer committing first is read after reservation, so now-ineligible or
terminal rows and changed provenance remain untouched. All six after-read race
cases fail against the reviewed `e6987dc74` migration function. Existing cutoff,
identity, snapshot/hash, terminal-work and config-lock-order tests stay in the
regression set. Actual parser help is exercised in both languages and names
Member or Owner. This round passed 540 related regression tests, three existing
initial/unversioned/pre-Show schema cases, changed-file Ruff and the UI build. PM
independently inspected the shared boundary and ran 19 concurrency, legacy-schema,
rollback and parser-help cases, all passing, then authorized this round's push.
Subsequent exact-head review/CI remain required.

The third-head diagnosis corrected the overbroad "untouched row" requirement,
not runtime semantics. PM and lane independently used real SQLite producers:
`record_run_skip_reason` preserves the authorization snapshot and hold clock;
`_clear_transport_skip_evidence` removes its reason and reason-start timestamp.
After skip then clear, decoded metadata equals pristine metadata, so a
`last_skip_at` deny-list cannot establish historical immutability. Advancing that
clock would change released hold/transition behavior. The invariant above instead
protects authorization attribution while retaining legitimate current diagnostics.

PM authorized comments, contract and producer-to-consumer evidence on 2026-09-17;
no scheduler, schema, cutoff, binding, concurrency or role decision changed.
The production-writer audit found:

- Task/Watch user add/update re-snapshot authority and advance `updated_at`;
  full-row upsert callers also advance it. Internal runtime writes preserve the
  principal and advance lifecycle time where applicable.
- Run producers construct a new `TaskExecutionRequest` ID with current creation
  time, including definition fires, callbacks and atomic completion/escalation
  outboxes. A duplicate callback returns its existing child. Requeue and metadata
  merge callers advance time; they do not silently replace old remote authority.
- UI Delivery submission builds verified metadata and sets current submission
  and update clocks. Retry either inserts a new Delivery or records history on
  retained input through timestamped CAS; it does not replace its snapshot.
- Legacy Task/Watch file imports emit empty metadata; task-request import retains
  only `ok`. Watch runtime import carries process-state entries whose actual
  producer contains no remote authorization snapshot. These are insert paths,
  not edits that replace old authority while retaining old clocks.
- Diagnostic skip/clear, Agent reference pin/rename, soft deletion and Delivery
  dedupe normalization do not change `resource_user_context`. Agent catalog
  identity and existing archived-reference execution policy are preserved.

Focused tests use real skip/clear, Task/Watch user updates, Run/Delivery lifecycle
writes and stable-Agent pin/rename before repair. They assert exact retained
principal/claims plus only two binding additions, current nonauthorization
metadata/columns, Harness admission and queued-Delivery execution recheck,
post-cutoff refusals and sealed idempotence. Existing concurrency, terminal/hash
and conservative negative-provenance tests remain required. This evidence makes
no claim to recover unrecorded mutation history or support forged database files.
This round passes 10 new producer/consumer cases, 160 combined resource-permission
and signed Member scenarios (including the existing concurrency/provenance matrix),
and five retained runtime/Agent regressions. Changed-file Ruff and diff whitespace
checks pass. PM independently inspected the complete three-file diff and PR ledger, ran all
10 new producer/consumer cases successfully, and compared the production AST
(excluding comments/docstrings) with `e943feee7`: executable logic is identical.
On 2026-09-18 PM approved explanatory closure and this comments/contract/tests
push. Another exact-head review and complete CI remain required.
