# Shared MCP placeholder ownership

## Change contract

Global Avibe-owned and project definitions accept `${VAR}` in URL, env values,
and header values. Missing variables persist and appear as `needs_config` with
names. One Configure flow (owner-only Web and CLI) updates private values;
successful discovery alone makes tools callable. Failed discovery leaves the
valid definition manageable but non-callable. Native imports and released global
normalized definition files remain compatible. Literal URL credentials are not
newly masked by this work.

Per-key replacement moves supplied env/header keys between template and private
storage; unspecified keys survive. Management displays template URL and metadata,
never substituted URL or private maps. Project approval remains project-only.

## Ownership design

Extend `scope.load_server` and its effective-definition validation to own all
Avibe-owned entries. Reuse its template substitution, missing-variable state,
publication comparison, and safe summary. Keep native parsing separate because
native imports have different ownership. Scope-specific file reads/writes are
storage adapters, not independent configuration lifecycles. Preserve the released
global normalized file through a load adapter; avoid a migration or generic store
framework.

Extract the existing project per-key partition operation for both storage paths.
Validate prospective raw/private values before any write. Runtime owns mutation
admission and discovery under its existing package lock. New calls must stop
using old publication before private values change. Already executing calls may
finish; no guarantee of a revision-bound secret snapshot is made. Failed native
file removal must not retire publication or OAuth state.

## Validation plan

At the scope/runtime boundary, parameterize global/project tests for missing and
mixed templates, configuration, per-key replacement, invalid no-write, failed
refresh visibility and non-callability. Existing tests cover project-only parsing
and global eager discovery separately; they do not protect the shared lifecycle.
Add released-global fixtures and rejected-native-deletion regression. Demonstrate
regressions fail on the original master for the intended reason. All fixtures use
temporary home/config and stub remote discovery. Verify UI with fixture APIs in
real Chrome at desktop and 390px; run focused Python/UI checks, Ruff and UI build.

## Delivery

New branch from origin/master f47433a7a; no old patch-series reuse. No installed
runtime changes, restart, deployment, or merge. One durable review/CI Watch before
push; new PR must reach clean exact-head Codex review, no unresolved threads,
and passing required CI.

## Reuse inventory (PR #3203 / fb5bfe945)

Reviewed the merged project implementation across scope/repository/runtime,
CLI/API, prompt and Web. Reuse extends beyond placeholder parsing:

| Responsibility | Existing owner | Shared approach | Scope difference |
| --- | --- | --- | --- |
| Validation and effective definition | scope._effective/load_server | One loader, namespace and structural validation | Project cwd resolution; global sanitized default |
| Add/replace and per-key template/private partition | scope.write_server | One prospective-entry builder and runtime add flow | Project document CAS/format preservation; global normalized definition file |
| Configure/clear | scope.configure, CLI/API owner guards | Same map replacement, omitted-map preservation, validation before write | Native imports cannot Configure |
| Status/management visibility | ScopedServer.summary | Same missing/state/publication comparison and template-safe metadata | Source/path and project approval |
| Discovery/publication/call gating | runtime refresh/_admit | Existing package lock also serializes admission; drop publication on accepted mutation before refresh | Approval blocks project discovery |
| Connection retirement | runtime._retire_package | Existing busy-call retirement for both scopes | None |
| Deletion | repository.remove_package/runtime._remove | Only retire runtime state after storage accepts deletion | Native file editing remains its own adapter; project document removal |
| OAuth | runtime scoped keys/providers and oauth storage | Existing scoped flow; cancel login on accepted configuration changes | Project callback return URL |
| Prompt/list/help | scope.summaries, prompt, CLI detail | Consume same state; advertise only matching successful publications | Project shadowing/root identity |
| Web Configure/status | McpHubSection, shared map editor | Enable same controls for Avibe and project rows | Inherited global rows remain view-only; Approve is project-only |

Configure replaces each supplied map (including `{}` to clear), preserving omitted
maps; add/replace retains the existing per-key partition semantics. These are
intentionally different operations. Native source imports retain native parser
semantics and do not gain placeholder substitution through this work.

## Implemented guarantees and evidence

Both scopes now use `write_server`'s key partition, `load_server`, `configure`,
`ScopedServer.summary`, and the same runtime add/refresh path. The package lock
covers admission and mutation, not the duration of tool execution. Accepted
mutations remove the publication before discovering again. A rejected validation
or native deletion leaves the working publication and OAuth runtime intact.
Global normalized files and legacy snapshot defaults load without rewriting them.
Discovery errors for owned servers retain their error type but omit transport
exception text because it can echo substituted URL/header credentials. Native
transport diagnostics and literal URL display are unchanged.

Validation (2026-11-04):

- 299 focused Python tests passed, including all project-scope, native and OAuth
  cases and AUTH-SETUP-138's shared template/configure/OAuth scenario.
- The new owning-boundary suite on original master: 12 failed / 1 passed. Failures
  demonstrate global placeholder rejection, project discovery error disclosure,
  admission during writes, native-delete OAuth retirement and missing global
  management visibility; the existing project partition case already passed.
- 26 focused UI tests passed; UI build, UI lint baseline, E2E TypeScript and changed
  Python Ruff checks passed. Follow-up idempotence checks passed (6 cases).
- Real Chrome (fresh profile, full product page, fixture APIs, dead backend port):
  desktop 1200px and mobile 390px Configure/save/tool-list flows passed. Captures
  are under `ui/e2e/.artifacts/mcp-placeholder/{configure,configured}-{1200,390}.png`.
  No denied write reached a real service. Reproduce with
  `cd ui && npx playwright test --config playwright.workbench-general.config.ts mcp-configure.spec.ts`.

One forever combined Watch (`9084a6861000`, both timeouts zero) was armed before
first push. Its branch bootstrap records an empty pre-push baseline, waits for
the new PR, then catches up once into the bundled PR/CI waiter's durable cursor;
it never reseeds between heads. The installed runtime was not changed.

Review follow-up: discovery errors now carry a backend catalog key over IPC;
Web translates single and batch failures, while CLI retains explicit English.
Repeated global imports compare the prospective partition via the shared scope
rules, preserving template intent and treating released inline literals as the
same effective values. The matching published revision is still required.
The three review regressions fail on the reviewed head; 44 configuration/API
cases pass after the fixes, alongside 170 shared runtime/project/scenario cases
and a released-inline-value replay regression. Template-to-literal edits remain
explicit replacements even if they would resolve to the same value.
