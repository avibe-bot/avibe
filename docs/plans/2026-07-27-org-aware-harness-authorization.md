# Organization-Aware Harness Authorization

Status: Proposed - owner approval required before implementation

Issue: [#1058](https://github.com/avibe-bot/avibe/issues/1058)

## Decision gate

This document is the Phase 1 authorization contract. Route, UI, storage, and
service enforcement changes MUST NOT start until the instance owner explicitly
approves this model on the design PR. Approval must cover the decisions in
"Decisions requiring approval" below, not only the general goal.

## Background

Harness Task and Watch definitions and their Runs are local execution resources.
Today the `/harness` surface is owner-only, and the underlying services do not
have an organization-aware authorization contract. Simply weakening the route
gate would expose prompts, commands, local paths, callback Sessions, and captured
output without a service-layer boundary.

This design builds on the existing authorization layers:

- `AuthorizationContext` supplies the signed instance role and organization
  identity. Instance roles remain ordered `owner > editor > viewer`.
- Project access narrows the instance role through
  `get_effective_project_role()` and never elevates it.
- Resource ACL grants use of an Agent, Vault secret, Skill, Show Page, or, after
  this work, a Task or Watch. A resource grant never grants management authority.
- The use-versus-manage capability split from #1055 remains authoritative.
  Harness consumes its finalized use checks for referenced resources and does
  not introduce a parallel capability model.

The service layer is the security boundary. HTTP routes, CLI handlers, frontend
controls, SSE events, and run-graph projections are callers of the same checks.

## Goals

- Allow organization viewers to inspect authorized workflow status, history,
  and sanitized output.
- Allow organization editors to operate authorized workflows without gaining
  access to their configuration or referenced resources.
- Keep definition configuration and ACL management owner-only.
- Re-evaluate authorization at invocation, execution, callback, event, and read
  time so a launch-time grant is not durable access.
- Fail closed for legacy, unscoped, cross-project, or incompletely attributed
  records.

## Non-goals

- Changing protected Vault approval, plaintext delivery, or signing rules.
- Granting organization members direct shell, terminal, file, or system APIs.
- Publishing prompts, commands, schedules, paths, resource selectors, Session
  identifiers, logs, or output to the hosted control plane.
- Giving individual Runs separately managed ACL policy in the first version.

## Authorization model

### 1. ACL attaches to definitions and Projects: both

Task and Watch definitions become Resource ACL subjects with resource kinds
`harness_task` and `harness_watch`. Their stable definition ID is the resource
ID. A definition ACL is a use grant, not a role grant.

Each organization-visible definition also has one primary `project_id`. Its
effective Harness role is:

1. the caller's signed instance role,
2. narrowed by the effective primary Project role,
3. narrowed to no access unless the caller may use the Task or Watch ACL, and
4. narrowed by every additional referenced Project or Session required by the
   requested operation.

The layers are intersected; none can elevate another. For example, a public
Task in a viewer-only Project is viewer-only, and an editor Project binding does
not expose a private Task that the editor cannot use.

New definitions created in an organization context are registered using the
existing private-resource ownership contract. Local definitions without an
organization policy remain local-private. A legacy definition that cannot be
resolved to exactly one primary Project is owner-only until an owner explicitly
assigns it; no Project is inferred from `cwd` or prompt text.

Definitions may reference a target Session and a callback Session. The primary
Project is stored directly rather than inferred on every request. Every Session
reference must still resolve to a Project at authorization time. Non-owner
operations require access to all such Projects. An owner may configure a
cross-Project workflow, but it is not usable by a member who lacks the required
role in any referenced Project.

### 2. Runs inherit authorization; they do not have independent ACL

A Run is an execution record, not a separately shareable resource in this MVP.
It has no `harness_run` policy that could diverge from the workflow that created
it. Instead, each Run records immutable, non-secret authorization provenance:

- definition kind and ID, when definition-backed;
- launch Project ID and all referenced Project IDs;
- target, source, and callback Session IDs;
- initiating or activation principal identity and membership version;
- selected Agent ID/name;
- declared and actually used Skill, Vault secret, Agent, Show Page, and Project
  resource IDs;
- policy revisions used by the launch preflight; and
- whether dependency attribution and output classification are complete.

The provenance is a lookup manifest, not an authorization snapshot. Current
policies are evaluated on every request.

For a definition-backed Run, safe status visibility requires current viewer
access to the launch Project, referenced Sessions, and the current definition
ACL. Historical Runs remain bound to their launch Project even if an owner later
moves the definition. If the definition or its policy is deleted, non-owner Run
access fails closed rather than falling back to a stale policy snapshot.

A direct Agent Run without a definition derives its safe-envelope visibility
from its launch Project and target/source Sessions. Agent access is an output
dependency and an invocation gate, not a prerequisite for fail-safe status or
cancellation after that Agent is revoked. A legacy or direct Run with missing
Project/Session provenance is owner-only.

Parent and child Runs are authorized independently. Run-graph edges to an
inaccessible node are omitted, so a visible child cannot disclose a hidden
parent ID or vice versa.

### 3. Role and operation matrix

"Owner" below means the trusted local owner or signed instance owner capability,
not merely the owner field on a Resource ACL row. A Resource ACL owner who is an
instance editor receives editor operations only.

| Operation | Owner | Effective editor | Effective viewer | No match |
| --- | --- | --- | --- | --- |
| List definitions and safe summaries | Allow | Allow, filtered | Allow, filtered | Omit |
| Read definition status/history | Allow | Allow | Allow | 404 |
| Read sanitized Run status/history | Allow | Allow | Allow | Omit/404 |
| Read logs/result/output | Allow subject to redaction | Allow subject to dependency checks and redaction | Allow subject to dependency checks and redaction | Omit/404 |
| Create Task/Watch | Allow | Deny | Deny | Deny |
| Update/delete definition | Allow | Deny | Deny | Deny |
| Change schedule, prompt/message, command, cwd, Agent, Project, Session policy, delivery, callback Session, or ACL policy | Allow through the owning management surface | Deny | Deny | Deny |
| Manual run | Allow | Allow after dependency preflight | Deny | Deny |
| Pause definition/Watch | Allow | Allow | Deny | Deny |
| Resume definition/Watch | Allow | Allow after dependency preflight | Deny | Deny |
| Cancel queued/executing definition-backed Run | Allow | Allow with current editor access to its definition, launch Project, and writable Sessions | Deny | Deny |
| Cancel queued/executing direct Agent Run | Allow | Allow with current editor access to its launch Project and every writable target/source/callback Session Project | Deny | Deny |

Pause and cancel are fail-safe operations. An editor who still has editor access
to the definition and Project may stop work even when a referenced Agent, Skill,
or Vault grant has just been revoked. Resume and manual run can create side
effects, so they require the full dependency preflight. Direct Agent Run
cancellation has no definition ACL to check: Project and writable Session
authorization identify its operational boundary, and current Agent access is
deliberately not required to stop it.

Owner-only definition details are omitted, not merely disabled, in member
responses. In particular, prompts/messages, shell commands and argv, schedules,
`cwd`, Agent selection, Session policy, target/callback Session, delivery target,
resource selectors, and ACL policy are never included in viewer/editor
definition projections.

### 4. Invocation and referenced-resource checks

Manual run and resume require all of the following at the service layer:

- editor Harness role for the definition and its primary Project;
- editor access to every target/source/callback Session Project that the action
  can write to;
- current use authorization for the selected Agent;
- current use authorization for every declared or automatically loaded Skill;
- current use authorization for every declared Vault secret selector;
- the independent Vault protection, approval, grant, and keypair-signing rules;
  and
- current use authorization for any other resource resolved before launch.

Prompt parsing is not an authorization mechanism. Definitions store normalized
resource references, and Agent/Skill/Vault services append actual dependency
usage to the Run through a shared run-scoped recorder. A resource discovered
dynamically after launch is checked before use under the Run's execution
principal. A denial is not retried as the definition owner or trusted local
caller; the attempted use fails and the Run is canceled or fails with a safe
`authorization_revoked`/`resource_access_forbidden` code.

Every Run has an execution principal:

- a manual Run uses the current caller;
- a resumed Task/Watch uses the resuming caller as its activation principal for
  work enabled by that resume; and
- an owner-created definition that has never been activated by a member uses
  its stored owner principal for automatic triggers.

The stored principal context is provenance, not a permanent bearer grant. It is
bound to membership version/expiry, but the stored claims are never sufficient
to authorize a queue claim or resource use. Current Project and Resource ACL
revisions and current principal entitlement are checked before queue claim and
before each resource use. If the runtime cannot establish a still-valid
principal for autonomous member work, it suspends the definition and fails
closed instead of substituting local-owner authority.

#### Authoritative principal revalidation

Phase 2 requires a versioned, device-authenticated control-plane entitlement
mirror for every remote principal allowed to activate autonomous work. Each
record contains only authorization metadata: instance ID, subject/member ID,
normalized email, organization ID, active state, current effective instance
role, organization role, group IDs, membership version, control-plane revision,
and freshness deadline. It contains no Harness configuration, Project content,
or output. The control plane advances `membership_version` for every email,
instance-access, organization-role, group-membership, or active-membership
change. A mirrored record is valid for at most five minutes without a successful
refresh.

Queue claim and every dynamic resource use compare the Run's activation
`membership_version` with the current mirrored record. On a version change, the
runtime rebuilds authorization from the mirrored current roles/groups and
normalized email and re-evaluates the definition, Projects, Sessions, and
resources. Inactive membership, a lower role, changed email/domain, removed
group, or no longer matching ACL cancels or suspends the work. Applying an
entitlement revision publishes the same internal authorization-invalidation
event used by Project and Resource ACL changes.

The existing deferred `resource_user_context` snapshot and its authorization
refresh deadline are launch provenance only; they cannot satisfy this current
membership check. If the authoritative record is absent, stale, or cannot be
refreshed by its deadline, non-owner autonomous work fails closed. A new HTTP
request may supply fresh signed claims only when their membership version
matches the current mirrored record. This device-protocol dependency must land
before Phase 2 can enable editor-activated recurring Tasks or Watches.

Before callback or delivery, the service rechecks that the execution principal
can write to the callback/target Session's current Project. Revocation suppresses
the callback and records only a safe callback status.

### 5. Run reads and output redaction

Run access is field-sensitive and always re-evaluated:

1. The safe Run envelope requires current viewer access to the definition,
   launch Project, and referenced Sessions. It contains only ID, kind, status,
   timestamps, duration, and redaction state.
2. Logs, result text, stdout, stderr, error detail, message payloads, and output
   require current use access to every resource in the complete dependency
   manifest, in addition to envelope access.
3. If a dependency is inaccessible, deleted, unknown, or incompletely
   attributed, content fields are replaced by a structured redacted projection.
   They are never returned as `null` alongside a side channel containing the
   original bytes.

Project, Agent, and Skill use does not by itself taint otherwise sanitized output,
but current access to each is required. Revoking any of them immediately removes
content access while leaving only the safe envelope when its base authorization
still matches.

Vault use is stricter: Resource ACL authorizes use, not plaintext disclosure.
When a Run used a Vault secret, all captured prompt/message, stdout, stderr,
result, error detail, logs, and callback payload are Vault-tainted and are never
serialized by Harness HTTP, event, SSE, or direct-ID responses for any browser
role. The response exposes only safe status plus a redaction reason that names
the resource kind, never the secret name or value. A future explicit Vault reveal
flow may define a separate contract; Harness output cannot become that flow.

Redaction is applied in the central serializer and, where possible, before
persistence. Every surface uses that serializer:

- list, count, bootstrap, detail, logs, and output endpoints;
- Workbench and inbox events, SSE, and WebSocket payloads;
- activity banners, notifications, callbacks, and run-graph nodes; and
- direct-ID lookups and pagination/search totals.

Lists and counts are filtered before aggregation. Events for inaccessible Runs
are dropped. Events for visible but content-redacted Runs contain only the safe
envelope. Hosted resource-index publication is limited to Task/Watch ID, kind,
name/title, enabled/state, and ACL revision; it never includes execution output
or local configuration.

### 6. Revocation semantics

Revocation is evaluated against the Run's execution principal. Removing access
from an unrelated viewer changes that viewer's reads but does not cancel another
principal's Run.

Policy application publishes an internal authorization-invalidation event. The
Harness service uses indexed definition and Run dependencies to handle affected
work without waiting for the next page request. Versioned principal-entitlement
updates, including instance removal, role downgrade, organization removal, and
group-membership change, publish the same event and are compared by member ID
and membership version.

| State when execution-principal access is revoked | Required behavior |
| --- | --- |
| Enabled definition, no queued Run | Mark it `suspended_authorization`; do not enqueue automatic work until an authorized owner/editor explicitly resumes it. |
| Queued/deferred Run | Recheck immediately before claim. Atomically mark it canceled with safe reason `authorization_revoked`; do not spawn a process, invoke an Agent, or deliver a callback. |
| Executing Task/Agent Run | Immediately quarantine output and suppress events/callbacks, request cancellation through the existing Agent/session cancellation path, and finish as canceled with `authorization_revoked`. Effects completed before revocation are not rolled back. |
| Executing Watch waiter | Stop the managed waiter/process tree, quarantine output, suppress its follow-up, and suspend the Watch as `authorization_revoked`. |
| Completed Run | Keep the audit row unchanged. Every later list/detail/log/output request uses current policy; hide the row or redact content according to the current field-level rules. |

Authorization is checked at enqueue/request time, queue claim, process/Agent
spawn, each dynamic resource use, callback/delivery, event emission, and read.
This closes the race between an accepted action and an ACL update. If immediate
process termination fails, the Run remains quarantined and cannot publish output
while cancellation is retried.

## Service and storage contract for Phase 2

Phase 2 should introduce one cohesive Harness authorization/service boundary,
not duplicate role tests in routes. Public service methods accept an explicit
`AuthorizationContext` and operation, resolve Project/Session/resource policy in
one transaction where practical, and return already filtered/redacted domain
projections. Trusted local CLI/system calls use an explicit trusted-local
context; a remote-triggered background call must never gain trust because its
context was omitted.

Storage needs:

- primary `project_id`, ACL kind/ID, owner/activation principal metadata, and
  policy revision on Task/Watch definitions;
- a current, versioned principal-entitlement mirror populated through the
  device-authenticated control-plane protocol;
- an immutable Run authorization-provenance record;
- normalized definition and Run dependency rows so revocation can find queued
  and active work without scanning prompt/output JSON; and
- output classification/redaction state that is set before any event or
  callback can serialize Run content.

The finalized #1055 use-capability methods and `resource_access_service` are
consumed directly for Agent/Skill/Vault/Show Page checks. This design requires no
new field or capability method on `AuthorizationContext`; any implementation
discovery to the contrary must be called out as a `CONTRACT CHANGE` before it is
merged.

Route projections must cover list, count/bootstrap, detail, create, update,
delete, manual run, pause, resume, cancel, logs, and output. Hidden direct IDs
return 404. A visible resource with an insufficient action role returns the
established forbidden response. Frontend filtering and disabled controls mirror
the service response but are not enforcement.

## Verification plan for Phase 2

- Unit matrices for owner/editor/viewer/no-match/local across Task, Watch, and
  definition-backed/direct Run operations.
- Contract tests proving service entry points deny a missing or insufficient
  context even when called without an HTTP route.
- Reference matrices for Project, Session, Agent, Skill, Vault, callback, parent,
  and child dependencies, including no privilege elevation.
- Revocation tests for dormant definitions, queued/deferred Runs, active Agent
  Runs, active Watch processes, callback delivery, and completed Run reads,
  including instance removal, membership-version change, normalized email and
  email-domain change, group removal, stale entitlement state, and offline
  refresh failure.
- Serialization tests for list/count/bootstrap/detail/log/output/direct-ID,
  activity/event/SSE/WebSocket, and run graph surfaces. Seed sentinel prompt,
  path, secret, and output strings and assert none cross a denied/redacted
  response.
- Scenario coverage for one owner-created scoped Task/Watch, editor run/resume,
  viewer history/output, no-match omission, dependency revocation, and
  cancellation.
- Real staging E2E for one scoped Task/Watch/Run lifecycle after unit and
  contract gates pass. No staging or Incus work occurs in design Phase 1.

## Acceptance-criteria mapping

| Issue acceptance criterion | Design decision and evidence |
| --- | --- |
| AC1: documented model agreed before route changes | This document is the proposed contract. The design PR contains no route/service changes and explicitly requires owner approval before Phase 2. |
| AC2: checks for list/detail/create/update/delete/run/pause/resume/cancel/logs/output | The role matrix and service contract assign every operation and require one service boundary for all projections. Phase 2 supplies automated evidence. |
| AC3: referenced Project/Agent/Skill/Vault ACL without elevation | Invocation intersects Project, definition, Session, and every referenced resource check; dynamic use is checked under the execution principal and can never fall back to owner. |
| AC4: queued/active/completed revocation defined and tested | The revocation table defines dormant, queued, executing Task/Agent, executing Watch, and completed behavior. Current principal entitlement is versioned and authoritative rather than a deferred claim snapshot. The verification plan names each test layer. |
| AC5: sensitive output absent from list/event/SSE/direct-ID | The field-sensitive central serializer, complete dependency manifest, Vault taint rule, filtered counts, and event suppression cover every named surface. Sentinel tests are required. |
| AC6: owner/editor/viewer/no-match automated matrices | The role matrix is the expected result table; the verification plan requires Task, Watch, direct Run, and definition-backed Run matrices. |
| AC7: staging scoped lifecycle | Phase 2 ends with an owner-created scoped definition, editor operation, viewer read, revocation, and cancellation lifecycle against real staging. |

## Decisions requiring approval

The owner is asked to explicitly approve all of the following before Phase 2:

1. Adopt the recommended baseline: Task/Watch ACL is capped by current Project
   role; invocation additionally requires every referenced resource; Run reads
   re-evaluate current access.
2. Put Resource ACL on Task/Watch definitions (`harness_task`, `harness_watch`)
   and derive Run access instead of creating independently grantable Run ACL.
3. Give viewers safe status/history and sanitized output; give editors manual
   run/pause/resume/cancel; keep all definition creation/configuration/deletion
   and policy mutation owner-only.
4. Keep a safe Run envelope visible when base authorization remains, but redact
   content when any current dependency is inaccessible or attribution is
   incomplete.
5. Treat all output from a Vault-using Run as non-serializable through Harness,
   regardless of browser role.
6. Require the versioned control-plane principal-entitlement mirror before
   editor-activated autonomous work, including normalized email for email/domain
   Project bindings; stale or unavailable membership state fails closed after
   at most five minutes rather than relying on the deferred 12-hour claim
   snapshot.
7. Cancel/suspend queued and active work when its execution principal loses
   access; quarantine output immediately; retain completed audit rows but
   re-evaluate every future read.
