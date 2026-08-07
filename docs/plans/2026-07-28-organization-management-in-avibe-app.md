# Organization Management in Avibe App

Status: Product decisions resolved; ready for implementation planning
Date: 2026-07-28
Last updated: 2026-07-29
Primary repository: `avibe-app`
Control-plane repository: `avibe-backend`

## Summary

Avibe App will become the primary user interface for Organization permission
management. Users will manage Organization members, groups, Instance access,
Project access, and Organization Resource ACLs without leaving the current
Avibe App.

This is a frontend ownership change, not a control-plane ownership change:

- `avibe-app/ui` owns the Organization management experience;
- `avibe-backend` remains the source of truth for Organization identity, data,
  roles, access policies, revisions, and authorization decisions;
- existing `avibe-backend` Organization, Instance, Project, and Resource APIs
  remain the business API surface;
- the local runtime proxies authenticated management requests but does not
  duplicate Organization state or make Organization authorization decisions;
- local SQLite continues to store only the applied Project and Resource policy
  projections needed to enforce access on that runtime.

The business services remain reused, but five backend contract gaps must be
closed before the frontend can be complete:

- a safe user-authentication path for Avibe App to call the existing Cloud APIs;
- member-safe Organization/Instance projections, including the caller's group
  summary without exposing another member's email;
- revision-based optimistic concurrency for member and group mutations;
- Organization owner/admin authorization for Project management on
  Organization-bound Instances; and
- a safe per-Instance Project/Resource synchronization summary.

Existing Cloud Console APIs are cookie-authenticated on the `avibe-backend`
origin and cannot be called reliably from localhost, managed Instance hostnames,
and custom hostnames without a Bearer-capable management session.

## Background

The current Organization Console lives in `avibe-backend`. It already supports:

- Organization membership and `owner | admin | member` roles;
- Organization groups and group membership;
- Organization-bound Instances and Instance access entries;
- per-Instance Project access policies;
- Organization Resource ACLs for Agents, Vault secrets, Skills, and Show Pages;
- versioned desired-versus-applied synchronization for Project and Resource
  policies.

Avibe App currently exposes only fragments of this model. For example, the
Agents page can onboard missing Agents privately and then links to the hosted
Resource Access console. Users must switch products and navigation systems to
finish the permission workflow.

Moving the frontend into Avibe App makes permission management part of the
runtime the user is already operating. It also lets Instance, Project, and
resource status appear in the context where those objects are used. The move
must not create a second control plane or let a trusted-local request impersonate
an Organization administrator.

## Goals

- Provide one Organization management area inside Avibe App.
- Let Organization owners and admins manage members and groups.
- Let Organization owners/admins and the relevant Instance owner manage
  Instance access.
- Let Organization owners/admins and the relevant Instance owner manage Project
  access.
- Let Organization owners/admins manage Organization Resource ACLs through the
  existing control-plane management contract.
- Preserve `avibe-backend` as the authoritative writer and validator for every
  Organization-level mutation.
- Reuse existing backend service logic, payloads, validation, error codes, and
  optimistic-concurrency revisions.
- Keep Project and Resource contents local. Cloud receives only the existing
  safe descriptors and policy metadata.
- Make pending, offline, conflict, and failed policy application visible rather
  than reporting a Cloud write as already effective locally.
- Support managed Instance hostnames and active custom hostnames without relying
  on third-party cookies.
- Make authorization revocation converge across HTTP, SSE, WebSocket, resumed
  streams, and every active hostname for an Instance.

## Non-Goals

- Moving Organization persistence or policy evaluation from `avibe-backend` to
  the local runtime.
- Copying the complete Cloud Console application into Avibe App.
- Reimplementing Organization business rules in Python or TypeScript inside
  `avibe-app`.
- Organization billing, plan management, invoices, or seat accounting.
- Organization domain verification or custom-hostname lifecycle management.
- Creating, deleting, pairing, or regenerating credentials for Instances in the
  first delivery. The Instances page manages access to existing Instances.
- Organization deletion or ownership transfer in the first delivery. Member
  role management must still protect the current owner.
- Custom roles, per-capability role editors, explicit deny rules, nested groups,
  or group inheritance.
- Individual Resource ACL grants. Resource access remains private, whole
  Organization, or selected Organization groups.
- Uploading Project paths, prompts, messages, commands, secret values, execution
  output, or local logs to the control plane.
- Removing the existing Cloud Console before Avibe App reaches verified feature
  parity.

## Resolved Product Decisions

The following decisions remove the remaining ambiguity from the implementation
contract.

1. **Organization routes use an identity gate, not an administrator gate.**
   Every `/admin/organization` route requires a valid, user-present Cloud
   management session and active Organization membership. Step-up establishes
   the Cloud subject; it does not establish an owner/admin role. Ordinary
   members and Instance owners may enter the routes they are authorized to
   read or manage. Page and object capabilities are evaluated separately.
2. **Member and group writes use optimistic concurrency.** Last-write-wins is
   not acceptable because the member editor and group editor mutate the same
   membership relation. Member and group responses carry integer revisions;
   every update supplies the matching revision; stale writes return `409` and
   are never retried automatically.
3. **Owner-only is a first-class Project editor choice and a derived wire
   state.** The UI choices are `inherit`, `owner_only`, and `restricted`.
   `owner_only` serializes as `mode=restricted` with no bindings. A restricted
   policy with no bindings always renders as Owner-only; the UI rejects saving
   the Restricted choice until at least one binding exists.
4. **Selected-group Resource access carries `group_ids`.** The request is
   `{ access_level, group_ids, if_match_revision }`. `scope` requires at least
   one group from the same Organization; the UI sends a unique list and permits
   only active groups to be newly selected. `public` and `private` require an
   empty group list.
5. **Member-safe views do not disclose Resource policy composition.** Ordinary
   members do not receive the Organization Resource management inventory or
   arbitrary audience-group names. Normal capability surfaces may say that a
   resource is available Organization-wide or through one of the caller's own
   matching groups. They never reveal non-matching groups.
