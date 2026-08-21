# Instance access role `member` (owner minus member management)

Issue: https://github.com/avibe-bot/avibe/issues/1596
Related: https://github.com/avibe-bot/avibe/pull/1562 (Personal editor resource-ACL semantics — the capability model this role plugs into)

This file is the **cross-lane contract**. Field names, role identifiers, and capability names are exact. Deviations require orchestrator sign-off.

## Decisions (frozen)

1. **Identifier**: the instance access role string is `member` (not `admin`, not `instance_member`).
2. **UI / docs copy**: display the instance access role as **"Member"** (ZH: **「成员」**). The owner accepts its overlap with the cloud organization-role copy. The stored/token value remains `member`.
3. **Ownership transfer stays owner-only.** `member` cannot transfer instance ownership (Personal or Organization). Treat it as member management.
4. **Applies to both Personal and Organization Avibe.**
5. **Personal vs Organization ACL**: `member` is treated as editor-or-higher for the Personal resource-use bypass (`has_role("editor")` already covers it once rank is `viewer < editor < member < owner`). Organization ACL evaluation is unchanged for *use*: `member` is still subject to Agent / Project resource ACL like any non-owner Organization principal. Agent *management* is owner-equivalent at the resource layer, including resources owned by another subject, resources bound to another organization policy, and resources with no policy row.

## Role axis (instance access)

```
viewer < editor < member < owner
```

Stored / token / session payload value: `vibe_instance_role` ∈ `{owner, member, editor, viewer}`.

Allowlist entry role (cannot be `owner`; owner is implicit via `instance.ownerUserId`):

```
InstanceAccessEntryRole = "member" | "editor" | "viewer"
```

Project access bindings stay `{editor, viewer}` — **do not** add `member` to project ACL rows. A `member` instance role on a restricted Organization project is narrowed by the existing `min(instance_role, binding_role)` rule (so a `member` bound as `viewer` becomes `viewer`).

Organization roles stay `{owner, admin, member}` and are a **separate axis**. Never conflate them.

## Capability contract

Exact capability names. Source of truth: `AuthorizationContext` in `vibe/authorization.py`.

| Capability | viewer | editor | **member** | owner |
|---|---|---|---|---|
| `can_read_instance` | yes | yes | yes | yes |
| `can_chat` | no | yes | yes | yes |
| `can_use_cloud_asr` | no | yes | yes | yes |
| `can_use_resource("agent"\|"skill"\|"vault_secret")` | no | yes | yes | yes |
| `can_use_resource("show_page")` | yes | yes | yes | yes |
| `can_use_terminal` / `can_use_files` / `can_use_terminal_files` | no | yes | yes | yes |
| `can_manage_projects` | no | no | **yes** | yes |
| `can_manage_agents` | no | no | **yes** | yes |
| `can_manage_instance` | no | no | **yes** | yes |
| `can_use_system` | no | no | **yes** | yes |
| **`can_manage_access_members`** (NEW) | no | no | **no** | yes |
| `is_instance_owner` | no | no | **no** | yes |

`is_instance_owner` remains `instance_role == "owner"` (identity, not rank). Canonical-owner control-plane actions (pairing keys, hostname writes, ownership transfer, allowlist mutate) stay `canonicalOwnerRequired`.

`capability_projection()` MUST include the new key:

```
"can_manage_access_members": <bool>
```

Existing keys keep their names and types. Frontend `InstanceCapabilities` adds the same key.

## Rank

```
_ROLE_RANK = {"viewer": 1, "editor": 2, "member": 3, "owner": 4}
INSTANCE_ROLES = frozenset({"owner", "member", "editor", "viewer"})
```

`has_role("owner")` is true only for owner. `has_role("member")` is true for member and owner. `has_role("editor")` is true for editor, member, and owner.

Therefore:

- `can_manage_projects` / `can_manage_agents` / `can_manage_instance` / `can_use_system` change from `has_role("owner")` to `has_role("member")`.
- `can_manage_access_members` is `has_role("owner")` (equivalently `is_instance_owner`).

## Member-management surface (owner-only)

These MUST require `can_manage_access_members` (not the broader `can_manage_instance`):

**App (`avibe`):**

- `PUT /api/permissions/authorized-users` (`vibe/ui_server.py` current-instance authorized-users PUT)
- Any other handler that adds / removes allowlist entries or changes an access role
- Ownership-transfer UI / API, if any local surface exists (keep owner-only)

**Backend (`avibe-backend`):**

