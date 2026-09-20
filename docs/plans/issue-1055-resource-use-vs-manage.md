# Issue 1055: Resource Use vs Management

## Background

Organization Resource ACL already filters Agents, Skills, Vault secrets, and
Show Pages at several domain-service boundaries. Instance authorization still
classifies much of the resource surface as owner-only, while the resource ACL
service maintains a second authorization context and does not consistently
enforce the Instance role when called directly.

## Goal

Make resource use a first-class Instance capability, separate from owner-only
resource management. Authorized editors may discover and use Agents, Skills,
and Vault secrets; viewers may only read the approved Show Page projection.
Every resource service combines that Instance capability with the current
effective Resource ACL.

## Solution

- Add a resource-kind-aware `AuthorizationContext.can_use_resource()` contract
  and expose its per-kind projections to the Web UI.
- Use `AuthorizationContext` as the identity passed through HTTP, SSE,
  WebSocket, and resource services instead of reparsing a parallel request
  context.
- Enforce owner-only creation and management in resource services, and require
  both the Instance use capability and effective Resource ACL for discovery,
  selection, session creation, and invocation.
- Project those rules onto HTTP routes and UI navigation/actions.
- Publish `authorization.changed` after a narrowing Resource ACL sync and clear
  Agent, Skill, Vault, and Show Page read caches in open clients.

## Scope Boundary with Issue 1057

This change re-evaluates the currently applied Resource ACL whenever a resource
is selected or invoked, and invalidates open-client resource caches when an ACL
is narrowed. Signed-claim revalidation, authorization watermarks, and
cross-host active-session invalidation remain owned by issue 1057.

## Todo

- [x] Add the central resource-use capability contract.
- [x] Unify resource services on `AuthorizationContext` and enforce use/manage.
- [x] Update route and UI projections.
- [x] Invalidate open-client caches after authorization narrowing.
- [x] Add owner/editor/viewer matrices and direct-service security tests.
- [x] Run focused tests, Ruff, and the UI build.