6. **Instance synchronization uses the worst active child state.** The severity
   order is `Error > Offline > Applying > In sync`. Deleted or tombstoned
   Projects and Resources are ignored. An Instance with no active managed
   descriptors shows `No managed policies`, not `In sync`.
7. **Silent reauthorization is a one-attempt convenience, not a fallback
   loop.** It is attempted only for an expired or missing local management grant
   while a valid Cloud browser session can preserve the already-bound subject.
   An explicit logout, subject mismatch, revocation, missing Cloud session,
   `login_required`, `consent_required`, invalid callback state, or token
   validation failure requires explicit sign-in.
8. **Subject mismatch is a distinct terminal state.** The stable code is
   `cloud_management_subject_mismatch`. The local server immediately clears the
   mismatched grant; the UI explains that the Avibe session and Cloud account
   differ and requires explicit re-entry with the same account. It never
   silently switches subjects. On trusted loopback, the first successful Cloud
   login establishes the subject because no remote subject existed beforehand.

## Product Model

Organization authorization consists of independent layers. No layer may elevate
another:

```text
effective operation
  = signed Instance role
  intersected with Project access
  intersected with Resource ACL
  intersected with resource-specific safety rules
```

The layers answer different questions:

| Layer | Question | Values |
| --- | --- | --- |
| Organization role | Who may govern the Organization? | `owner`, `admin`, `member` |
| Instance access | What may this principal do on one Avibe Instance? | implicit owner, `editor`, `viewer` |
| Project access | What may this principal do in one Project? | inherited, restricted `editor`, restricted `viewer`, owner-only |
| Resource ACL | Which Organization audience may use one resource? | private, Organization-wide, selected groups |
| Resource management | Who may change or delete the resource definition? | separate service-level management check |

Within one Instance, the implicit Instance owner has all Instance-level
capabilities, including settings, files/terminal, access management, and Project
policy management. An Instance `editor` inherits viewer access and may perform
only explicitly editor-gated collaboration actions, such as sending messages
and invoking editor-level resources when their Project and Resource policies
also allow it. An editor cannot use owner-only system/files/terminal surfaces or
change permission policies. An Instance `viewer` is read-only: it may read
authorized Instance/Project data and view viewer-level surfaces such as an
allowed Show Page, but cannot make state-changing calls.

These Instance roles are separate from Organization `owner | admin | member`:
an Organization owner/admin governs Organization policy, while an Instance owner
governs only the Instances they own unless they also hold an Organization
management role. “All Instance-level capabilities” does not bypass Project,
Resource ACL, resource ownership, Vault approval/signing, or other independent
safety checks.

Resource use never implies resource-definition management or Organization ACL
management. A user may be allowed to invoke an Agent, load a Skill, request a
Vault secret, or open a Show Page without being allowed to edit, delete, or
share that object. Local resource-definition ownership remains separate from
the Organization owner/admin authority required by the Cloud Resource API.

Vault Resource ACLs do not bypass Vault protection, approval, signing, or
plaintext-delivery rules. Show Page Organization access remains separate from a
public internet share link.

## Information Architecture

Organization management is a first-level destination in the existing Control
Panel. It is not a new landing page and does not add Organization pages to the
Workbench capability navigation.

```text
/admin/organization
  /overview
  /members
  /groups
  /groups/:groupId
  /instances
  /instances/:instanceId/access
  /instances/:instanceId/projects
  /resources
```

`/admin/organization` redirects to `/admin/organization/overview`.

The whole route tree requires a valid Cloud management identity and active
membership in the selected Organization. It does not require the caller to be
an Organization owner/admin. Route loaders then apply the following capability
gates:

| Route | Read gate | Write gate |
| --- | --- | --- |
| Overview | Any active member; member-safe projection for non-managers | None |
| Members | Owner/admin sees the directory; other members see self only | Owner/admin, subject to owner protections |
| Groups | Any active member sees group metadata and own membership | Owner/admin |
| Instances | Backend-visible Instances only | Owner/admin or owner of the target Instance |
| Projects | Backend-visible Projects under an authorized Instance | Owner/admin or owner of the target Instance |
| Resources | Owner/admin only | Owner/admin only |

An unauthorized child route returns the localized unavailable/forbidden state;
it does not redirect to login when the management identity is already valid.

Project access is nested under an Instance because a Project is a local-runtime
resource whose safe descriptor and desired policy are mirrored through that
Instance. The UI may offer an Organization-wide Project search later, but it
must retain the Instance boundary in URLs and API calls.

An Organization switcher appears in the Organization header when the signed-in
user belongs to more than one Organization. It is omitted for a single
Organization. The selected Organization ID is kept in the route or feature
state, not written into global runtime configuration.

### Overview

The overview shows:

- Organization name and the current user's Organization role;
- member-safe aggregate active-member/active-group counts, with invited,
  removed, and archived breakdowns shown only to managers;
- counts for Instances and Projects visible to the caller and, for managers,
  the managed Resource inventory;
- Instances with pending, offline, or error policy synchronization;
- links into Members, Groups, Instances, Projects, and Resources;
- a member-safe summary when the caller is not an Organization manager.

It must not use fixture counts. Every value comes from Cloud responses.

### Members

Owners and admins can:

- list active, invited, and removed members;
- invite a member by email with a role and initial groups;
- resend a pending invitation;
- change a member between `admin` and `member`;
- replace a member's Organization groups; and
- remove a member by setting status to `removed`.

Only the Organization owner can make ownership decisions. The first delivery
does not expose ownership transfer, so neither an admin nor the current owner
can assign the `owner` role from the generic member editor. Existing backend
owner-protection rules remain authoritative.

Members who are not Organization managers see only their own membership, role,
and group summary. They do not receive the full member directory or other member
email addresses from the local proxy.

Each member response includes `member_revision`. Role, status, and that member's
complete group set share this revision. Member PATCH/replace-membership requests
include `if_match_revision=member_revision`; a successful change increments the
member revision and the revisions of every group whose membership relation was
added or removed.

