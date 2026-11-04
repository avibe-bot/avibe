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