- `instance.authorized_users.mutate` (allowlist add/remove/replace) — remains `canonicalOwnerRequired`
- Instance ownership transfer — remains `canonicalOwnerRequired`
- Pairing-key issue/rotate, custom hostname writes — unchanged, still canonical owner

`member` MUST be a valid **value** of an allowlist entry (so an owner can grant `member`), but a principal whose own role is `member` cannot mutate the allowlist.

## Token / session payload contract (cross-repo)

Produced by avibe-backend (`lib/security.ts` cloud token `vibe_instance_role`; `lib/oidc/authorization-context.ts`; `app/api/v1/instances/[instanceId]/user-token`).

Consumed by avibe (`vibe/authorization.py` `context_from_session_payload`, `vibe/remote_access.py` `_validated_authorization_payload`).

| Field | Type | Semantics |
|---|---|---|
| `vibe_instance_role` | `"owner" \| "member" \| "editor" \| "viewer"` | Instance access role. Unknown values fail closed (empty context). |
| `vibe_instance_access_source` | existing enum, unchanged | |
| `vibe_instance_kind` | `"personal" \| "organization"` | server-owned; unchanged |
| `vibe_instance_id` | string | pairing identity; unchanged |

Allowlist schema:

- `remote_access_allowlist_entries.role`: enum `["viewer", "editor", "member"]`, default `"viewer"`
- CHECK constraint updated to match
- `oauth_authorization_codes.instance_access_role`: enum `["owner", "editor", "viewer", "member"]` (nullable snapshot)
- `InstanceAccessRole` type includes `"member"`
- `InstanceAccessEntryRole = Exclude<InstanceAccessRole, "owner">` therefore becomes `"member" \| "editor" \| "viewer"` automatically

Project ACL (`access_role` `editor|viewer`) is **out of scope**.

## Persisted-shape rule

- Older releases never write `member`. Loaders MUST keep accepting `{owner, editor, viewer}` snapshots with no `member` field.
- An unknown role value fails closed (empty / unauthorized context), never crashes startup.
- Backend migration: add `member` to the allowlist CHECK; existing `viewer`/`editor` rows unchanged. Provide a load fixture covering a pre-`member` allowlist row.
- App: `context_from_session_payload` accepts `member`; payloads without it behave as today.
- Deferred `resource_user_context` snapshots: a `vibe_instance_role: "member"` written by this release must round-trip; a snapshot without the field keeps its previous role.

## HTTP / SSE policy (app)

- Default `/api/` minimum stays fail-closed.
- Member-reachable management routes are explicitly declared in `_MEMBER_HTTP_RULES`; unknown APIs remain Owner-only. Member-management, credential, host-lifecycle, ACL-write, and bulk-onboarding routes stay outside that allowlist.
- `POST /api/agents/default` requires the member tier and its setter requires `can_manage_agents`. The default remains advisory: assigning a private or scope-policy Agent does not widen its ACL, and callers who cannot use it degrade to another usable Agent at resolution time.
- Privileged SSE events (definitions/runs/vaults updated) currently require owner. **Member receives them** (they are instance-management events, not member-management). Implement by treating them as `has_role("member")`.
- Editor SSE (`queue.updated`) and viewer SSE unchanged.

## Frontend

- `ui/src/lib/sessionInfo.ts`: `instance_role: 'owner' | 'member' | 'editor' | 'viewer'`; `InstanceCapabilities.can_manage_access_members: boolean`.
- Permissions page (`ui/src/features/permissions/`): role picker offers `member`; controls that mutate members / roles require `can_manage_access_members` (not `can_manage_instance`).
- i18n: EN `"Member"` / ZH `"成员"` for the instance-access role. The deliberate overlap with organization-member copy is accepted.
- AppShell / Workbench / settings: continue to key off capabilities. After projection includes `can_manage_instance=true` for member, those surfaces light up automatically. Member-management widgets must additionally require `can_manage_access_members`.

## Tests (property, not enumerations)

Seed one principal of every existing role **plus** `member`, run the change, assert:

1. `member` can do every owner instance operation except member management and ownership transfer.
2. `member` cannot add/remove allowlist entries or change roles (HTTP 403 / capability false).
3. `owner` still can.
4. `editor` / `viewer` capabilities unchanged vs current master.
5. Unknown role still fails closed.
6. Pre-`member` allowlist / token / snapshot fixtures still load.
7. Personal `member` gets the Personal editor resource-use bypass (Agent + projects); Personal `viewer` does not.
8. Organization `member` remains subject to Agent / Project ACL for *use*; Agent management is owner-equivalent at the resource layer.
9. Instance-access role copy is `"Member"` / `"成员"` without changing the stored value.

