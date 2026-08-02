# UI Lint Debt Cleanup

Restore `cd ui && npm run lint` to a green, meaningful gate.

- Base commit: `78c26be8296f5ba006780b94f7724fd3815ffa1b`
- Branch: `fix/ui-lint-debt`
- Scope: `ui/**` and this document only.

## 1. Verified baseline

Reproduced from a clean `npm ci` at the base commit. `npm run lint` is
`eslint .` with no `--max-warnings`, so **only errors decide the exit code**;
the 41 warnings are already tolerated by the declared command.

```
403 problems (362 errors, 41 warnings) across 90 files
```

| Rule | Errors | Warnings | Files |
| --- | ---: | ---: | ---: |
| `@typescript-eslint/no-explicit-any` | 280 | 0 | 49 |
| `react-refresh/only-export-components` | 42 | 0 | 23 |
| `react-hooks/exhaustive-deps` | 0 | 37 | 14 |
| `react-hooks/refs` | 14 | 0 | 10 |
| `react-hooks/set-state-in-effect` | 14 | 0 | 12 |
| `@typescript-eslint/no-unused-vars` | 7 | 0 | 5 |
| unused `eslint-disable` directives | 0 | 4 | 4 |
| `react-hooks/globals` | 2 | 0 | 1 |
| `@typescript-eslint/no-unused-expressions` | 2 | 0 | 1 |
| `no-useless-escape` | 1 | 0 | 1 |
| **Total** | **362** | **41** | **90** |

### Why the debt accumulated

`ui/eslint.config.js` is the stock Vite scaffold and arrived in the squashed
`3faf4712 v2 init` commit, together with `eslint-plugin-react-hooks@^7` — a
major version that added the `refs`, `set-state-in-effect`, and `globals`
rules. Nothing has run the linter since: `.github/workflows/lint.yml` executes
`validate:theme`, `test`, and `build` in `ui/`, but **never `npm run lint`**.
An ungated command drifts by default.

Wiring the gate into CI is outside this lane's scope (`.github/**`) and is
reported separately. Everything below assumes the gate will eventually be
enforced, which is why the end state must be a command that *stays* green
rather than one that merely exits zero once.

## 2. Root-cause grouping and decisions

### G1 — `no-unused-vars` disagrees with the codebase's `_` convention (7 errors)

Six of seven are deliberate `_`-prefixed placeholders (`_value`,
`_legacyDefaultBackend`, `_loading`, `_itemValue`, `_label`, `_drop`). The
convention exists in the source; the config never opted into it.

**Decision: fix at the config.** Set `argsIgnorePattern`,
`varsIgnorePattern`, `caughtErrorsIgnorePattern`, and
`destructuredArrayIgnorePattern` to `^_`. This aligns the rule with the
convention already in use — it does not weaken it: any unused binding that is
*not* explicitly marked still errors, which a fixture probe pins.

The seventh (`BackendOAuthPanel.tsx`, an unused `catch` binding) is a real
finding and is fixed as code (`catch { }`).

### G2 — `catch (err: any)` is the codebase's only error-message idiom (67 errors)

67 of the 280 `no-explicit-any` errors are one shape:

```ts
catch (err: any) { setError(err?.message || FALLBACK); }
```

`any` is used here purely to reach `.message` on an `unknown` catch binding.
The codebase already contains the correct idiom, written inline ~20 times
(`err instanceof Error ? err.message : String(err)`), but no shared helper
exists, so the wrong shape kept being copied.

**Decision: fix with a shared contract.** One helper module in `ui/src/lib`
that turns `unknown` into a message. Three call-site shapes exist and are
*not* interchangeable, so the helper exposes them explicitly rather than
collapsing them:

| Observed shape | Empty-string `message` behaviour |
| --- | --- |
| `err?.message \|\| fallback` | falls through to `fallback` |
| `err?.message ?? String(err)` | keeps the empty string |
| `err instanceof Error ? err.message : String(err)` | keeps the empty string |

Only the `any`-bearing sites are converted. The already-correct inline sites
are left alone: touching them would enlarge the diff without changing a single
lint result.

### G3 — react-hooks correctness (30 errors)

`react-hooks/refs` (14), `set-state-in-effect` (14), `globals` (2). These are
genuine React correctness rules from the v7 plugin bump, never previously run.

**Decision: fix as code, per site.** Each is judged individually; there is no
shared cause to correct. A minority are deliberate, already-commented patterns
— for example `usePendingVaultRequests.ts` resets stale rows *during render*
on purpose, so switching sessions never paints the previous session's cards
for a frame. Those keep their behaviour and receive a narrow
`eslint-disable-next-line <rule> -- <reason>` that points at the existing
rationale. Behaviour-preserving restructures are preferred everywhere else.

Any site whose behaviour genuinely changes gets a dedicated regression test and
is called out in the PR body.

### G4 — trivia (3 errors, 4 stale directives)

`no-unused-expressions` ×2 (`ChannelList.tsx`), `no-useless-escape` ×1
(`mentions.ts`, an unnecessary `\[` inside a character class), and 4
`eslint-disable` comments that no longer suppress anything.

**Decision: fix as code.** Stale directives are deleted — a suppression that
suppresses nothing is misleading documentation.

