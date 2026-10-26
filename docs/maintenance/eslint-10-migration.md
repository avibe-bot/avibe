# ESLint 10 companion migration

## Contract

Upgrade the UI's lint tools together, without changing UI runtime code or weakening
the lint gate. This replaces the incomplete ESLint-only update in #2844.

- ESLint: 9.39.2 -> 10.12.0; `@eslint/js`: 9.39.2 -> 10.0.1.
- React Hooks plugin: 7.0.1 -> 7.1.1, still exactly pinned.
- Keep React Refresh at 0.4.26 (`eslint >=8.40` peer) and typescript-eslint at
  8.71.2 (explicit ESLint 10 peer). Refresh 0.5's rule/config rewrite is not
  necessary for this migration; do not silently take it with the Hooks update.
- UI development Node range: `^20.19.0 || ^22.13.0 || >=24`, matching ESLint 10.
  UI CI uses Node 22.23.4. This is not a change to the separately
  packaged Show Runtime's Node requirement.
- Keep all enforced rules, severities, source coverage, inline-configuration
  guards, and the per-file/per-rule debt ratchet. No peer-dependency bypass,
  additional suppression, or runtime refactor is part of this change.

## Why #2701 pinned Hooks

#2701 correctly rejected a blind preset update: Hooks 7.1 removed
`component-hook-factories`, and 7.1.1 only restored its name as a deprecated
no-op. Adding that name back to the config would not restore detection.