### Groups

Owners and admins can:

- create a group with name, description, color, and initial members;
- update name, description, and color;
- replace group membership;
- archive and restore a group; and
- inspect where a group is referenced before archival.

Deletion is presented as archival because the existing backend `DELETE` route
archives a group. An archived group cannot be added to a new Instance, Project,
or Resource policy. Existing references remain visible and must fail closed when
they no longer produce a valid signed group claim.

Active members may read group name, description, color, status, and whether they
belong to the group. Only owners/admins receive the full member roster or member
identities. Each group response includes `group_revision`. Group metadata and
the complete group member set share this revision. Group PATCH/archive/restore
and replace-members requests include `if_match_revision=group_revision`; a
successful membership replacement increments the group revision and the member
revision of every member added or removed.

Both editor paths apply membership changes as relation deltas in one transaction
and lock all affected member and group rows in one shared deterministic order.
A change through either path therefore invalidates a stale editor on the other
path without overwriting unrelated membership relations or creating opposite
lock ordering. Timestamps are display metadata, not concurrency tokens.

### Instances

The list shows Organization-bound Instances visible to the current caller:

- display name and stable Instance ID;
- owner identity for managers, or `You`/a non-identifying owner label for a
  member-safe caller;
- effective public address;
- pairing and runtime status;
- access-entry count; and
- Project/Resource synchronization health.

The first delivery manages access to an existing Instance. It does not create,
delete, pair, regenerate pairing keys, or manage hostnames.

The Instance access editor supports these principal types:

- Organization group;
- individual email; and
- email domain.

Each access entry grants `viewer` or `editor`. The Instance owner is implicit,
owner-equivalent, and cannot be removed or represented as a normal entry.

### Projects

The Project page lists safe Project descriptors published by one Instance. It
never displays or sends the local Project path.

Each Project has one access rule:

- `inherit`: use the effective Instance role;
- `owner_only`: only the implicit Instance owner receives Project access; an
  Organization owner/admin may still manage the policy but does not gain
  Project use access from management authority alone; or
- `restricted`: apply one or more explicit email, email-domain, and
  Organization-group bindings with `viewer` or `editor`.

`owner_only` is a first-class UI selection but remains derived on the existing
wire contract:

| UI selection | Wire representation |
| --- | --- |
| Inherit | `mode=inherit`, empty bindings |
| Owner-only | `mode=restricted`, empty bindings |
| Restricted | `mode=restricted`, one or more bindings |

The editor never displays `Restricted, 0 bindings`. Any restricted response
with an empty binding list renders as Owner-only, and Restricted cannot be saved
until at least one valid binding exists.

Project access can only narrow the Instance role. An `editor` Project binding
does not elevate an Instance `viewer`.

Current backend Project management is limited to `instance.owner_user_id`.
Organization management is incomplete unless Organization owners/admins can
also manage Projects on Organization-bound Instances. The shared backend helper
must therefore authorize:

```text
Organization owner/admin for the Instance's Organization
OR the Instance owner
```

Personal-workspace Instances retain their existing owner-only behavior.

### Resources

The Resource page manages the safe Organization resource index for:

- Agents;
- Vault secrets;
- Skills; and
- Show Pages.

Harness Tasks and Watches are added only after their Organization-aware
authorization contract is implemented. Unknown resource kinds fail closed and
must not appear automatically.

The UI labels the three access levels as:

- `Private`;
- `Organization`; and
- `Selected groups`.

The existing wire value `public` means every active member of the Organization.
The UI must not label it `Public`, because that is easily confused with an
internet-visible Show Page. The wire contract remains `public | scope | private`
until a separately versioned protocol changes it.

The Resource ACL mutation payload is:

```json
{
  "access_level": "scope",
  "group_ids": ["grp_engineering", "grp_design"],
  "if_match_revision": 3
}
```

`scope` requires one or more canonical group IDs belonging to the selected
Organization. Unknown and cross-Organization groups are rejected; duplicate IDs
are normalized to one binding. Archived groups cannot be newly selected. An
already-bound archived group remains visible as archived, grants no access, and
may be preserved or removed without making it selectable for another policy.
`public` and `private` require `group_ids=[]`; the backend rejects inconsistent
access-level/group combinations instead of silently normalizing them.

The `/resources` route is an owner/admin management surface. Ordinary members
discover usable resources only in their normal capability surfaces. Those
surfaces may show `Organization-wide` or `Available through <matching own
group>`, but must not expose non-matching audience groups, the full binding list,
or the Organization Resource inventory.

## Management Authorization Matrix

| Operation | Org owner | Org admin | Instance owner | Org member |
| --- | --- | --- | --- | --- |
| View Organization overview | Full | Full | Member-safe unless also manager | Member-safe |
| List all members and emails | Allow | Allow | Deny unless also manager | Deny |
| Invite/remove members | Allow | Allow except owner operations | Deny | Deny |
| Change member role | Allow except generic owner assignment | Allow between admin/member | Deny | Deny |
| Create/update/archive groups | Allow | Allow | Deny | Deny |
| Read group metadata | Allow | Allow | Active-member view | Active-member view |
| Read group member roster | Allow | Allow | Deny unless also manager | Deny |
| View all Organization Instances | Allow | Allow | Own and otherwise authorized | Authorized only |
| Manage Instance access | Allow | Allow | Own Instance | Deny |
| Manage Project access | Allow | Allow | Own Instance | Deny |
| Manage Resource ACL | Allow | Allow | Deny unless also an Org manager | Deny |
| View Resource management inventory | Allow | Allow | Deny unless also manager | Deny |
| Use an Instance/Project/resource | Re-evaluate all access layers | Re-evaluate all access layers | Re-evaluate all access layers | Re-evaluate all access layers |

The frontend uses capability projections to hide or disable actions, but the
projection is never the authorization boundary. Every backend mutation invokes
the existing service-level role and ownership checks again.

## Frontend Experience

### Page shell