### G5 — react-refresh module boundaries (42 errors, 23 files)

One rule, two structurally different sub-classes.

**G5a — pure helpers stranded in component modules (28 errors, 15 files).**
Constants, lookup tables, and pure functions exported from a `.tsx` component
module, mostly so tests can import them: all 8 of `vendorMeta.tsx`, 4 of
`DockContext.tsx`, `badgeVariants`, `buttonVariants`,
`sessionAgentDisplayName`, `harnessTabFromParam`, `selectApiErrorFields`, and
so on. Two files (`apps/registry.tsx`, `ui/error-boundary.tsx`) are the mirror
image: a *local* component sitting in a module whose exports are not
components.

*Decision: fix.* Each moves to a sibling module that owns it. These are small,
additive, obviously-correct diffs, and they are the module-boundary correction
the rule is actually pointing at.

**G5b — a context hook next to its provider (14 errors, 11 files).** The
standard `XContext.tsx = context + Provider + useX` layout. Two fix directions
exist and they differ enormously in cost:

| Direction | Files whose imports change |
| --- | --- |
| move the hook out | up to 118 (`useApi`), 54 (`useToast`), 22 (`useWindowManager`) |
| move the Provider out | 1–4 (providers are mounted once) |

*Decision: fix by moving the Provider out*, for every context except
`ApiContext.tsx`. The context object, hook, and types stay where 100+ modules
already import them; only the component relocates.

**Baselined exception — `ApiContext.tsx` (1 error, `useApi`).** Both
directions are disproportionate here and the risk is measured, not assumed:

- 3194 lines; `ApiProvider` is roughly a third of the file.
- 143 commits in the last 90 days — the highest-churn file in `ui/`.
- Five branches currently hold unmerged edits to it, four of them from the
  last four days.

Moving that provider would collide with every one of those branches in a way
git cannot resolve line-locally, and moving `useApi` instead would touch 118
files. Neither belongs in a lint PR. The two pure helpers in the same file
*are* extracted (G5a), leaving exactly one baselined error with a written
rationale and a named follow-up: extract `ApiProvider` in a dedicated PR when
the branch queue for that file is short.

### G6 — residual `@typescript-eslint/no-explicit-any` (~213 errors)

After G2 the remainder is per-site typing work, not a shared cause. The shapes
are `param: any` (70), `Promise<any>` (46), `<any>` type arguments (19),
`field: any` (16), `Record<_, any>` (15), `as any` (14), `any[]` (12), other
(21). The `Promise<any>` cluster is the return type of ~50 API-client methods
whose responses come from the Python backend, which publishes no schema the UI
can generate from. Inventing those contracts from call-site usage would encode
guesses as types and risk runtime mismatch — that is a worse outcome than an
honest `any`.

**Decision: narrowest explicit baseline plus a ratchet.** See §3.

**Explicitly rejected:** a named alias such as `type ApiJson = any` to collapse
the 46 `Promise<any>` sites. It would silence the rule while leaving the values
just as untyped, and it would destroy the ratchet's ability to notice a *new*
untyped endpoint. Keeping them counted is the honest option.

### `react-hooks/exhaustive-deps` (37 warnings)

Warnings do not affect the exit code and are not part of restoring the gate.
They are not fixed here — but they are frozen by the ratchet (§3) so the count
cannot grow.

## 3. Baseline and ratchet

`ui/eslint-baseline.json` records **exact per-file, per-rule, per-severity
counts** for the accepted residue. `npm run lint` runs ESLint and then the
ratchet.

### Invariant

The build fails when any of these is true:

1. **Unclassified** — an error exists for a `(rule, file)` pair with no
   baseline entry. New rules, new files, and newly-affected files all fail.
2. **Expanded** — a pair's count exceeds its baseline number.
3. **Stale** — a pair's count is *below* its baseline number, or the pair has
   disappeared entirely. Fixing debt requires lowering the ledger in the same
   commit, so the baseline can never drift into overstating what is broken.

The baseline can therefore only ever shrink, and it cannot hide anything: every
tolerated violation is one grep-able line naming its rule, its file, and its
exact count.

### Non-goals of the baseline

- It is not an ignore list. Ignored paths and disabled rules would hide
  unclassified errors; per-pair counts cannot.
- It does not downgrade errors to warnings.
- It does not cover rules that are fully fixed — those pairs are absent, so any
  regression fails as *unclassified*.

The ratchet's comparison logic is a pure function with its own unit tests
(red-first): unclassified, expanded, stale, and exactly-matching inputs.

## 4. Acceptance

From a clean install in `ui/`:

```bash
npm ci
npm run lint            # eslint + ratchet, exit 0
npm test                # full Vitest suite
npm run validate:theme
npm run build           # validate:imports + tsc -b + vite build
```

Plus `git diff --check` and a full review of the diff against `origin/master`.

Exact before/after totals for every command are recorded in the PR body.

## 5. Out of scope, reported separately

`.github/workflows/lint.yml` never runs `npm run lint`. Until that changes,
this cleanup can silently rot again. The workflow change is outside this
lane's file ownership and is raised with the orchestrator rather than made
here.