Inspection of the **published 7.0.1 bundle**, rather than the rule name alone,
shows its diagnostic was guarded by
`validateNoDynamicallyCreatedComponentsOrHooks`, defaulting to `false`.
Neither the plugin's lint `COMPILER_OPTIONS` nor Avibe's configuration overrides
that flag. Upstream removed it as an unused experiment in
[React #35825](https://github.com/facebook/react/pull/35825); the no-op is documented
in [#36307](https://github.com/facebook/react/pull/36307).

Two real-config probes confirmed that a module-level factory returning a nested
component, and one returning a nested hook, produced no factory diagnostic with
7.0.1 or 7.1.1. Thus removing the dead entry from the policy snapshot does **not**
remove actual enforcement. It does not assert that arbitrary factories are safe.
The distinct `static-components` rule still rejects a component created and
rendered inside another component in both versions. No custom replacement or
no-op rule is installed.

## Reviewed effective-policy changes

`eslint.config.js` itself is unchanged. The checked-in resolved-policy snapshot
changes only where the new upstream presets/defaults change:

| Change | Effect |
| --- | --- |
| `no-unassigned-vars`, `no-useless-assignment`, `preserve-caught-error` | Newly enabled ESLint 10 recommended errors |
| `no-shadow-restricted-names.reportGlobalThis: true` | Stricter default, now rejects shadowing `globalThis` |
| `no-constant-binary-expression.checkRelationalComparisons: false` | Explicit new option preserves previous relational-comparison behavior |
| Core `no-unused-vars` explicit default options | Still disabled by typescript-eslint in favor of its enabled TS rule |
| No `component-hook-factories` entry | Retired inactive experiment, as verified above |

The TypeScript parser, browser globals, file domain, all other rule options and
severity levels, and unused-disable reporting stay unchanged. See the
[ESLint migration guide](https://github.com/eslint/eslint/blob/v10.12.0/docs/src/use/migrate-to-10.0.0.md)
and [Hooks changelog](https://github.com/facebook/react/blob/d1727fbf987392749b374f3afe62227d2c70fc41/packages/eslint-plugin-react-hooks/CHANGELOG.md).

## Remeasuring existing debt

Both versions linted the **same source tree** at base `c3b9b0bc2`, with clean
installs. No `ui/src` or `ui/e2e` file was edited. The old gate passed with 190
errors. The new tools report 472 errors across 102 files (previously 38), and the
44-entry suppression tally is identical, including every file/rule count.

This is an explicitly reviewed baseline migration, not routine permission to
increase counts. Hooks 7.1 improves effect/ref analysis and reports multiple
compiler diagnostics instead of stopping after the first. The ledger records
newly visible **existing** debt; it does not claim all diagnostics are new
runtime bugs. The compiler is not enabled in the application build.

| Error rule | Old | New | Reproducible example in unchanged source |
| --- | ---: | ---: | --- |
| `@typescript-eslint/no-explicit-any` | 168 | 168 | Existing API boundary debt; unchanged |
| `react-hooks/set-state-in-effect` | 14 | 153 | `QuickReplies.tsx`: reset state in an effect |
| `react-hooks/refs` | 0 | 98 | `WorkbenchProjectsProvider.tsx`: write current-state/runner refs during render |
| `react-hooks/preserve-manual-memoization` | 7 | 26 | `ChatPage.tsx`: inferred callback dependencies differ from the explicit array |
| `react-hooks/immutability` | 0 | 11 | `VersionBadge.tsx`: an effect closes over later declarations; `ChatPage.tsx`: ref mutation after passing it to a hook |
| `react-hooks/purity` | 0 | 3 | `RemoteAccess.tsx`, `OAuthConnectDialog.tsx`, `useBackendOAuth.ts`: render-time `Date.now()` |
| `react-hooks/static-components` | 0 | 1 | `ChannelList.tsx`: render-local `Wrapper` component |
| `no-useless-assignment` | 0 | 10 | `ShowPageShareControl.tsx`: initial `nextAccess = null` overwritten before use |
| `preserve-caught-error` | 0 | 1 | `useBackendRuntime.ts`: recovery readback throws inside an outer catch without retaining its error as cause |
| `react-refresh/only-export-components` | 1 | 1 | Existing `ApiContext.tsx` export shape; unchanged |
| **Total** | **190** | **472** | **+282 newly reported errors, no runtime edits** |

The immutability forward-reference messages describe compiler constraints: a
closure capturing a later declaration does not by itself demonstrate a runtime
temporal-dead-zone exception. Similarly, the recovery readback error describes
a separate failure, and retaining the outer error as cause requires a deliberate
provenance decision. These are recorded diagnostics, not claims of observed
runtime failure. Render-time latest-value refs and timers need their own
consumer/lifecycle verification before refactoring. Rule-specific rationales
remain in `ui/eslint-baseline.json`.

The gate implementation is unchanged: an unrecorded pair fails, growth fails,
and a reduced count fails until its allowance is reduced too. Suppressions are
independently counted and cannot grow silently. Later debt fixes therefore
continue to reclaim budget rather than making room for new violations.

## Behavioral and installation evidence

The existing real-config tests in `ui/scripts/eslintConventions.test.mjs` now
probe conditional Hooks, render-local components, ref reads/writes, effect
setters, mutation, purity, and the new ESLint rules. They include valid
counterparts and require error severity, not merely a configured rule name.

The key dual-version probes were:

| Probe | ESLint 9 / Hooks 7.0.1 | ESLint 10 / Hooks 7.1.1 |
| --- | --- | --- |
| Component/hook factory without the experimental flag | No diagnostic | No diagnostic |
| Render-local component used as JSX | `static-components` error | Same error |
| Conditional `useState` | `rules-of-hooks` error | Same error |
| Render ref read | `refs` error | Same error |
| Synchronous effect setter derived from a prop | Missed | `set-state-in-effect` error |
| Effect setter and ref read in one component | Only `refs` | Both errors |
| Event-only state setter / module-level child | No relevant error | No relevant error |
| Overwritten assignment / lost caught cause / shadowed globalThis | No relevant error | New recommended/default errors |

The combined-diagnostic probe fails on the old tools for the intended reason;
the existing policy/baseline tests continue to verify tampering detection and
both upward and downward count drift. Fresh `npm ci` and `npm ls` verify the peer
graph without `--force` or `--legacy-peer-deps`. Use a clean dependency directory
when regenerating the lock: npm 11 can otherwise prune optional cross-platform
entries from an existing platform-specific installation.

Reproduce from `ui/`:

```sh
npm ci
npm ls eslint @eslint/js eslint-plugin-react-hooks eslint-plugin-react-refresh typescript-eslint
npm run lint
npm test -- scripts/eslintConventions.test.mjs scripts/lintPolicy.test.mjs scripts/lintBaseline.test.mjs
npm test
npm run typecheck:tests
npm run build
```

Local tests use isolated HOME/XDG/cache/config directories and a fixed `C` locale.
Full repository and browser coverage remains part of current-head CI. No service
restart, deployment, live account access, or regression-environment update is
needed for this development-tool migration.