Organization pages reuse the existing Avibe App Control Panel shell and UI
primitives. They do not import Cloud Console Next.js components. Cloud Console
screens are behavior and contract references only.

Suggested feature ownership:

```text
ui/src/features/organization/
  api/
    client.ts
    errors.ts
    types.ts
  components/
    OrganizationHeader.tsx
    OrganizationSwitcher.tsx
    OrganizationRoleBadge.tsx
    PrincipalSelector.tsx
    PolicySyncBadge.tsx
  pages/
    OrganizationOverviewPage.tsx
    OrganizationMembersPage.tsx
    OrganizationGroupsPage.tsx
    OrganizationGroupDetailPage.tsx
    OrganizationInstancesPage.tsx
    InstanceAccessPage.tsx
    InstanceProjectsPage.tsx
    OrganizationResourcesPage.tsx
```

The exact extraction may follow the existing repository reuse ladder. This
layout describes ownership, not a requirement to create one file per name.

All user-visible copy lives in `ui/src/i18n/en.json` and
`ui/src/i18n/zh.json`. The UI reuses `Button`, `Badge`, `Input`, `Dialog`,
`Popover`, `Tabs`, and other primitives under `ui/src/components/ui/`.

### Loading and errors

Every page has explicit states for:

- Cloud not connected;
- Organization management authorization required;
- authorization expired and reauthorization in progress;
- management subject differs from the current remote-access subject;
- loading;
- empty Organization data;
- caller has a member-safe view but no management capability;
- Cloud unreachable;
- validation error;
- authorization revoked;
- revision conflict; and
- policy accepted but not yet applied by the Instance.

A `401` caused only by an expired or missing local grant may attempt one
controlled silent reauthorization under the rules below. Other authentication
failures require explicit sign-in. A `403` is shown as current authorization
denial and must not be retried as the local owner. A `404` for a protected object
is treated as unavailable rather than disclosing cross-Organization existence.
`cloud_management_subject_mismatch` has its own terminal state and is not
collapsed into authorization revoked.

### Mutations

Member removal, role downgrade, group archival, and access narrowing use a
confirmation dialog that names the actual effect. The frontend does not claim
an impact count unless the backend returned one.

Member and group mutations send `if_match_revision`, wait for a successful Cloud
response before changing the canonical list, and adopt the revisions returned
by that response. A `409 organization_member_conflict` or `409
organization_group_conflict` refetches the affected records and preserves the
draft for comparison; it never silently retries. Project and Resource policy
editors may render an optimistic pending revision because the existing APIs
return versioned desired policy state; a conflict restores and refetches the
authoritative row.

Any successful member-group relation mutation invalidates both the member and
group query families in the frontend cache. This ensures the other editor loads
the newly bumped revisions before its next save.

## Architecture and Ownership

```text
Browser
  -> Avibe App Organization UI
  -> same-origin local management proxy
  -> existing avibe-backend user APIs with a short-lived user Bearer token
  -> existing avibe-backend service layer
  -> Cloud database

Cloud Project/Resource desired policy
  -> paired-device intent polling
  -> local transactional applied projection
  -> local enforcement
  -> exact revision acknowledgement
```

### Avibe App responsibilities

- Render the Organization management frontend.
- Start and complete a user-present Cloud management authorization flow.
- Keep Cloud management tokens in process memory, isolated per browser session.
- Proxy allowlisted Organization API requests to the configured, pairing-
  validated Cloud backend URL.
- Apply existing local CSRF protection to every proxy mutation.
- Preserve Cloud status codes and stable error codes while stripping unsafe
  upstream headers and response content.
- Never use the Instance device secret as proof of Organization user identity.
- Never persist member lists, Cloud tokens, or Organization policy drafts to
  local product state.

### Avibe Backend responsibilities

- Authenticate the Cloud user and issue a short-lived management token.
- Accept either the existing Cloud Console cookie session or a valid management
  Bearer token in the shared user API authentication boundary.
- Reuse existing Organization, group, Instance, Project, and Resource services.
- Re-evaluate current Organization role and target ownership for every request.
- Maintain optimistic-concurrency revisions for member, group, Project, and
  Resource policies.
- Bump Instance authorization revision for access-narrowing and membership
  changes that invalidate active local sessions.
- Return only safe Project and Resource descriptors.

### Browser responsibilities

- Hold only an opaque, HttpOnly, same-origin local management-session handle.
- Never receive the Cloud management Bearer token.
- Send the existing Avibe App CSRF token on state-changing local proxy calls.

## Cloud Management Authentication

### Why existing authentication is insufficient

Existing Organization APIs call `requireUserApi()`, which reads the
`avibe-backend` origin's HttpOnly session cookie. An Avibe App may run on
localhost, a managed Instance hostname, or a customer custom hostname. Cross-
origin cookie requests are not a reliable or acceptable authentication contract.

The current paired-device user-token endpoint is also insufficient for
Organization management. It lets a device-secret-authenticated runtime request
a capability token for caller-supplied `sub` and `email`, then checks Instance
access. Extending that mechanism with an Organization management scope would let
a compromised device secret attempt to impersonate a known Organization admin.

Organization management requires a user-present Cloud authorization. The Cloud
login, not the device, determines the subject.

### Required flow

1. The user opens `/admin/organization`.
2. Avibe App verifies that the runtime is paired with Avibe Cloud.
3. If no valid per-browser management grant exists, Avibe App starts a step-up
   Cloud authorization with PKCE and an opaque state bound to that browser.
4. Cloud authenticates the user and validates that the requesting client is an
   active paired Instance with an allowed callback hostname.
5. Cloud issues a one-time authorization code for explicit management scopes.
6. The local server exchanges the code and verifier for a short-lived, signed
   management Bearer token.
7. The local server stores the Bearer token only in process memory under a
   random management-session handle.
8. The browser receives only the HttpOnly handle cookie and returns to the
   requested Organization route.
9. Local proxy calls resolve the handle, attach the Bearer token server-side,
   and call the existing Cloud API.
