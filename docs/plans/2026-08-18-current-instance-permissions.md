# Current-instance Permissions

## Background

Issue #1507 replaces the embedded Organization administration surface with a
Permissions page for exactly the paired Avibe instance. Backend PR #239 is the
wire authority; this implementation targets its merged head
`8bf0024d87d1cb6a64be31b8a8068e89162a9153`.

## Contract

The browser calls only same-origin routes:

- `GET /api/permissions`
- `PUT /api/permissions/authorized-users`
- `PUT /api/permissions/projects/{project_id}/access`

The local service obtains `backend_url`, `instance_id`, and `instance_secret`
from `V2Config.remote_access.vibe_cloud.runtime_credentials()`. It never accepts
an instance or Organization identifier from the browser. Server-to-server calls
use `X-Vibe-Device-Secret` against:

- `GET /api/v1/instances/{instance_id}/permissions`
- `PUT /api/v1/instances/{instance_id}/permissions/authorized-users`
- `PUT /api/v1/instances/{instance_id}/permissions/projects/{project_id}/access`

Reads require the existing authenticated instance session. Mutations require
`AuthorizationContext.can_manage_instance` both in the central HTTP policy and
again in the handler. Backend `local_mutation_allowed` remains an independent
write gate for Cloud-owned policy.

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
- Delete the obsolete Organization UI, Cloud-management OAuth/cookies/proxies,
  scenarios, tests, and stale plan after the replacement path is covered.
- Keep English and Chinese copy in exact key parity.

## Verification

- Focused Python API/client/cache tests and authorization policy tests.
- Focused Vitest coverage for navigation, authority, offline, applying, denied,
  empty, and draft-preserving conflict behavior.
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
