# TypeScript 7 and Oxlint migration

## Goal and change contract

Replace the UI development toolchain with TypeScript 7.0.2 and Oxlint, without a
TypeScript 5/6 compatibility package or a maintained fork of typescript-eslint.
This is a complete replacement of the incomplete version-only proposal in #3212;
that PR remains untouched until an explicit disposition. Implementation starts
from master `734485c83a6a5c0e921236bb035e7b8d2e23279e`.

- Preserve the existing UI development Node range and all runtime dependencies.
- Preserve the lint domain: every repository-owned TS/TSX file under `ui/`, with
  only `node_modules` and `dist` excluded. New files cannot escape the gate.
- Preserve effective rule severity/options and the `_` unused-binding convention;
  replacement implementations must demonstrate the intended detection behavior.
- Preserve React Hooks/Compiler and Refresh checks and the localized-user-text
  rule's scope/import-identity/immutable-binding analysis.
- Preserve separate per-file/per-rule violation and suppression ratchets,
  mandatory rationale, stale-budget reclamation, fatal parse/plugin failures, and
  prevention of inline/configuration-based policy bypasses. Existing equal-count
  replacement within a file/rule pair remains the documented limitation.
- Migrate all direct compiler API consumers and removed configuration/CLI paths.
  The i18n guard must retain semantic string-union/template-key analysis, not
  degrade to text matching. Parse/traversal checks must remain structural.
- Do not migrate formatting, enable new broad lint presets, fix unrelated runtime
  debt, change CI concurrency, or update services/regression instances.

## Approach

Use Oxlint's native rules for the existing untyped TypeScript/core checks.
Retain the exact React plugin behavior through Oxlint's JS plugin compatibility
where needed; any ESLint package retained solely to satisfy these plugin peers
or supply a missing core rule is not a second lint engine or an old TS compiler.
Verify the published package graph, rather than hiding an old compiler in
transitive dependencies. Oxlint JS plugins are alpha: pin the validated release
and cover the actual used APIs and rules behaviorally.

Keep one user-facing lint gate and one debt ledger. Inspect native rule coverage,
configuration resolution, file coverage and suppression reporting before
selecting the smallest complete gate implementation. Do not regenerate a
baseline until all behavior differences have been explained on unchanged source.

For compiler APIs, use the exact published TS7 APIs where suitable; AST-only
checks may use Oxc without duplicating semantic type checking. Do not assume
unreleased 7.1/main APIs exist in 7.0.2.

## Validation and delivery

1. Capture the existing toolchain's real diagnostics, suppressions, domain and
   targeted policy/convention/localization tests on a frozen base tree.
2. Establish the Oxlint rule/API compatibility matrix and negative behavior
   probes, then migrate configuration, gate and its existing tests together.
3. Migrate compiler API consumers; run their focused tests, test/design type
   checks, import/theme guards and UI build.
4. Fresh locked install and package graph prove no old TypeScript or
   typescript-eslint remains. No force/legacy-peer-deps installation.
5. Compare old/new diagnostics on identical application source. Preserve every
   unexplained detection gap as a blocker, not lower counts disguised as fixes.
6. Measure full lint gate, raw lint and compiler check time separately, including
   React/custom rules, plus process peak memory where comparable. Record host,
   repetition and cache conditions; never substitute upstream benchmark claims.
7. Push a focused non-draft implementation PR; exact-head Codex review, CI and
   zero unresolved findings are mandatory. User authorization is implementation,
   not merge, deployment or closing #3212.

## Implemented compatibility boundaries

- TypeScript **7.0.2**, Oxlint **1.91.0**, and `oxc-parser` **0.157.0** are
  exactly pinned. There is no `typescript-eslint`, `@typescript-eslint/*`, or
  TypeScript 5/6 package in the installed graph. Runtime package versions and
  tarball identities are unchanged, including Monaco's bundled browser worker;
  this migrates the repository compiler, not the editor's runtime implementation.
- `.oxlintrc.json` contains an independent, explicit rule/global configuration.
  All 87 enforced rules keep their existing ledger identities. Native aliases
  are normalized; Hooks uses `react-hooks-js` because its original namespace is
  reserved in Oxlint. Existing disable comments change only that namespace.
- Oxlint is the only lint execution engine. `eslint` remains as the React
  plugins' peer and supplies `no-octal` (missing natively) and
  `no-useless-assignment` (native analysis misses two existing try/finally
  patterns) through the JS plugin. There is no copied/forked rule implementation.
  `preserve-caught-error` drops only the unsupported, behaviorless empty
  `errorClassNames: []` option, retaining `requireCatchParameter: false`.
- The complete input config is compared, not `--print-config`, which omits JS
  rules in this release. Fixed CLI flags disable nested configuration and ignore
  files. Explicit file input, engine discovery, and parsed-file counts verify
  the same TS/TSX domain in both directions. JS/MJS/CJS retain fatal parse checks
  without inheriting the TS rules.