10. Expiry removes the in-memory token. The app may attempt the single silent
    reauthorization path below; otherwise it requires explicit sign-in.

For a remote browser session, the authorized management subject must equal the
subject in the current validated local remote-access session. A remote viewer
must not sign in as a second account and attach that account's management grant
to the first account's local session. A trusted loopback caller has no implied
Cloud identity and must complete the same user-present Cloud login; its first
successful Cloud login establishes the bound subject for that management
session.

If a returned Cloud subject differs from an already-bound remote-access subject,
the local server deletes the management grant immediately and returns
`cloud_management_subject_mismatch`. The UI does not retry, switch accounts, or
classify this as generic revocation. It tells the user to sign out and re-enter
Organization management using the same account as the current Avibe session.
The localized state uses the title `Cloud account does not match` and the body
`The Avibe session and Avibe Cloud are signed in with different accounts. Sign
in to Organization management with the same account as this Avibe session.` It
does not need to expose either account's email address.

If the Organization flow starts on an insecure loopback origin, the paired
HTTPS Instance origin is the canonical callback and Organization management
origin. The app may hand the browser to that route after authorization. The
authorization server must not accept arbitrary callback URLs or weaken the
existing active-hostname validation.

### Token contract

The management token is an RS256 JWT using the existing published JWKS but a
distinct audience from ID tokens and low-risk capability tokens.

Required claims:

```json
{
  "iss": "https://avibe.bot",
  "aud": "avibe-organization-api",
  "sub": "user-id",
  "email": "member@example.com",
  "vibe_instance_id": "inst_...",
  "scope": "organization:read organization:manage instance:read instance:manage project:read project:manage resource:read resource:manage",
  "jti": "token-id",
  "iat": 0,
  "exp": 0
}
```

The token lifetime is at most ten minutes. No refresh token is written to disk
or returned to frontend JavaScript. Reauthorization may be silent only while
Cloud still has a valid user session and the current user explicitly entered
the Organization management surface.

Silent reauthorization is attempted at most once per entry or recovery attempt
and only when all of the following are true:

- the local management handle is missing or expired;
- the user is entering or already using an Organization management route;
- Cloud still has a valid browser session that can complete authorization
  without interaction; and
- the returned subject is identical to the subject already bound to the local
  browser session, when one exists.

The app transitions to the explicit `Sign in to manage Organization` state,
without another silent retry, after explicit logout, subject mismatch,
authorization revocation, missing Cloud session, Cloud `login_required` or
`consent_required`, invalid callback state, authorization-code failure, or
management-token validation failure. A failed silent attempt also transitions
to explicit sign-in. This state machine must not loop or repeatedly navigate
the browser between Avibe App and Cloud.

| Trigger | Silent attempt | Result |
| --- | --- | --- |
| Local grant missing/expired; Cloud session valid; same subject | Once | Restore the local grant and requested route |
| Cloud session missing, `login_required`, or `consent_required` | Never again | Explicit sign-in state |
| Explicit logout | Never | Explicit sign-in state |
| Subject mismatch | Never | Dedicated mismatch state; grant cleared |
| Authorization revoked or token validation fails | Never | Explicit sign-in/error state; grant cleared |
| Callback state/code validation fails | Never | Explicit sign-in/error state; no grant attached |
| The one silent attempt fails for any reason | Never again | Explicit sign-in state |

To distinguish an explicitly signed-out browser from a browser whose in-memory
grant merely expired, logout sets a same-origin, HttpOnly manual-reauthorization
marker containing no identity or token. A failed silent attempt, subject
mismatch, authorization revocation, and token validation failure set the same
marker and clear any grant. The explicit sign-in action clears the marker when
starting a new interactive authorization; silent authorization never clears or
bypasses it.

Scopes are coarse transport capabilities. They do not encode the caller's
Organization role. Backend services load current membership and ownership on
every request so a demoted or removed user cannot keep managing until token
expiry.

### Local proxy contract

Suggested local endpoints:

```text
GET    /api/cloud-management/session
POST   /api/cloud-management/session/start
GET    /auth/organization/callback
DELETE /api/cloud-management/session

ANY    /api/cloud-management/organizations/*
ANY    /api/cloud-management/instances/*
```

The proxy uses an explicit method/path allowlist. It does not accept an arbitrary
upstream URL. It forwards only required headers, JSON request bodies, and query
parameters. It rejects redirects, non-JSON management responses, oversized
responses, and a backend origin that did not pass pairing validation. Tokens,
cookies, and pairing secrets are redacted from logs and diagnostics.

## Existing API Reuse

The following business APIs remain authoritative. Bearer authentication is an
additional authentication mode for the same routes, not a second
implementation.

| Capability | Existing `avibe-backend` route | Required gap |
| --- | --- | --- |
| Organization switcher | `GET /api/organizations` | Bearer user auth |
| Organization detail | `GET /api/organizations/:orgId` | Bearer user auth; add caller-only group summary and manager-only detailed counts |
| Invite/list members | `GET/POST /api/organizations/:orgId/members` | Bearer user auth; return `member_revision` |
| Update/remove member | `PATCH /api/organizations/:orgId/members/:memberId` | Bearer user auth; accept `if_match_revision`; UI uses `status=removed` |
| Resend invitation | `POST /api/organizations/:orgId/members/:memberId/resend-invite` | Bearer user auth |
| List/create groups | `GET/POST /api/organizations/:orgId/groups` | Bearer user auth; return `group_revision` |
| Edit/archive group | `PATCH/DELETE /api/organizations/:orgId/groups/:groupId` | Bearer user auth; accept `if_match_revision` |
| Replace group members | `PUT /api/organizations/:orgId/groups/:groupId/members` | Bearer user auth; accept `if_match_revision`; atomically bump affected member revisions |
| List Instances | `GET /api/organizations/:orgId/instances` | Bearer user auth; member-safe owner projection; add Project/Resource sync summary |
| Manage Instance access | `GET/PUT /api/instances/:instanceId/authorized-users` | Bearer user auth |
| List Projects | `GET /api/instances/:instanceId/projects` | Bearer user auth; authorize Org owner/admin |
| Manage Project access | `GET/PUT /api/instances/:instanceId/projects/:projectId/access` | Bearer user auth; authorize Org owner/admin |
| List resources | `GET /api/organizations/:orgId/resources` | Bearer user auth |
| Manage Resource ACL | `PATCH /api/organizations/:orgId/resources/:instanceId/:kind/:resourceId/access` | Bearer user auth |

