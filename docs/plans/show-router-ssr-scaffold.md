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

Never rewrite an existing editable router during initialization or a request.
An editor does not participate in Avibe's locks, and a final comparison followed
by rename cannot prevent overwriting a save in that window.

Runtime's existing SSR module plugin supplies its current generated router only
when the module source exactly matches the released Python History-router
template. Match complete source fingerprints, not the absence of an SSR export.
The released LF and Windows CRLF SHA-256 values are respectively
`1154739b3e21e2f1c7f45e3d0b7454dc5541fdf15e2c79bbc2f96f766338706e` and
`ed7cbd0aa11a491ac8b7621b8a7ea64d7c83c0b53ec46e950cca95b7f4d0079e`.
Fingerprint the exact source being transformed, rather than rereading a path
whose contents may change. Reuse the authored Runtime template without copying
its implementation or creating temporary workspaces.

This compatibility applies only to the Markdown SSR environment of the ordinary
workspace router module. Ordinary API SSR, browser HTML/client modules, and files
on disk remain unchanged. Unknown/custom,
hash, routerless, and symlink routers retain existing behavior. Module
invalidation must observe later custom edits. Exercise HTML-first and
Markdown-first requests, LF/CRLF, non-ASCII routes, and concurrent edits.
Retain the legacy router's existing exported fields and semantics, including
`routes[].dynamic`, when adding SSR support. Legacy-compatible root renders keep
their existing request-scoped read-only location facade and static Motion
configuration; genuinely modern providers do not acquire browser globals.
An internal compatibility variant may extend the shared Runtime author without
changing its default fresh-scaffold output or creating another implementation.
Avibe's fresh scaffold works with the existing default-branch Runtime contract;
old-stock SSR compatibility additionally requires Runtime companion PR #70.

### Review scope decision

The first reviewed Avibe head, `638b7a78ec`, received three findings: the
check/rename race, read-only filesystem errors, and blocking request-path I/O.
There is one findings-bearing head and no repeated-class circuit-breaker trigger.
The orchestrator removes the source migration rather than adding further
checks or locks that external editors cannot honor. Loading compatibility in
Runtime preserves the intended old-page SSR outcome without mutation, and
removes the other two findings at their source. No database or public protocol
changes are required. Runtime owns the compatibility implementation; Avibe owns
the generated resource, packaging, and cross-repository acceptance.

The Runtime compatibility rewrite subsequently received findings on two reviewed
heads: `17637f8477` changed ordinary API router exports, and `0c9d99c0a7` still
changed those exports for Markdown consumers and removed the legacy root location
facade. These share a root cause: treating a legacy-compatible router as a wholly
modern router. The repeated-class circuit breaker stopped further edits/pushes.
Independent HTTP comparison additionally found that the same transition removed
the legacy static Motion configuration. The orchestrator's scope decision retains
all three existing contracts inside the shared template and entry/worker flow,
without filesystem migration, a second module graph, broader sandbox globals, or
new invalidation behavior. Real legacy page consumers in the combined integration
test verify route fields, root location, and static rendering against the released
producer as well as the Runtime companion.

## Runtime error boundary

The worker rejects a non-root request without a renderable `SsrRouterProvider`
with the stable code `router_not_ssr_capable`. The parent recognizes this typed
worker error and returns HTTP 502 with that code and the fixed public message:

> This Show Page router does not support Markdown for subpages. Export an SSR-capable SsrRouterProvider from src/router.tsx.

The structured failure log records the same code and message. Never expose an
arbitrary workspace error or stack. Root-only fallback remains supported. The
Runtime error improvement is independently deployable; the Python scaffold fix
works with the already released SSR provider contract.
Avibe's HTTP error allowlist also recognizes `router_not_ssr_capable`, so the
fixed Runtime message reaches both private and public callers unchanged.

## Regenerating and checking the source resource

The checked-in resource was generated from Runtime commit
`5a9a6a52f2ae03611d617a659bfd0c1c32389478`. After building the Runtime checkout:

```sh
node scripts/sync_show_router.mjs --runtime-module ../vibe-show-runtime/packages/runtime/dist/templates.js
node scripts/check_show_router.mjs --runtime-root ../vibe-show-runtime --python .venv/bin/python
```

The `show-router-integration` CI job pins that producer and checks byte parity,
then runs the real HTTP renderer for fresh nested SSR and legacy root/HTML
fallback while asserting that no source files change. Combined acceptance
against Runtime companion PR #70 adds `--legacy-router-ssr` to the same command
to require legacy LF/CRLF nested SSR as well. The default-branch CI does not
pretend that the old producer implements this new compatibility behavior.
When intentionally refreshing this resource,
update that producer pin too. Official and GitHub-only releases export the router
from each actual platform build, verify parity, and package that output alongside
the matching generated manifest. No Node process is needed to create a page.

## Acceptance

- Real Markdown renders for root, static and dynamic routes, query strings,
  non-ASCII values, and private/public bases; generated links retain their base.
- Real HTML remains available for the same workspace.
- Known old stock routers render nested Markdown through Runtime compatibility
  while retaining their bytes and metadata. Initialization and requests never
  rewrite existing routers, including unknown/custom/legacy/symlink routers.
- Custom non-SSR routers receive an actionable, sanitized error for subpages.
- Legacy pages consuming exported route fields and the root-only location facade
  keep working; root Motion rendering remains static. Private/public requests do
  not reuse another request's location. Fresh scaffold output remains unchanged.
- Wheel and sdist contain the generated router resource.
- Release export and integration checks detect future template drift.

No database migration, runtime restart, production update, or bulk live-workspace
rewrite is part of this change.
