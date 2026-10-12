# CI test-value audit: UI lane evidence

Lane of `docs/plans/ci-test-value-audit.md`. Scope: `ui/` test code, tooling
and config only; no application behavior, workflow, or package script name
changes. Base: `e089ae442` (contracts) on `b85e3c567`.

## Baseline attribution

From PR run 38258683681 (`ui-checks`, 16m45s, one job before the orchestrator's
base/browser split):

| Step | CI time |
| --- | --- |
| `npm ci` + `validate:theme` | ~16s |
| `npm run lint` (whole-tree ESLint + integrity) | 61s |
| `npm run typecheck:tests` (four `tsc` projects) | 72s |
| `npm test` (378 files, 6696 tests, 3 workers) | 342s wall; 599s summed test time |
| Playwright install + 8 serial suites | 8m14s |

The largest Vitest file was `scripts/lintBaseline.test.mjs` at 116.5s (next:
`SettingsModelsPage.render` at 40.2s). It spawned a second complete
`lint-baseline.mjs --update` ESLint pass immediately after the job's own
`npm run lint`. Four other `scripts/*.test.mjs` cases walked and parsed the
whole tree (`scenarioCatalog` 21.0s, `nonRenderingText` 11.1s, `glowScale`
9.8s, `modelCatalogDiscovery` 6.8s file times).

## Changes

### Commit `9eacb1c7d` — repeated whole-tree scans and source-text guards

Deleted test cases (exact names), reason, and remaining coverage:

| File > test | Reason | Remaining coverage |
| --- | --- | --- |
| `lintBaseline.test.mjs` > `an incomplete measurement blocks --update too, not just the verdict` > `refuses to write a baseline while an integrity check is failing` | Full second whole-tree ESLint run (116s CI file time). Integrity checks never read the baseline and run unconditionally first in every non-update `npm run lint`, so a baseline written during an integrity failure cannot pass CI. | Required CI `npm run lint` executes the real integrity path every run; `integrityLines` unit cases. **Known gap:** the developer-only `--update` "no write while integrity fails" ordering in `main()` no longer has automated full-tree coverage. The pure helper tests do not prove command ordering. Risk is low because the real lint still rejects the invalid tree. |
| `lintBaseline.test.mjs` > `compareToBaseline accepts the recorded debt unchanged` > `accepts a baselined violation replaced one-for-one within the same pair` | Same assertion as `reports nothing when the counts match exactly` (ledger vs identical ledger). | `reports nothing when the counts match exactly`; residual documented in `lint-baseline.mjs` header and bounded by `catches the same swap the moment it changes the count, the rule or the file`. |
| `lintBaseline.test.mjs` > `compareToBaseline cannot pass vacuously` > `fails when the lint run produced nothing at all` | Identical inputs and assertion class to `flags a rule that is now clean`. | `compareToBaseline fails on a baseline that overstates the debt` > `flags a rule that is now clean`. The empty/empty case moved into the first describe unchanged. |
| `lintBaseline.test.mjs` > `the gate script stays reviewable text` > `contains no raw NUL byte` | Byte scan of a dev script's source; not a user-facing contract. A binary diff is visible in review anyway. | None needed. |
| `lintBaseline.test.mjs` > `the gate script stays reviewable text` > `still joins the pair on a character neither half can contain` | Expected value is the implementation's separator spelling. | `pairKey` > `cannot confuse two different pairs that share a prefix` (the behavior). |
| `lintPolicy.test.mjs` > `the real UI tree is what the gate measures` > `covers the root config file the previous scope contract could not see`, `covers nested sources without anyone listing them`, `resolves exactly the pinned policy for every one of them` | Re-walk the real tree and resolve config for all ~1062 files, which `npm run lint`'s `measureIntegrity` already does on every CI run (`UNLINTED`/`UNWALKED`/`POLICY`/`DOMAIN`). | Required `npm run lint`; fixture tests `intendedFiles walks the whole repo-owned TypeScript tree` (root, nested, symlink, exclusions) and `a scope narrowing takes files out of the policy` (`basePath` dropping `vite.config.ts`). |
| `lintPolicy.test.mjs` > same describe > `gives every whole-tree test a clock that measures a hang, not a busy runner` | AST meta-test over test files pinning the `UI_ROOT` variable name and per-test timeouts; source structure, no product behavior. Its subjects are deleted. | None needed. |
| `glowScale.test.mjs` > `can spell every blur a design annotation names` and the 7 `reads %s` probes of its `ANNOTATED_BLUR` helper | Asserts code comments ("glow, blur 16") against the token scale; comments render nothing, and the probes test a test-only helper. Whole-tree parse (9.8s CI file time). | Per-role/per-theme rung and `OFF_RULE` assertions in the same file pin the actual token values; `validate:theme` forces every glow through a token. |
| `nonRenderingText.test.mjs` > `withoutNonRenderingText` > `preserves length and line breaks across every scanned file` | Whole-tree TypeScript parse (11.1s CI file time). The defect class it names is astral characters (UTF-16 vs code points). | Replaced by `preserves length and line breaks in %s after an astral character` (`.ts`, `.tsx`, `.css` fixtures with exact expected output). Mutation check: changing the blanker's `source.split('')` to `[...source]` fails the `.ts` and `.tsx` rows. |
| `nonRenderingText.test.mjs` > `withoutNonRenderingText` > `is applied to every extension the scan reads` | Regex over `validate-theme.mjs` source text for its last `extensions:` literal. | The dispatcher has two branches (`.css` vs TypeScript), both exercised by the `NON_RENDERING`/`RENDERING` fixtures for `probe.ts`, `probe.tsx` and `probe.css`. |
| `modelCatalogDiscovery.test.mjs` > `isolated catalog discovery retains all scenario-labelled browser cases` | Pins an exact case count (36), so any added case breaks it; a dropped spec file is already caught. One fewer `playwright --list` spawn. | `isolated discovery includes every fixture spec on both viewports`; `live E2E discovery never collects the model catalog fixture`; the suite itself runs in daily/manual `model-hub-ui-regression`. |

