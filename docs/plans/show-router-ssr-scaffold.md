# Show Page router SSR contract

Issue: avibe-bot/avibe#1984.

## Outcome and ownership

Fresh Python-created Show Pages must render their actual nested route as Markdown.
The Runtime's `packages/runtime/src/templates.ts` owns the router implementation.
Avibe ships its generated `src/router.tsx` as a package resource, rather than
maintaining another implementation in a Python string. The existing Runtime
`ensureSessionTemplate()` API produces that resource; no new Runtime API or
unmerged dependency is required.

Release builds export the router from the same Runtime build as their archives
and require identical output across platforms. A checked-in generated resource
keeps source checkouts and offline creation usable. A cross-repository test uses
the real Python initializer and the real Runtime Markdown endpoint, and checks
the packaged resource against the Runtime generator.

## Existing workspaces

Only a byte-for-byte known, released History-router template is eligible for an
automatic upgrade. Match its complete SHA-256, not the absence of an SSR export.
Preserve custom routers, hash routers, routerless apps, pages, App.tsx, and styles.
Do not follow router symlinks. Repeated initialization must leave upgraded and
custom workspaces unchanged. Exercise both HTML initialization and Markdown-first
requests, because the latter currently bypass Python initialization.

## Runtime error boundary

The worker rejects a non-root request without a renderable `SsrRouterProvider`
with the stable code `router_not_ssr_capable`. The parent recognizes this typed
worker error and returns HTTP 502 with that code and the fixed public message:

> This Show Page router does not support Markdown for subpages. Export an SSR-capable SsrRouterProvider from src/router.tsx.

The structured failure log records the same code and message. Never expose an
arbitrary workspace error or stack. Root-only fallback remains supported. The
Runtime error improvement is independently deployable; the Python scaffold fix
works with the already released SSR provider contract.

## Acceptance

- Real Markdown renders for root, static and dynamic routes, query strings,
  non-ASCII values, and private/public bases; generated links retain their base.
- Real HTML remains available for the same workspace.
- A known old stock router upgrades on first relevant access; a second access
  changes nothing. Unknown/custom/legacy/symlink routers are not rewritten.
- Custom non-SSR routers receive an actionable, sanitized error for subpages.
- Wheel and sdist contain the generated router resource.
- Release export and integration checks detect future template drift.

No database migration, runtime restart, production update, or bulk live-workspace
rewrite is part of this change.
