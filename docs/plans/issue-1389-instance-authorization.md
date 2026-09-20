# Issue 1389: Instance Authorization Repair

## Background

The current runtime still contains two transitional authorization models:

- trusted-local and local-only product gates;
- unrestricted access for active Organization members.

Those models override Viewer/Editor/Owner and resource ACL decisions, so the
same admitted Instance user receives different capabilities depending on the
request origin or Organization claims.

## Goal

After Instance admission, every product decision uses only:

1. Instance role (`viewer`, `editor`, or `owner`);
2. Project ACL;
3. Agent ACL;
4. Show Page ACL.

`is_remote` remains transport metadata only. Organization claims remain
admission and ACL attributes only. Connection/session validation, Origin/CSRF,
path validation, Vault approval, and input validation remain unchanged.

## Implementation Contract

- A local Web request and standalone local administration without an initiating
  user context receive an ordinary Instance Owner context. There is no trusted
  local authorization identity or bypass.
- Viewer may read allowed Projects, Sessions, messages, and Show Pages. Viewer
  cannot chat, invoke Agents, or use Files, Editor, Terminal, Dock, Skills,
  Vault, or Harness.
- Editor may chat only in Projects whose effective Project role is Editor, may
  discover/select/invoke only Agents allowed by Agent ACL, and may use Files,
  Editor, Terminal, Dock, Skills, Vault, and Harness. Historical Skill/Vault ACL
  rows do not constrain these MVP capabilities.
- Owner has Editor capabilities across the Instance and may manage all Projects,
  Agents, Show Pages, Instance configuration, and runtime administration.
- Show Page email grants and anonymous public pages remain confined to their
  existing signed/public page scopes.
- Unknown authenticated Instance API routes require Owner. They do not require
  local origin and never return `remote_execution_disabled`.

## Work

- [ ] Remove transitional backend authorization helpers, route matrices,
  payload projections, and product-error branches.
- [ ] Apply role and Project/Agent/Show Page ACL checks at HTTP, service,
  WebSocket, SSE, queue, and Harness enforcement points.
- [ ] Remove frontend origin/member fallbacks and render navigation, direct
  routes, selectors, and controls from capability projections.
- [ ] Replace obsolete tests with local/remote parity and representative
  Viewer/Editor/Owner plus ACL coverage.
- [ ] Run focused tests, Ruff, UI tests/build, and regression browser flows.

## Out Of Scope

- New identity/database schemas or migrations.
- Online revalidation redesign for persisted Harness work.
- Filesystem/Terminal sandboxing or dependency isolation through
  Skills/Vault/Harness.
- Cloud Organization management authorization redesign.