`scripts/wholeTreeScan.mjs` (the shared 60s timeout constant) had no remaining
users and is removed. Test count in `scripts/`: 693 → 676.

### Commit `a6a6e3e1d` — one `tsc` pass for equivalent test projects

`src/components/settings/models/tsconfig.tests.json`,
`tsconfig.i18n-tests.json` and
`src/components/onboarding/providers/tsconfig.tests.json` extended the same
`tsconfig.app.json` with the i18next augmentation, so shared application
sources were type-checked three times. They become one `ui/tsconfig.tests.json`.

- `tsc --listFilesOnly`: the sorted union of the three old projects (1061
  files) is identical to the new project's list.
- `tsc --showConfig`: effective compiler options are identical except
  `composite: false` (the default) and the unused `tsBuildInfoFile` (neither
  `incremental` nor `composite` is set, so nothing is written).
- `src/i18n/tsconfig.guard.json` stays separate on purpose: it adds node types
  and deliberately omits the application augmentation so the key-coverage
  guard's synthetic resources remain untyped.
- `typecheck:tests` keeps its name.

## Measurements (local, 10-core macOS, same worktree)

| Command | Before | After |
| --- | --- | --- |
| `npx vitest run scripts/lintBaseline.test.mjs` | 30.3s | 0.7s |
| `npx vitest run scripts/` (17 files) | 32.2s wall, 91s user CPU | 4.9s wall, 30s user CPU |
| `npm run typecheck:tests` | 32.4s (12.5 + 8.8 + 0.9 + 10.3) | 13.1s |
| Merged project alone (`tsc -p tsconfig.tests.json`) | — | 12.3s |

CI file times of removed work: 116.5s (`lintBaseline`) plus the whole-tree
cases inside `nonRenderingText`, `glowScale` and `lintPolicy` (~5s + ~3.4s +
~1s locally) and one Playwright list spawn. With 3 Vitest workers that is an
expected ~40–60s less `npm test` wall time. The `lintBaseline` child ESLint
process also stops competing for runner CPU. Expected `typecheck:tests`
saving is ~40s of 72s. Confirm both on the first real CI run.

Validation run on `a6a6e3e1d`: `npx vitest run scripts/` (676 passed),
`npm run typecheck:tests`, `npm run lint` (passes; 1062 sources measured),
`npm run build` (passes), ESLint on the changed test files.

## Not changed, with evidence

- **E2E.** Each suite pays its own `tsc` + Vite cold start (~5–13s) and runs
  `workers: 1`. Onboarding `lineup.spec.ts` runs a 7-viewport × 2-state
  matrix. `onboarding.css` has ~5 width/height bands, so `1024x768` shares
  `1200x800`'s band. Trimming it would save ~7s, and the orchestrator already
  moved E2E to its own lane, so the specs are untouched.
- **`scenarioCatalog.test.mjs`** (21s CI) runs the real scenario-catalog
  citation gate through `vitest list`. It is the repository's only enforcement
  of that gate, so it is kept.

## Follow-up candidates (audit only, not implemented)

Two read-only subagent audits produced candidates. They are unverified by
mutation, and the orchestrator asked to land the first CI run before broadening.

- **Source-text tests in `src/components/settings/models/`.** ~34 cases match
  the AGENTS.md §7 source-text exclusion and name behavioral coverage. Examples:
  - `repair.test.ts` spelling bans
  - `modelHubStylePolicy.test.ts` regexes over class names or formulas
  - `regionRead.test.ts`, `mutationSettlement.test.ts`,
    `mutationConvergence.test.ts`, `hopIdentity.test.ts`,
    `sourceStateClassification.test.ts` and `pickerExits.test.ts` call-site
    regexes
  - two cases in `SettingsModelsPage.render.test.tsx` (:587, :2422)
  - two cases in `SourceDetailPanel.test.tsx` (:958, :967)
  - `workbenchEventOwnership.test.ts` :92

  About 37 more are formally §7 violations but are the only PR-gate guard of a
  timing or layout property. They need an owner decision rather than deletion.
- **Production code reached only by tests.** `repair.ts` probe family,
  `modelsApi.probeAgent` and `supply.ts` `healthyButUnrunnable` (dead code per
  §7; outside this lane's no-application-change scope).
- **Real-time waits that can move to fake timers without changing
  assertions.** PetPage registration retry (~6.5s local), the
  SettingsModelsPage OAuth `POLL_MS` (~4.2s), the BackendModelEditorDialog
  250ms lookup debounce (~3.5s), and `waitFor` 50ms polling in the
  ChatPage.hydration matrix (2.8s → 1.1s).
- **Redundant matrix rows that reach no distinct branch.** ChatPage.hydration
  `:460` (35 → 18 rows), `AddApiKeyDialog` `:198` (11 `openai_chat` vendor
  rows), and `SourceDetailPanel` `:1043`/`:631`.
- **Possible flake to triage separately:** `SettingsModelsPage.render.test.tsx`
  `installs a removed backend before the committed route closes and applies
  PF-1` (3/11 local failures at the focus `waitFor`).
