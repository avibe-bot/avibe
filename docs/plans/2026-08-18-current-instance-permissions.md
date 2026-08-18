# Current-instance Permissions

## Background

Issue #1507 replaces the embedded Organization administration surface with a
Permissions page for exactly the paired Avibe instance. Backend PR #239 is the
instance and Project wire authority; this implementation targets its merged
head `8bf0024d87d1cb6a64be31b8a8068e89162a9153`.

Issue #1522 extends that boundary to Show Page Workspace access. Backend PR
#251 is the Resource ACL wire authority and is deployed to Production. Link
access remains the local `ShowAccess` aggregate delivered by #1501; Workspace
access is the exact-current-instance Resource ACL. Neither axis derives or
mutates the other.

## Contract

The browser calls only same-origin routes:

- `GET /api/permissions`
- `PUT /api/permissions/authorized-users`
- `PUT /api/permissions/projects/{project_id}/access`
- `GET /api/permissions/resources/{resource_kind}/{resource_id}/access`
- `PUT /api/permissions/resources/{resource_kind}/{resource_id}/access`

The local service obtains `backend_url`, `instance_id`, and `instance_secret`
from `V2Config.remote_access.vibe_cloud.runtime_credentials()`. It never accepts
an instance or Organization identifier from the browser. Server-to-server calls
use `X-Vibe-Device-Secret` against:

- `GET /api/v1/instances/{instance_id}/permissions`
- `PUT /api/v1/instances/{instance_id}/permissions/authorized-users`
- `PUT /api/v1/instances/{instance_id}/permissions/projects/{project_id}/access`
- `GET /api/v1/instances/{instance_id}/permissions/resources/{resource_kind}/{resource_id}/access`
- `PUT /api/v1/instances/{instance_id}/permissions/resources/{resource_kind}/{resource_id}/access`

Reads require the existing authenticated instance session. Mutations require
`AuthorizationContext.can_manage_instance` both in the central HTTP policy and
again in the handler. Backend `local_mutation_allowed` remains an independent
write gate for Cloud-owned policy.

Resource writes contain `access_level`, `group_ids`, and
`if_match_revision`. The browser also sends the local-only
`if_match_instance_id` pairing precondition; the local service validates and
strips it before Backend contact. Resource responses must match the requested
instance, kind, and ID exactly.

## Show Page Ownership

The current paired instance, never a browser claim, owns every Show Page in
that instance. The shared local ownership fence has four states:

- `unmanaged`: no authoritative pairing exists; retain standalone compatibility.
- `personal`: the exact current instance is authoritatively Personal.
- `organization`: the exact current instance and Organization ID are known.
- `organization_pending`: the instance is known to be Organization-owned, but
  its exact Organization ID is temporarily unavailable.

The last known exact instance/Organization binding is persisted in SQLite and
is used only while it still matches the current paired instance. A re-pair does
not inherit the old binding. Resolution may contact Backend, but it happens
outside SQLite transactions and failure never blocks Show Page creation or
runtime.

Every new or existing Show Page passes through one idempotent reconciliation:

- missing policy + Personal creates a private Personal policy;
- missing policy + Organization creates a private policy for the exact
  Organization;
- missing policy + Organization pending remains private and pending;
- a null Organization policy adopts the exact Organization without changing
  owner/audit data, ACL level, groups, policy revision, or applied revision;
- the same Organization is unchanged;
- another Organization, or an Organization policy on a Personal instance, is
  an integrity conflict and is never overwritten.

Pending, conflicting, and mismatched concrete Show Page policies fail closed
for non-owners across use, management, owner-control, and list filtering. The
Instance Owner may still inspect and recover them. Link access mode, share ID,
Limited emails, revision, and availability are outside this reconciliation.

## State Model

- Live instance-managed: owner-capable sessions can edit users/groups and
  Project policy with explicit revisions.
- Live Cloud-managed: readable but locally immutable; the only handoff is an
  ordinary Avibe Cloud link, never Management OAuth.
- Offline: a sanitized last-known projection, bound to the exact paired
  instance ID, may be served only after connectivity or Backend availability
  failures. Credentials are never cached.
- Applying: projection and Project sync status are rendered explicitly.
- Conflict: the editor draft remains open, authoritative state is refreshed,
  and retry is explicit with the refreshed revision.
- Organization pending: Show Page Workspace access is unavailable and private;
  it never appears Personal merely because the exact binding is unavailable.
- Ownership conflict: the mismatched policy remains untouched, non-owner use is
  denied, and the Share UI exposes a diagnosable unavailable state.
- Denied: session authorization and paired-credential failures are not treated
  as offline or empty data.
- Empty: a successful live projection with no access entries or Projects is a
  real empty state.

## Implementation

- Add a cohesive paired-instance Permissions client/cache and narrow UI routes.
- Add neutral frontend authorization types, API client, and a two-tab
  Permissions page.
- Show Permissions immediately above Advanced Settings on desktop and mobile
  for Personal and Organization instances.
- Preserve #1498 local Show Page Private/Limited/Public policy while removing
  the independent Cloud Management OAuth/resource-proxy control.
- Reconcile Show Page ownership in the shared storage/service path and expose a
  separate neutral Workspace section in the Share popover.
- Map Workspace `Private`, `Organization`, and `Selected groups` to wire
  `private`, `public`, and `scope`; retain already-bound archived groups while
  preventing new archived selections.
- On a Resource revision conflict, refresh the authoritative resource while
  preserving the browser draft and any newly observed bound archived group.
- Constrain the Share popover to Radix's available height and make it vertically
  scrollable on desktop and mobile.
- Delete the obsolete Organization UI, Cloud-management OAuth/cookies/proxies,
  scenarios, tests, and stale plan after the replacement path is covered.
- Keep English and Chinese copy in exact key parity.

## Verification

- Focused Python API/client/cache tests and authorization policy tests.
- Focused Show Page ownership/reconciliation tests covering new, legacy,
  idempotent, offline, pending, Personal, same-Organization, and conflict paths.
- Focused Vitest coverage for navigation, authority, offline, applying, denied,
  empty, and draft-preserving conflict behavior.
- Focused Vitest coverage for Workspace modes, active and archived groups,
  Personal/pending/conflict states, stale-response fencing, and Link/Workspace
  independence.
- Closed-loop current-instance Permissions scenario catalog and harness cases,
  while retaining AUTH-SETUP-401 through AUTH-SETUP-404 for #1498.
- Ruff on changed Python, UI production build, `git diff --check`, and local
  Incus regression without resetting pairing or product state.

## Non-goals

- Organization switching or member/group administration.
- Managing another instance or accepting a caller-selected target.
- Cloud Management OAuth, management-token cookies, or embedded consent.
- Uploading #1498 local exact-email Show Page guest lists to Backend.
- Implementing future Cloud-owned per-Show-Page policy distribution.
- Transferring an explicitly bound resource between Organizations.