No frontend call depends on Cloud Console HTML, Next.js server actions, or a
Cloud-origin cookie.

## API Client and Contracts

Avibe App defines explicit TypeScript request and response types for the reused
Cloud APIs. Types are based on the Cloud JSON contract, not imported from the
sibling checkout at build time. A contract fixture suite prevents drift between
the repositories.

Member and group revisions are monotonically increasing, non-negative integers.
Creation returns the initial revision. A mutation of an existing entity includes
the revision observed by the editor, for example:

```json
{
  "role": "member",
  "group_ids": ["grp_engineering"],
  "if_match_revision": 7
}
```

```json
{
  "member_ids": ["mem_alice", "mem_bob"],
  "if_match_revision": 12
}
```

Member responses expose `member_revision`; group responses expose
`group_revision`. A change increments only the revisions whose covered state
actually changed; a no-op returns the current revision. A stale mutation makes
no changes and returns the current safe revision hint:

```json
{
  "error": "organization_member_conflict",
  "current_revision": 8
}
```

The corresponding group error is `organization_group_conflict`. The revision
hint is not authorization to retry: the client must refetch the authoritative
entity and ask the user to reconcile the preserved draft.

The local proxy returns a stable envelope for transport failures while
preserving successful Cloud payloads and Cloud business error codes:

```json
{
  "error": "cloud_management_unavailable",
  "retryable": true
}
```

Expected stable business and management errors include:

- `unauthorized`;
- `forbidden`;
- `organization_not_found`;
- `organization_member_not_found`;
- `organization_group_not_found`;
- `organization_group_archived`;
- `organization_member_conflict`;
- `organization_group_conflict`;
- `instance_not_found`;
- `project_not_found`;
- `resource_not_found`;
- `member_email_taken`;
- `group_name_taken`;
- `too_many_entries`;
- `invalid_project_access_intent`;
- `invalid_resource_acl_intent`;
- `cloud_management_subject_mismatch`; and
- `resource_sync_conflict`.

The frontend maps codes to localized copy. It never renders raw backend error
details.

## Synchronization Semantics

Member and group changes are authoritative once the Cloud API transaction
commits, but authorization narrowing is not complete until affected runtime
sessions reject stale authorization revisions.

Project and Resource changes have two states:

1. Cloud desired policy was accepted at revision `N`.
2. The paired runtime applied and acknowledged revision `N`.

The UI derives these statuses:

| UI state | Meaning |
| --- | --- |
| Applying | Desired revision is newer than applied revision |
| In sync | Desired and applied revisions match and sync is healthy |
| Offline | Instance has not synchronized within the configured freshness window |
| Error | Runtime rejected or failed to apply the desired policy |
| Deleted | Descriptor is tombstoned and omitted from normal lists |

Project and Resource writes include `if_match_revision`. A `409
resource_sync_conflict` causes a row/dialog refetch and preserves the user's
draft only for comparison. The client never retries the stale write
automatically.

The Instance overview aggregates all active Project and Resource descriptor
states by the deterministic severity order:

```text
Error > Offline > Applying > In sync
```

The Organization Instance-list response carries the safe aggregate so the
frontend does not fetch a manager-only Resource inventory or independently
reimplement the offline freshness window:

```json
{
  "policy_sync": {
    "status": "error",
    "projects": { "active": 3, "error": 1, "offline": 0, "applying": 1, "in_sync": 1 },
    "resources": { "active": 2, "error": 0, "offline": 0, "applying": 0, "in_sync": 2 }
  }
}
```

`status` is `error | offline | applying | in_sync | none`. The counts disclose
no Project/Resource names, owners, ACLs, or audience groups and are safe for a
caller already authorized to see that Instance.

The aggregate badge is the worst child state. Deleted and tombstoned descriptors
are ignored. When no active Project or Resource descriptors exist, the badge is
`No managed policies`, not `In sync`. The detail view also shows Project and
Resource counts by state so the aggregate is explainable rather than a lossy
single signal.

Access narrowing must trigger authorization refresh or revocation for:

- Organization member removal;
- Organization role downgrade where effective management changes;
- removal from an authorized group;
- group archival;
- Instance role downgrade or access-entry removal;
- Project role downgrade or binding removal; and
- Resource ACL narrowing.

## Privacy and Security Requirements

- Organization data never becomes a local source of truth.
- Member directories and email addresses are returned only to Organization
  managers. Member-safe pages show only the current user's membership.
- Non-manager Resource surfaces return only resources usable by the caller and
  may identify Organization-wide access or one of the caller's own matching
  groups. They do not return non-matching audience names or full ACL composition.
- Project descriptors contain a safe display name and stable ID, never a path.
- Resource descriptors contain safe names and IDs, never prompts, secret values,
  source paths, configuration, or execution output.
- The browser never receives the management Bearer token, Instance device
  secret, or pairing secret.
- One runtime may serve multiple browsers and users. Management tokens are keyed
  per browser session and subject, never stored in one global current-user slot.
- Logout deletes the in-memory token and handle mapping immediately. Its
  no-token manual-reauthorization marker only suppresses silent login and is
  cleared when the user explicitly starts sign-in.
- Service restart drops all management grants and requires reauthorization.
- Management responses use `Cache-Control: no-store, private` and vary on the
  local management-session cookie where applicable.
- Local proxy mutations require the existing CSRF token.
- The proxy rejects cross-Origin browser requests and uses a strict upstream
  allowlist derived from the paired backend URL.
- Cloud APIs continue returning `404` for protected cross-Organization objects
  where existence must not be disclosed.
