# Organization-to-Master Conservative Execution Gate

Status: Approved for the `org` to `master` integration release on 2026-08-04.

## Background

PR #1162 carries Organization principals into the local Workbench and Resource
ACL model. The Agent runtimes are intentionally shell-capable and can mutate
their own environment, so Agent environment variables cannot authenticate a
remote caller or turn persisted identity into durable execution authority.

The existing Organization-aware Harness plan already defines stored principal
data as provenance, not bearer authority, and requires an authoritative local
entitlement mirror before remote recurring execution can be enabled.

## Approved Release Boundary

- Keep Organization management, Project and Resource ACL management, and
  authorized Project, Session, message, and history reads.
- Keep trusted-local Workbench, CLI, Task, Watch, Agent, terminal, and file
  behavior unchanged.
- Keep IM-triggered Agent behavior unchanged.
- Make every remote Workbench capability read-only with respect to local Agent,
  terminal, file, and Harness execution.
- Reject remote Task and Watch creation or update at the definition service
  boundary when an explicit remote `AuthorizationContext` is present.
- Suspend previously persisted remote-origin Tasks and Watches before any Agent,
  command, or waiter process starts.
- Retire previously queued remote Workbench Deliveries before they can be
  claimed by a durable Agent turn.
- Treat persisted `resource_user_context` only as provenance indicating remote
  origin. It is never sufficient authority for autonomous execution.

## Enforcement Points

1. The signed HTTP session projects remote `can_chat`, terminal, file, and
   system execution capabilities as false.
2. The UI server rejects remote message dispatch, Show-event Agent dispatch,
   Agent-definition and instruction mutations, model/backend mutations and
   runtime operations, service control, local installers, terminal sockets,
   file operations including Show Page icon uploads, and Harness definition
   mutations before invoking their underlying services.
3. The durable Delivery owner retires remote-origin queue entries before the
   FIFO claim that starts an Agent turn.
4. Task and Watch stores reject explicit remote definition writes.
5. Task and Watch executors detect persisted remote provenance and atomically
   disable the definition with `remote_autonomous_harness_disabled` before any
   process spawn or Agent dispatch.

## Deferred Phase 2

Remote Agent and autonomous Harness execution stay disabled until both are
available:

- server-mediated Agent tools whose caller identity cannot be removed or forged
  by the Agent process; and
- an authoritative local entitlement mirror that revalidates current Instance
  revision, membership, role, groups, Project access, and Resource ACLs at every
  launch and resume boundary.

No opaque token passed through the current Agent environment is considered a
substitute for either dependency.

## Validation

- Remote Workbench message and Show dispatch do not reserve or start a turn.
- Remote terminal and file requests fail closed; trusted-local requests retain
  existing behavior.
- Remote service control, installers, Agent-definition mutations, model/backend
  mutations and probes, and Show Page icon writes fail before reaching their
  local runtime or filesystem services.
- Paired Project synchronization never forwards the device secret through an
  HTTP redirect.
- Remote Task and Watch add/update calls produce no definition write.
- Existing remote-origin Tasks and Watches are disabled before command, waiter,
  or Agent dispatch.
- Existing queued remote Workbench Deliveries are retired before FIFO claim;
  later local queue entries remain runnable.
- Focused backend tests, frontend build, Ruff, lock validation, and diff checks
  pass before the PR is pushed for a new current-head review.
