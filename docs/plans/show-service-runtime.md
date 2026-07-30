# Show Service Runtime Integration Plan

## Summary

Show Pages are moving from static visual files to session-scoped visual
services.

Every Agent Session still has one Show Page URL. Vibe Remote owns that URL,
session visibility, authentication, public sharing policy, and remote-access
host checks. A managed Node sidecar owns the React/Vite runtime, hot reload,
shared UI dependencies, and optional server handlers.

The companion runtime project plan lives in `../../../vibe-show-runtime/docs/plan.md`.

## Product Goals

- Make Show Page the default visual collaboration surface for agent sessions.
- Let agents write React components with Vite hot reload.
- Let agents optionally add backend handlers without starting arbitrary web
  frameworks or allocating per-session ports.
- Keep one internal runtime sidecar, not one public dev server per session.
- Avoid per-session dependency installs and session-local `node_modules`
  growth.
- Keep the runtime independently releasable from Vibe Remote when the Python
  integration contract does not change.

## Runtime Modes

The target runtime model has two modes:

- `service`: the default managed runtime. It supports React UI and optional
  server handlers in one stack.
- `external`: a future advanced escape hatch for user-owned processes.

Static files are now compatibility fallback, not a first-class new-session
mode. Existing static pages still serve when the runtime is unavailable or when
the workspace contains only static assets.

## Architecture

```text
User browser
  -> /show/<session-id>/...
  -> Vibe Remote Web UI server
       - validates host and remote-access login
       - enforces Show Page visibility
       - strips UI credentials before proxying
       - starts or wakes Show Runtime on demand
       - proxies HTTP, service handlers, and Vite HMR WebSocket
  -> Vibe Show Runtime sidecar on 127.0.0.1:<internal-port>
       - manages active session Vite contexts
       - serves React app and assets
       - runs optional api/*.ts handlers
       - reuses shared dependencies
```

The sidecar binds only to loopback. Vibe Remote remains the only public entry
point through the local UI server and Avibe Cloud tunnel.

## Current Integration Contract

Vibe Remote proxies live Show Pages to the sidecar for both private and public
visibility:

```text
GET/HEAD /show/<session-id>/...
ANY      /show/<session-id>/api/...
WS       /show/<session-id>/__vite_hmr

GET/HEAD /p/<share-id>/...
WS       /p/<share-id>/__vite_hmr
```

Sidecar routes:

```text
GET  /health
POST /sessions/:sessionId/ensure
GET  /sessions/:sessionId/status
POST /sessions/:sessionId/suspend
ANY  /sessions/:sessionId/app/*
```

Important policies:

- private `/show/...` and public `/p/...` both serve live runtime pages and HMR
- public page rendering stays live, while write/event/handler/agent-action
  capabilities are controlled separately by visibility and permission policy
- offline pages keep the existing offline response
- Vibe Remote never forwards UI cookies, authorization headers, CSRF headers,
  or `Set-Cookie` between browser and sidecar
- HMR WebSocket separately checks local loopback or remote-access session
  cookie, because FastAPI WebSocket routes do not pass through HTTP
  `before_request` hooks

## Session Workspace

New `vibe show path` workspaces are React/Vite workspaces:

```text
~/.vibe_remote/show/<session-id>/
  index.html
  src/
    main.tsx
    App.tsx
    styles.css
  api/
    health.ts
```

Pure frontend page:

```tsx
export default function App() {
  return <main>Hello from this session</main>
}
```

Page with backend behavior:

```ts
export async function GET() {
  return Response.json({ ok: true })
}

export async function POST(request: Request) {
  const body = await request.json()
  return Response.json({ received: body })
}
```

Front-end code calls handlers through relative paths:

```ts
await fetch("./api/data")
```

## UI SDK And shadcn Alias Strategy

The default visual direction is shadcn-style. Agents should not run the shadcn
CLI inside each session, and Vibe Remote should not copy component source into
each workspace.

The runtime owns a shared UI package:

```text
@avibe/show-ui
@avibe/show-ui/button
@avibe/show-ui/card
@avibe/show-ui/dialog
@avibe/show-ui/input
@avibe/show-ui/progress
@avibe/show-ui/switch
@avibe/show-ui/theme
@avibe/show-ui/utils
```

To preserve agent prior knowledge, the runtime also supports familiar shadcn
import paths:

```tsx
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"
```

Those paths resolve to `@avibe/show-ui/*` inside the shared dependency store.

