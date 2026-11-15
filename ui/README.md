# Avibe Web UI

## Development toolchain

Use Node.js `^20.19.0 || ^22.13.0 || >=24` (UI CI uses 22.23.4), then run
`npm ci`. TypeScript 7 is the only installed compiler. Oxlint is the lint engine;
React plugins and two core-rule compatibility implementations still require
ESLint 10, so the Node support range has not changed.

```sh
npm run dev
npm run build
npm run typecheck:tests
npm run typecheck:design
npm run lint
```

`npm run build` validates out-of-tree imports, runs `tsc -b`, and builds with
Vite. TypeScript's native compiler is a development tool, not a browser runtime
optimization. The React Compiler is not enabled in the application build; its
lint diagnostics are enforced through the React Hooks plugin.

## Isolated headless browser scripts

Use `scripts/safe-browser.mjs` for small local UI probes and captures.
`npm run design:shot` uses it automatically. It launches only Playwright's
bundled headless Chromium, with an ephemeral profile/context, mock Keychain and
basic password storage; it accepts no headed/focus/debug, custom launch flags,
existing profile, or stored credentials. `PWDEBUG` is refused on every OS.

```sh
node scripts/safe-browser.mjs   # launch, self-check, close; no capture
npm run test:safe-browser      # focused guards + one small browser smoke
```

For an ad-hoc script in `ui/scripts/`:

```js
import { launchSafeBrowser, assertSafePage } from './safe-browser.mjs';
const browser = await launchSafeBrowser({ contextOptions: { viewport: { width: 900, height: 700 } } });
try {
  const page = await browser.context.newPage();
  // Navigate to a test-owned fixture, then exercise/assert its UI.
  await assertSafePage(page); // also call after navigation and before capture
} finally {
  await browser.close();
}
```

Before page scripts (including frames/popups/navigation), a context init script
replaces `navigator.clipboard` read/write APIs and `execCommand` copy/cut/paste,
and isolates Tauri/native IPC. The launch self-check verifies identities before
calling anything, exercises each stub, and closes the browser on failure. Calls
are recorded as operation names only; text/items stay in page memory, never in
logs or the OS clipboard. Native design fixtures use `setNativeMock` for their
in-page peer; clipboard IPC still cannot reach it. This is browser automation
isolation, not a sandbox for hostile scripts or an OS-level audit.

Existing Playwright suites are **not** globally migrated by this helper. Use the
CI Playwright container from `.github/workflows/lint.yml` (or local Incus) for
repeats, cross-engine checks and captures that do not need this Mac. Installing
`installBrowserIsolation(context)` alone does not make an externally launched
browser headless or isolate its profile/Keychain. Captures still need sensitive
mnemonic, passphrase, quiz and suggestion content masked **before saving**; this
tool does not infer which UI content is sensitive.

## Lint policy and existing debt

`npm run lint` checks every repository-owned TS/TSX file under `ui/`, excluding
only dependency/build directories, against the reviewed per-file/per-rule debt
ledger. `.oxlintrc.json` is independently checked against `scripts/lintPolicy.mjs`.
The gate verifies file coverage, rule options/severity, browser globals, inline
configuration, parse/plugin failures, and both visible and suppressed diagnostics.
Do not add presets or regenerate the ledger merely to make errors pass.

- `npm run lint:report` prints diagnostics, including existing debt.
- `npm run lint:baseline` records deliberate debt reductions after integrity
  checks pass. Review the diff; it is not permission to add new debt.
- `eslint-baseline.json` and its historical rule IDs remain the one ledger.
  Their names do not imply a second lint run or a legacy TypeScript dependency.
- Hooks disable directives use `react-hooks-js/…`, the JS plugin's Oxlint alias.
  Adding a disable cannot hide new debt: the gate measures suppressed messages
  independently in an isolated copy with the directive keywords neutralized.

TypeScript, Oxlint and the Oxc parser are exactly pinned because the compiler API
and JS-plugin interfaces are still evolving. The
[TS7/Oxlint migration](../docs/plans/typescript-7-oxlint-migration.md) records
compatibility boundaries and measurements. The
[ESLint 10 migration](../docs/maintenance/eslint-10-migration.md) records the
preceding, one-time baseline remeasurement; it is historical, not the current
engine configuration. These development requirements do not change the
separately packaged Show Runtime's Node contract.

Production TSX enforces `avibe/localized-user-text`: put visible copy and
sentence/list composition in locale keys, `Trans`, or locale-aware formatters,
not literal JSX text, templates, `+`, or `.join` glue. JSX children and
`aria-label`, `title`, `placeholder`, and `alt` are checked, including immutable
local aliases. The [localized-text guard contract](../docs/plans/ui-localized-text-guard.md)
documents narrow decorative/technical exceptions and bounded analysis. Existing
debt is not a budget for new copy. Equal-count replacement within one file/rule
pair remains the documented limitation of the count ratchet.