## Lane split

### Lane B — avibe-backend (`codex`)

Repo: `avibe-backend`, branch from latest `origin/main`.
Scope (no-touch for lane A):

- `lib/types.ts` (`InstanceAccessRole`, `InstanceAccessEntryRole`)
- `lib/oidc/authorization-context.ts`
- `lib/db/schema.ts` allowlist + oauth snapshot enums + CHECKs
- new drizzle migration (allocate next revision against `origin/main` HEAD at rebase)
- `lib/instances.ts` `resolveInstanceAccess` (member is a valid allowlist role; editor-vs-viewer aggregation becomes editor-or-member-or-viewer with member > editor > viewer if any aggregation exists)
- `lib/security.ts` token mint `vibe_instance_role`
- `app/api/instances/[instanceId]/authorized-users` and v1 equivalent (accept `member` as an entry role; mutate still canonical-owner)
- store drizzle/memory allowlist writers
- tests covering schema, resolution, token projection, persisted pre-`member` fixtures
- i18n / console labels if the org console exposes the allowlist role picker

Do **not** touch avibe-app files.

### Lane A — avibe (`claude`)

Repo: `avibe`, branch from latest `origin/master`.
Scope (no-touch for lane B):

- `vibe/authorization.py` (roles, rank, capabilities, HTTP/SSE policy, projection)
- `core/vibe_agents.py` (Agent management/default gates and pre-catalog entitlement fallbacks)
- `vibe/ui_server.py` (authorized-users PUT and any sibling member-mutation gate → `can_manage_access_members`)
- `vibe/permissions.py` if it validates allowlist roles
- `vibe/remote_access.py` only if it independently validates the role set
- `storage/project_access_service.py` / `storage/resource_access_service.py` only if they hardcode `{owner,editor,viewer}` instead of using `has_role` / `INSTANCE_ROLES`
- `ui/src/lib/sessionInfo.ts`, `ui/src/features/permissions/*`, i18n en/zh
- focused tests: instance authorization, HTTP policy, permissions page / capability projection, persisted snapshot fixtures
- `ui/` build if UI files change

Do **not** touch avibe-backend files. Consume the token field `vibe_instance_role: "member"` as documented; if backend is unmerged, tests stub that payload.

## Merge / deploy order

Backend first (token + allowlist can already emit `member`; old app treats unknown-or-absent as fail-closed / ignore). App second (understands `member`, exposes the capability). Independent PRs, no stacked PR. App PR body declares "expects backend #NNNN merged first" once the backend PR exists.

## Out of scope

- Changing organization roles
- Adding `member` to project ACL bindings
- Billing / plan gates
- Renaming the organization role `member`

## Addendum: Agent management alignment (2026-08-21)

### Divergence found

The initial implementation left three authorization layers inconsistent. The capability projection granted `can_manage_agents` to `member`, and the HTTP allowlist admitted Agent CRUD, but `storage/resource_access_service.py::_policy_allows_management` still required every non-owner to match the resource policy's `owner_user_id`. Built-in SYSTEM Agents such as `claude` and `codex` normally have no policy row, so the missing-policy fail-closed path denied every member mutation even though the capability and router both admitted it.

### Owner decisions

1. `member` gets owner-equivalent Agent management at the resource layer. This includes built-in Agents, Agents owned by other subjects, missing-policy rows, and Organization policies bound to another organization. Editor behavior is unchanged. Resource *use* remains ACL-governed, and `_policy_allows_owner_control` remains limited to the Instance Owner or resource owner.
2. Instance-wide default Agent selection is open to `member`: the HTTP route is member-tier and `set_default_agent_name` uses `can_manage_agents`. Bulk Agent onboarding and credential/host-lifecycle routes remain Owner-only.
3. The instance-access role display copy is `"Member"` in English and `"成员"` in Chinese. The stored/token value remains `member`; overlap with organization-member copy is deliberate.

### Pre-catalog fallback audit

| Site | Decision | Rationale |
|---|---|---|
| `ensure_agent_selection_access` missing-row fallback | Include `member` via `has_role("member")` | A historical backend-name selector is Agent entitlement compatibility, not Owner identity. |
| `ensure_agent_name_access` legacy task/watch binding fallback | Keep `is_instance_owner` | The fallback preserves executable pre-catalog definitions; a member-created binding is new data and must reference a real catalog Agent because execution deliberately requires that row. |
| `ensure_session_agent_access` backend-only session fallback | Include `member` via `has_role("member")` | Dispatching a persisted pre-catalog session is compatibility for Agent use/management, not ownership or migration control. |
| `_require_agent_onboarding_access` | Keep `is_instance_owner` | Onboarding inventories and claims every policy-less Agent in one one-way migration; its instance-wide migration authority is distinct from per-Agent management. |

