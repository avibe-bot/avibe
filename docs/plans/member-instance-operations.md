# Instance Member operations

Status: approved behavior contract, implementation in progress (2026-09-17).
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
   apply where they also apply to Owner. IM binding/admin is a separate identity
   system and is not elevated by a cloud Instance Member grant.
4. **Preserve other roles.** Editor, Viewer, unauthenticated and invalid-context
   behavior remain unchanged. Existing restrictive resource rules still apply to
   those roles. Preserve CSRF, signed identity validation, resource existence,
   Agent enabled/available checks and other ordinary execution preconditions.
   Missing Agent catalog names are not made valid merely for apparent parity.
5. **Effect-based policy, bounded implementation.** Evolve the existing central
   capabilities and route policy. Member operational permission should follow
   that common decision, while access/identity effects retain explicit gates.
   Do not default-allow unknown APIs, redefine Owner, or rebuild the complete
   four-role policy framework. Registered routes must remain deliberately
   classified and covered by the existing complete-router invariant.

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
- [ ] Implement and validate in the isolated task worktree.
- [ ] Complete exact-head review and CI; independently verify the final diff.
- [ ] Present the concrete merge-ready PR and residual acceptance steps.