- Suppressions are measured by neutralizing only parsed disable/enable keywords
  in isolated copies and comparing diagnostic multisets. Strings, tokens,
  line/byte offsets, original source and rule configuration remain unchanged.
  Visible diagnostics must survive this replay, or the gate fails closed.
  A second Oxlint suppression ledger is rejected.
- Eight direct compiler consumers use the published `typescript/unstable/*`
  APIs. `typescriptSource.mjs` owns hermetic single-file parsing and token-gap
  recovery; AST-only traversal loses comments before punctuation. Snapshots are
  disposed between parses; the upstream API owns process-exit cleanup. The i18n
  guard still uses a real checker and resolves string unions/template keys.
  Vite's existing `transformWithOxc` replaces the one `transpileModule` call.
- Removed `baseUrl` and the design config's duplicate path mapping; launcher
  discovery goes through the exported TypeScript `package.json`. The only
  application edit removes an unreachable always-null fallback newly rejected
  by TS7. A FormData test now narrows its value instead of asserting through a
  possibly missing optional-chain result. The migrated B7 test's stale callback
  title is aligned with its existing source; the old compiler also fails that
  lookup, independently reproduced before fixing it.

## Local acceptance evidence (2026-11-04)

On macOS arm64, Node 22.23.4 / npm 11.6.2, in independent task-owned worktrees:

- Old gate: 192 focused tests pass. Candidate: **867 tests across 26 files**
  (all scripts plus the five direct unit-test API consumers), and **136** focused
  harness-lifecycle/FormData tests pass. The 198 lint contracts are included in
  the 867, not additional. Three token/comment regressions extend the existing
  theme-guard tests; a JSX scanner hang is bounded in a subprocess.
- Node **20.19.0**, the minimum supported release, passes 291 targeted lint,
  compiler-consumer and launcher tests and the full UI build. The upstream
  RuleTester itself requires Node >=22; localized-rule cases therefore run
  uniformly through the actual CLI in per-test fixture batches, without a
  Node-range change, skipped assertions, or a parser shim. The same final harness
  also passes all 97 localized cases on Node22.
- **53 hermetic Model Hub cleanup tests** pass, including the actual B7 callback
  through Oxc type stripping. No live instance, user browser or gateway is used.
- `npm run build`, test/i18n-guard typechecking, design typechecking, import and
  theme guards pass. Build retains its pre-existing crypto externalization and
  chunk-size warnings. Full repository tests and browser suites remain CI gates.
- Fresh `npm ci` and `npm ls` pass without force/legacy-peer-deps. npm 11's update
  pruned two bundled `@emnapi` entries from the unchanged Tailwind WASI package;
  preserving their exact original lock records fixes clean installation. Other
  surviving lock entries change only peer/libc metadata, not package versions or
  tarball identities, except the intended TypeScript upgrade.
- Final diagnostic replay: **1381 TS/TSX files, 766 errors, 46 suppressed
  messages**, identical in every file/rule/severity pair to the frozen old run.
  `eslint-baseline.json` is byte-for-byte unchanged. The new `.d.mts` helper type
  declaration is outside the established `.ts`/`.tsx` lint domain, but participates
  in consumer typechecking; this migration does not redefine that domain.

### Observed stage costs, not an end-to-end CI speedup

Three sequential rounds per toolchain on the same loaded machine and frozen
base, installed dependencies, existing filesystem caches, no claim of idle-host
or cold-cache control. `tsc -b --force` forces each compile. Raw lint exits 1 for
existing debt by design; the complete gate passes. Values are wall-clock seconds.

| Stage | Old runs | Candidate runs | Old median | Candidate median |
| --- | --- | --- | ---: | ---: |
| Complete lint gate | 30.00 / 33.43 / 25.49 | 28.23 / 28.46 / 28.52 | 30.00 | 28.46 |
| Raw lint, all configured React/custom rules | 18.00 / 21.98 / 16.90 | 22.37 / 23.68 / 22.38 | 18.00 | 22.38 |
| Forced compiler build | 29.40 / 27.01 / 33.25 | 5.28 / 4.75 / 5.46 | 29.40 | 5.28 |

The clear gain is the compiler stage (about **5.6×** in these runs). The complete
lint gate is roughly unchanged relative to old-run variability, and raw lint is
slower here; native-only marketing benchmarks do not describe this React/JS-plugin
workload. Keeping detection takes priority over removing compatibility rules for
a more attractive speed number. This does not accelerate the running UI itself.

For completeness, `/usr/bin/time -l` median reported max RSS (MiB) was
2817→767 for the gate, 2997→783 for raw lint and 2866→2331 for compilation.
These are OS-reported process/child high-water marks, **not sampled simultaneous
whole-process-tree memory**. Different native/JS process architectures prevent a
claim that total machine memory fell by those ratios.

## Progress

- [x] Current branch, dependency and lint-gate inventory; isolated task worktree.
- [x] Published API/rule probes and baseline evidence.
- [x] Oxlint and compiler migration.
- [x] Contract, build and dependency verification; measured performance.
- [x] Minimum-supported Node 20 verification.
- [ ] Final base integration.
- [ ] PR review/CI and merge-ready handoff.