- Logs and Sentry data redact Authorization headers, cookies, OAuth codes,
  verifiers, tokens, member invitation links, and pairing credentials.

## Delivery Plan

### Phase 1: Authentication and contracts

- Add Cloud management step-up authorization and signed short-lived tokens.
- Extend the shared Cloud user authentication boundary to accept cookie or
  management Bearer authentication.
- Add the member-safe self-membership/group-summary projection without exposing
  another member's identity or email.
- Add the member-safe Instance owner projection and safe per-Instance policy
  synchronization aggregate.
- Add member/group revision columns, mutation preconditions, atomic cross-entity
  revision bumps, and stable `409` conflict responses.
- Add the Avibe App in-memory management grant store and allowlisted local proxy.
- Add contract fixtures for Organization, member, group, Instance, Project, and
  Resource payloads and errors.
- Do not expose write controls until subject binding, expiry, logout, and
  revocation tests pass.

### Phase 2: Read-only Organization UI

- Add Control Panel navigation and Organization routes.
- Add Organization switcher and overview.
- Add member-safe Members and Groups pages, authorized Instance and Project
  pages, and an owner/admin-gated Resources page.
- Verify owner, admin, member, Instance-owner, local-loopback, managed-hostname,
  and custom-hostname views.

### Phase 3: Members and groups

- Add invite, resend, role, group-membership, removal, group create/edit/archive,
  and group-member replacement flows.
- Add destructive-action confirmation, member/group optimistic-concurrency
  recovery, and authorization-revision verification.

### Phase 4: Instance and Project access

- Add Instance access editor.
- Generalize Project management authorization to Organization owner/admin or
  Instance owner for Organization-bound Instances.
- Add revision conflicts, pending application, offline, and error states.

### Phase 5: Resource access

- Replace the Agent-page external Resource Access dependency with native deep
  links into the Organization Resources page.
- Add Agent, Vault secret, Skill, and Show Page ACL management.
- Retain separate use-versus-management enforcement and resource-specific safety
  rules.

### Phase 6: Parity and Cloud Console transition

- Run cross-repository contract and E2E coverage.
- Verify mobile and desktop UI, accessibility, localization, deep links, and
  browser refresh behavior.
- Keep the Cloud Console available as a fallback until the current-head Avibe
  App passes the parity checklist.
- Only then decide whether Cloud Console Organization pages redirect to an
  Instance or remain as a recovery surface.

## Verification Strategy

### Avibe App unit and component coverage

- Role and capability projection controls visible actions correctly.
- A valid management identity admits ordinary members and Instance owners to the
  Organization shell without exposing manager-only routes or data.
- Member-safe responses cannot render a directory or management action.
- Member-safe Resource surfaces reveal only usable resources and matching own
  audience reasons, never non-matching group names or full ACL composition.
- Principal editors normalize and deduplicate email, domain, and group entries.
- Archived groups remain visible on existing policies but cannot be newly
  selected.
- Project `inherit`, `restricted`, and `owner_only` rules serialize correctly;
  empty restricted bindings always render as Owner-only.
- Resource UI maps wire `public` to `Organization` and validates the `group_ids`
  invariants for `scope`, `public`, and `private`.
- Member, group, Project, and Resource conflict recovery refetches without
  auto-overwriting.
- Instance sync aggregation follows `Error > Offline > Applying > In sync` and
  emits `No managed policies` for an empty active descriptor set.
- Token expiry and logout clear Organization state; silent reauthorization is
  attempted at most once and never after a terminal authentication result.
- Subject mismatch clears the grant and renders its dedicated state without
  silently changing accounts.
- Route changes and Organization switching cancel stale requests.

### Avibe Backend contract coverage

- Every reused route accepts the existing Cloud cookie and the new management
  Bearer token through one authenticated-user abstraction.
- Scopes, issuer, audience, expiry, signature, Instance binding, and subject are
  validated.
- Device-secret-only requests cannot mint an Organization management token for
  caller-supplied identity.
- Current Organization role is re-read for every management request.
- The self-membership projection returns only the caller's role and groups to a
  non-manager and never expands into another member's directory record.
- A member-safe Instance response hides another owner's email while returning
  only descriptor counts and the canonical worst synchronization state.
- Member and group mutations reject stale revisions, bump all affected entity
  revisions atomically, and cannot lose a relation change made through the
  other editor path.
- Organization admins can manage Projects on Organization-bound Instances;
  unrelated members and cross-Organization callers cannot.
- Project and Resource optimistic-concurrency behavior remains compatible.
- Instance synchronization aggregation ignores tombstones, uses the shared
  offline freshness resolver, follows the severity order, and returns `none`
  for no active descriptors.
- Resource `scope` rejects empty, unknown, and cross-Organization group IDs,
  canonicalizes duplicates, and prevents new archived-group selection;
  `public` and `private` reject non-empty IDs.

### Closed-loop scenarios

- Owner signs into Organization management and views the same data as the Cloud
  Console.
- Admin invites a member, adds the member to a group, grants that group Instance
  editor access, restricts a Project to viewer, and grants one Agent to the
  group.
- The new member sees only the authorized Instance, Project, and Agent and cannot
  access management actions.
- Removing the member invalidates an already-open editor session across managed
  and custom hostnames.
- Archiving a referenced group prevents new use and does not expose stale policy
  data.
- An offline Instance shows pending policy state, then converges to In sync after
  reconnect.
- Two admins editing the same Project or resource produce a revision conflict;
  neither silently overwrites the other.
- A member editor and group editor concurrently changing the same membership
  relation produce a revision conflict; neither silently overwrites the other.
- An Instance with mixed Project/Resource states displays the worst state and
  exposes the per-type counts that explain it.
- A local trusted owner without Cloud login cannot access Organization data.
- A stolen/invalid device secret or a mismatched remote-session subject cannot
  obtain or attach an Organization management grant.
- An expired local grant with a valid same-subject Cloud session silently
  reauthorizes once; logout, mismatch, revocation, and `login_required` require
  explicit sign-in and never create a redirect loop.