Theme customization uses the standard shadcn token contract (`--background`,
`--foreground`, `--card`, `--primary`, `--muted`, `--border`, `--ring`,
`--radius`, and peers). Complete CSS color values drive both semantic Tailwind
classes and native `var(...)` references. Dark mode overrides the same tokens
under `.dark` or `[data-theme="dark"]`; the optional provider is available at
the familiar `@/components/ui/theme` alias for runtime-computed subtree values.

## Shared Dependencies

Dependencies are owned by the runtime, not by each session.

Cloudflare Tunnel makes Vite dev-mode request count and cache-key stability
visible. The Show Page must stay live in every visibility mode, so the
performance fix is to cache immutable Vite optimized dependencies separately
from session source rather than serving a public snapshot.

Target cache policy:

- successful Vite optimized dependency responses: long immutable cache
- session HTML and injected config: no-store
- session source modules such as `src/App.tsx`: fresh/HMR-controlled
- runtime package `@fs/.../dist` paths: fresh until they move behind a
  content-addressed runtime asset namespace
- HMR WebSocket, event streams, and handlers: never cached

The current integration passes a shared runtime cache root to the sidecar so
Vite optimized deps do not live under each session workspace. Vibe Remote also
marks successful Vite optimized dependency responses as long-cacheable for both
session-local `.vite/deps/...`, Vite default `node_modules/.vite/deps/...`,
and relocated `@fs/.../vite-cache/.../deps` URLs. These immutable dependency
responses do not carry Show Page write-token cookies; session source modules
and in-place runtime dist paths stay fresh.

The next distribution step is the manifest/cache model described in
`show-runtime-manifest-cache.md`: Vibe Remote wheels should carry a pinned
runtime manifest, while official install and upgrade flows prepare only the
current platform's runtime archive into the global cache. This replaces the
temporary bundled-six-archive wheel strategy once implemented.

Vibe Remote resolution order:

1. `VIBE_SHOW_RUNTIME_BIN`, for local development or pinned custom runtime.
2. `avibe-show-runtime` on PATH.
3. Managed GitHub source install under Vibe Remote runtime state.

Managed GitHub source install:

```text
~/.vibe_remote/runtime/show-runtime/source/github/avibe-bot_vibe-show-runtime/main/
  package.json
  node_modules/
  packages/runtime/dist/cli.js
```

The default managed source is GitHub, so early runtime iteration does not
require publishing npm packages for every change:

```bash
VIBE_SHOW_RUNTIME_SOURCE=github
VIBE_SHOW_RUNTIME_GITHUB_REPO=https://github.com/avibe-bot/vibe-show-runtime.git
VIBE_SHOW_RUNTIME_GITHUB_REF=main
```

For stable releases, the same manager can use npm explicitly:

```bash
VIBE_SHOW_RUNTIME_SOURCE=npm
VIBE_SHOW_RUNTIME_PACKAGE_SPEC=@avibe/show-runtime
```

`VIBE_SHOW_RUNTIME_AUTO_INSTALL=0` disables managed install entirely.

## Lifecycle

Runtime state is automatic and demand-driven:

```text
created -> warming -> active -> idle -> suspended
```

Rules:

- opening `/show/<session-id>/` wakes a suspended session
- cold wake requires no CLI command or agent action
- active Vite contexts should be bounded by idle TTL and LRU pressure
- historical sessions should consume disk only, not watchers or module graphs

The first implementation starts the sidecar lazily from Vibe Remote. Runtime
context TTL/LRU enforcement belongs in `@avibe/show-runtime`.

## Dynamic Runtime Updates

Vibe Remote can update the Show Runtime independently if the sidecar API stays
compatible:

- runtime releases can be GitHub refs during fast iteration and npm package
  versions for stable channels
- Vibe Remote installs them under managed runtime state
- active sessions keep their current process until idle or restart
- new sessions use the currently installed package
- template migrations must be explicit and idempotent

Vibe Remote still needs a Python release when routing, auth, CLI, storage, or
prompt behavior changes.

## Remaining Work

- Publish `@avibe/show-runtime`, `@avibe/show-ui`, and `@avibe/show-sdk` under
  the `@avibe` npm organization when the runtime API stabilizes.
- Add runtime status to `vibe show status`.
- Add sidecar logs/error surfacing to the UI.
- Implement active-context TTL/LRU limits in the runtime package.
- Add a stable runtime asset namespace and cache policy so immutable runtime
  resources can be reused across session and share URLs without disabling HMR.
- Keep public write/event/handler permissions explicit; public page rendering
  and HMR remain live by default.
- Expand `@avibe/show-ui` component coverage and default visualization
  dependencies.