The remote-Owner task/watch write fallback can still admit a missing legacy name while `ScheduledTaskService._require_execution_agent_access` requires a catalog row at execution. That divergence predates this change and is a known latent issue outside this alignment's scope; it does not justify admitting new member-created bindings. New routing must reference a real Vibe Agent catalog row.

### Current-master default semantics

The original follow-up brief referenced the pre-PR-#1606 audience-wide assignment predicate. PR #1606 had already replaced that model on `master`: an instance default is advisory, and ACL enforcement occurs per principal when the default is resolved. This alignment deliberately preserves #1606. A member may assign a private or scope-policy Agent as the default; a principal who cannot use it degrades to another usable Agent without changing the configured default or widening the target ACL.

## Addendum: Agents page load 403s (2026-08-21, post-#1621)

### Divergence found

After #1621 aligned the Agent ACL layer, a remote member opening `/agents` still
got two `instance_access_forbidden` toasts. Mutation worked; the *page load*
fired two Owner-only GETs. Both are capability/HTTP mismatches, not ACL gaps:

1. `AgentsPage.refreshOnboarding` was gated on `can_manage_agents`. #1621 made
   that bit true for `member`, so the page began requesting
   `GET /api/agent-onboarding` — a route deliberately kept Owner-only because
   bulk onboarding is a one-way instance-wide migration. The handler's `catch`
   swallowed the throw, but `ApiContext.handleApiError` had already toasted.
2. The Agents detail panel auto-selects the default Agent on first load and
   loads that backend's model catalog: `GET /api/claude/models`,
   `GET /api/codex/models`, or `GET /api/backend/opencode/providers`. None was
   named by any policy table, so all three fell to the Owner default-deny.

   For OpenCode the default-deny was also load-bearing. `/api/backend/opencode/
   providers` is the *Settings* catalog: each row carries the provider's base
   URL, a masked API-key preview, its active auth type, and the instance's
   tool-call permission setting. The picker consumed one field of it.

### Decisions

1. Onboarding UI keys off `is_instance_owner`, not `can_manage_agents`. The
   route stays Owner (addendum above, `_require_agent_onboarding_access`); the
   UI now matches it, so the banner simply never loads for a member and cannot
   403. The member keeps New Agent, Import, Global prompts, edit, and default —
   all already member-tier.
2. The three read-only model catalogs are editor-tier, added to
   `_EDITOR_HTTP_RULES` as exact GET rules. Editor rather than member because
   Chat's `AgentRoutePicker` shares the same loader and is an editor surface;
   member inherits editor by rank. Deliberately **not** an `/api/backend`
   namespace: the rest of that namespace is credential and host work —
   `*/auth*`, custom-provider writes, CLI install, runtime restart — and keeps
   Owner by the unknown-route default.
3. OpenCode is admitted through a new model-only projection,
   `GET /api/backend/opencode/models`, and the Settings catalog it projects from
   stays Owner. The projection returns configured providers reduced to
   `{id, name, models}` — the question a picker asks — and drops the setup state
   that made the Settings route Owner in the first place. `configured` is
   filtered server-side rather than by the caller, so which providers an owner
   has connected is not disclosed either. Claude and Codex need no equivalent:
   `claude_models()` / `codex_models()` are already snapshots of a shared model
   catalog and carry no provider configuration.

   The daemon start behind the projection (`_opencode_get_server()` →
   `ensure_running()`) is left as-is. It is not a privilege escalation for these
   ranks: editor and member can send chat, and `OpenCodeAgent` ensures the same
   daemon on the message path, so a rank that may run an OpenCode Agent may
   already start it.

### Rule this leaves behind

A capability bit is the UI's permission to *render* a surface; the HTTP policy
is what may be *called*. When a rank gains a capability, every request the
surface issues on load has to be re-checked against the policy tables, including
the shared read-only lookups a page pulls in indirectly. A UI gate that is
merely close to the route's tier produces a toast on every page load.

The second half of that rule: when the route a widened surface needs is also a
management endpoint, classify the *question the surface asks*, not the endpoint
that happens to answer it. "Which models can I pick" and "how is this provider
configured" are different questions, and only the first belongs to a rank that
configures Agents but does not administer the instance.