Implementation must add the new multi-step management authorization cases to
the canonical scenario catalog and a closed-loop harness under
`tests/scenarios/auth_setup/`, following the repository scenario-testing
standard.

### Manual regression

After implementation, update the existing local Incus `master` regression
environment through the supported runner, preserve accumulated state, and verify
service health. Exercise the Organization UI from desktop and mobile viewports
and verify at least one managed hostname and one active custom hostname. Do not
reset Organization, pairing, or regression state to make the flow pass.

## Acceptance Criteria

- Organization management is available under the Avibe App Control Panel.
- All Organization routes require user-present Cloud identity, while ordinary
  active members and Instance owners can enter their member-safe/owned scopes
  without being treated as Organization managers.
- Owners/admins can manage members and groups without opening the Cloud Console.
- Authorized managers can manage existing Instance and Project access without
  opening the Cloud Console.
- Authorized managers can manage Agent, Vault secret, Skill, and Show Page
  Organization ACLs without opening the Cloud Console.
- All business mutations are processed by existing `avibe-backend` services and
  stored only in the Cloud control plane.
- Avibe App does not persist a duplicate Organization directory or desired
  policy database.
- Cloud-origin cookies are not required by Avibe App API calls.
- Management identity is user-present, short-lived, subject-bound, and cannot be
  minted from the device secret plus caller-supplied identity.
- Trusted-local access alone does not grant Organization management authority.
- Project management works for Organization owners/admins and the relevant
  Instance owner, while personal Instances remain owner-only.
- Owner-only is an unambiguous Project UI choice that serializes to restricted
  with empty bindings; Restricted always has at least one binding.
- Resource `scope` carries validated `group_ids`, and non-managers never receive
  arbitrary Resource audience names or management inventory.
- Member, group, Project, and Resource policy writes reject stale revisions
  without silent overwrite.
- Instance sync badges use the worst active child state and distinguish an empty
  managed-policy set from `In sync`.
- Silent reauthorization has a single-attempt boundary, and subject mismatch is
  a dedicated terminal state that clears the grant.
- Access downgrade and removal invalidate stale HTTP, SSE, and WebSocket
  authorization across all active Instance hostnames.
- Cloud Console remains available until read/write, error-state, localization,
  accessibility, and regression parity are verified.

## Open Follow-Ups

These items are deliberately deferred and require separate product decisions:

- whether Organization creation and ownership transfer should later move into
  Avibe App;
- whether domain and custom-hostname management belongs beside Instance access;
- whether the Cloud Console remains a permanent recovery surface;
- whether individual-user Resource ACL grants are needed beyond private,
  Organization, and group audiences;
- whether Organization changes require a durable audit-log UI; and
- when Harness Task and Watch resources join the Organization Resource ACL.

## Design Appendix

High-fidelity mockups for this plan live in `avibe-docs/design.pen` (Pencil), in
the dark **"Org · … (Dark)"** cluster laid out below the light
**"avibe.bot — Console · Organization"** frames. The light frames are the Cloud
Console **reference only** (they also cover deferred first-delivery items such as
instance creation, ownership transfer, and domain management). The dark cluster
is the Avibe App Control Panel implementation described here.

All screens reuse the vibe-remote dark shell: sidebar + `padding:[32,48]` main,
breadcrumb → title → toolbar → content; tokens `$--background`, `$--card`,
`$--mint` primary, `$--muted`, with Inter body / JetBrains Mono for labels and
identifiers. A reusable **`OrgSidebar`** component provides the Overview /
Members / Groups / Instances / Resources sub-navigation with the Organization
switcher on top and a Cloud-connection status box at the bottom.

### Screen inventory

Pages (8):

| Screen | Route |
| --- | --- |
| Overview | `/overview` |
| Members | `/members` |
| Groups | `/groups` |
| Group detail | `/groups/:groupId` |
| Instances | `/instances` |
| Instance access | `/instances/:instanceId/access` |
| Instance projects | `/instances/:instanceId/projects` |
| Resources | `/resources` |

Modals & confirmations (10): Invite member; Edit member; Remove member
(confirm); New group; Edit group; Archive group (confirm); Instance access
entry; Project access editor; Resource ACL editor; Resource ACL · Selected
groups variant.

States & reference (12): Cloud not connected; Sign-in required (PKCE step-up);
Reauthorizing; Cloud unreachable; Access revoked; Cloud account does not match
(subject mismatch); Loading (skeleton); Empty; Member-safe overview; Validation
error; Revision conflict; Policy sync states reference.

### Resolved-decision traceability

| # | Resolved decision | Where it shows in the mockups |
| --- | --- | --- |
| 1 | Identity gate, not administrator gate | Sign-in required is an identity step-up; Member-safe overview shows a non-manager view without a directory |
| 2 | Member/group optimistic concurrency | Revision conflict state (`if_match` / `409`, draft kept for comparison; same pattern applies to member/group) |
| 3 | Owner-only first-class Project choice | Project access editor exposes Inherit / Restricted / Owner-only; Restricted always shows ≥1 binding |
| 4 | Selected-group Resource `group_ids` | Resource ACL · Selected groups variant with a group multi-select and `group_ids` payload note |
| 5 | Member-safe hides Resource composition | Resources page is owner/admin-only; Member-safe overview lists only the caller's own usable resources |
| 6 | Worst-active-child sync + empty set | Policy sync states reference includes In sync / Applying / Offline / Error / Deleted / **No policies** |
| 7 | Silent reauthorization single attempt | Reauthorizing state framed as an automatic one-shot |
| 8 | Subject mismatch terminal state | Dedicated "Cloud account does not match" state using the specified title/body copy |

### Known non-blocking mockup gaps

- No standalone member/group revision-conflict dialog; the project/resource
  conflict screen demonstrates the identical pattern.
- The Member-safe overview sidebar shows the manager-only Resources item in a
  normal inactive style rather than an explicitly gated/hidden treatment.
