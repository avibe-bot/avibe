# UI Lint Debt Cleanup

Restore `cd ui && npm run lint` to a green, meaningful gate.

- Measured at: `78c26be8296f5ba006780b94f7724fd3815ffa1b`
- Rebased onto: `2d47aa469d0ef9c6c9a0f7382fc014706145f18a` — its only `ui/` change
  (`agentActivity.ts`) adds no lint debt, so every count below still holds.
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

### The count is not an inventory

`eslint-plugin-react-hooks@7` is compiler-backed: it bails out of a component
as soon as it hits something it cannot analyse. An unfixed `react-hooks/refs`
error is therefore a *frontier*, not a leaf — everything behind it in the same
component is invisible until it is fixed, and fixing it can make the error
count go **up**.

Measured, not assumed: deleting the 43 `react-hooks/exhaustive-deps` disable
comments — without changing a line of logic — moves the totals from 287 errors
/ 37 warnings to **296 errors / 80 warnings**, revealing 2 more `refs`, 3 more
`set-state-in-effect`, 2 `immutability` (both in `EditorApp.tsx`), and 2 more
`preserve-manual-memoization`.

Two consequences run through every decision below. Progress in this family
cannot be read off the total. And every inline suppression is a piece of the UI
the analysis cannot see, which is why the ratchet freezes the suppressions too
(§3) rather than only the reported errors.

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
shared cause to correct.

`refs` and `globals` are fixed here. 13 of the 16 are behaviour-preserving
restructures. Three are deliberate, already-commented patterns that the rule
cannot express — `App.tsx` bridges one frame on purpose, `useLatestRef.ts`
*is* the read-a-ref-in-render helper, `WindowManagerProvider.tsx` bootstraps at
mount. Those keep their behaviour and receive a narrow
`eslint-disable-next-line <rule> -- <reason>` pointing at the existing
rationale; the ratchet freezes those three suppressions like any other (§3), so
they are counted, not hidden.

`set-state-in-effect` is not fixed here — see its own section below.

Any site whose behaviour genuinely changes gets a dedicated regression test and
is called out in the PR body.

### G4 — trivia (3 errors, 4 stale directives)

`no-unused-expressions` ×2 (`ChannelList.tsx`), `no-useless-escape` ×1
(`mentions.ts`, an unnecessary `\[` inside a character class), and 4
`eslint-disable` comments that no longer suppress anything.

**Decision: fix as code.** Stale directives are deleted — a suppression that
suppresses nothing is misleading documentation.

### G5 — react-refresh module boundaries (42 errors, 23 files)

One rule, but its two messages ask for **opposite** fixes, so they are
different classes of work:

- *"Use a new file to share constants or functions between components"* — the
  module has a recognised component export **and** a non-component one. Move
  the non-component out.
- *"Move your component(s) to a separate file"* — the module has exports but no
  recognised component export, so its local components can never be
  hot-replaced. Move the component out. (A **class** component does not count
  as a component export; React Refresh does not support classes.)

**G5a — pure helpers stranded in component modules (26 errors, 12 files).**
Constants, lookup tables, and pure functions exported from a `.tsx` component
module, mostly so tests can import them: all 8 of `vendorMeta.tsx`, 4 of
`DockContext.tsx`, `badgeVariants`, `buttonVariants`,
`sessionAgentDisplayName`, `harnessTabFromParam`, `selectApiErrorFields`, and
so on.

One is a false positive rather than a stranded helper:
`export const DEFAULT_VENDOR = VENDOR_OPTIONS[0]` — an uppercase name with an
initializer the plugin cannot analyse reads as a component, which flags every
sibling export in a `.tsx` file that contains no JSX at all. Renaming the file
to `.ts` is the correct fix, not a suppression.

*Decision: fix.* Each moves to a sibling module that owns it, following the
convention already in the tree (`settings/models/` keeps `eligibility.ts`,
`format.ts`, `reorder.ts` beside its components). Two of the 12 files are
context modules contributing *helper* exports (`DockContext.tsx` ×4,
`ApiContext.tsx` ×2); their hook exports belong to G5c.

**G5b — a component stranded in a data module (2 errors, 2 files).** The mirror
image: `apps/registry.tsx` defines its Suspense fallback next to the registry
object, and `ui/error-boundary.tsx` pairs a class boundary with its fallback.

*Decision: fix.* The component moves out, which is also what the rule means
here — a fallback that can never hot-reload forces a full page reload for a
one-line copy change.

**G5c — a context hook next to its provider (14 errors, 11 files).** The
standard `XContext.tsx = context + Provider + useX` layout. Two fix directions
exist and they differ enormously in cost:

| Direction | Files whose imports change |
| --- | --- |
| move the hook out | up to 78 (`useApi`), 55 (`useToast`), 22 (`useWindowManager`) |
| move the Provider out | 1–4 (providers are mounted once) |

*Decision: fix by moving the Provider out*, for every context except
`ApiContext.tsx`. Renaming `XContext.tsx` to `XContext.ts` keeps every
consumer's extensionless import specifier valid, so only the mount points
change. The tree already demonstrates the pattern twice
(`showPageDrag.ts` + `ShowPageDragProvider.tsx`, `unsavedChangesContext.ts` +
`UnsavedChangesProvider.tsx`).

**Baselined exception — `ApiContext.tsx` (1 error, `useApi`).** Both directions
are disproportionate here, and the risk is measured rather than assumed:

- 3194 lines, of which `ApiProvider` is ~1200; the rest is the API surface type
  and ~120 exported payload types that most of `ui/` imports.
- 143 commits in the last 90 days — the second-busiest file in `ui/src` after
  `ChatPage.tsx`.
- **36 unmerged local branches currently modify it, 21 of them committed to
  within the last 30 days.** For comparison, the three contexts split in this
  PR are touched by 1, 2 and 2 unmerged branches.

Splitting it means relocating ~1200 lines in the file with by far the widest
live-branch overlap in the repository — a conflict for 36 branches, in exchange
for hot-reload on one file. Moving `useApi` instead would rewrite 78 import
sites. Neither belongs in a lint PR, and the brief forbids work that comes
closest to overwriting other branches.

The two pure helpers in the same file *are* extracted (G5a), leaving exactly
one baselined error with a written rationale and a named follow-up: extract
`ApiProvider` in a dedicated PR when the branch queue for that file is short.

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

### `react-hooks/set-state-in-effect` (17 errors after G3)

What survives G3 is the "reset derived state when the subject changes" and
prop-mirroring shapes, in components with no test coverage. Each rewrite moves
when a render happens, so each needs its own regression test first — and by the
frontier property above, fixing one reveals more.

**Decision: baseline.** Batching render-order rewrites behind a lint deadline is
how a subtle bug ships.

### `react-hooks/preserve-manual-memoization` (9 errors)

**Decision: baseline.** These are React Compiler *adoption* advice.
`babel-plugin-react-compiler` is not in this project's Vite plugin list
(`plugins: [react()]`), so no shipped behaviour depends on them. They become
actionable in the change that turns the compiler on.

### `react-hooks/exhaustive-deps` (37 warnings)

Warnings do not affect the exit code and are not part of restoring the gate.
They are not fixed here — but their **suppressions** are frozen by the ratchet
(§3) so the unanalysed area cannot grow.

## 3. Baseline and ratchet

`ui/eslint-baseline.json` is an explicit per-file, per-rule ledger of the
residue, and `npm run lint` is now `node scripts/lint-baseline.mjs`: the same
ESLint pass, measured against that ledger. `npm run lint:report` still runs
plain `eslint .`; `npm run lint:baseline` regenerates the ledger.

The ledger has two tallies, because a rule can be silenced two ways:

| Tally | What it records | Final count |
| --- | ---: | ---: |
| `violations` | errors ESLint reported | 240 across 48 files |
| `suppressions` | messages an inline `eslint-disable` comment hid | 48 across 34 files |

Freezing the suppressions is what makes the gate sound. Without it, adding one
comment is a free way past the ratchet — and each comment also removes its
component from the compiler-backed analysis, so the dark area measured in §1
would grow unobserved.

### Invariant

The build fails when any of these is true. The first three apply to each
tally independently; the last two are ledger-wide:

1. **Unclassified** — a `(file, rule)` pair with no ledger entry. The lookup is
   per pair, so a file already listed for one rule buys no tolerance for
   another.
2. **Expanded** — a pair's count exceeds its recorded number.
3. **Stale** — a pair's count is *below* its recorded number, or the pair is
   gone. An improvement has to be recorded in the same commit, otherwise the
   freed headroom silently becomes budget for new violations.
4. **Unexplained** — a rule appears in the ledger with no entry in its
   `rationale` map. A rule nobody had to explain is exactly how an unrelated
   error class slips in and stops being visible.
5. **Fatal** — ESLint reported a problem with no rule id: a parse failure, an
   unresolvable config, a plugin that threw. This one is unconditional, and it
   has to be: such a problem has no `(file, rule)` key to be recorded under,
   and a file that does not parse reports *no violations at all*, which a pure
   tally comparison reads as clean. Without this, breaking a file's syntax
   would be a way to make its debt disappear and still pass.

The ledger therefore never grows on its own — raising a number is an edit to a
reviewed file, not a side effect of a push — and nothing it tolerates is
anonymous: every tolerated violation is one greppable line naming its rule, its
file, and its exact count, under a rule-level rationale in the same file.

### What the invariant does not cover

The ratchet is keyed on `(file, rule)` counts, which is less than a violation's
identity. Two consequences follow, and both are stated here rather than left for
a reader to discover:

- **A one-for-one site replacement inside an already-recorded pair passes.**
  Delete one baselined `no-explicit-any` in `src/lib/platforms.ts`, add another
  anywhere in the same file, and the count is unchanged, so the gate exits 0.
  Measured, not theorised. This is the accepted residual of a count ratchet, and
  it is the same trade-off ESLint 9's own `suppressions-service` makes with the
  identical `Record<file, Record<rule, {count}>>` shape. It is bounded on three
  sides: the pair must already carry recorded debt, the count cannot grow, and
  once that debt is genuinely paid down the *stale* check forces the allowance
  down with it, permanently. Raising it again needs a reviewer.
- **The ledger records what was found, never what was looked at.** Widening
  `globalIgnores` would exempt arbitrary source with no signal in the ledger at
  all — the exempted pairs would simply read as *stale* and the next
  `npm run lint:baseline` would book the exemption as if the debt had been paid.
  That hole is closed at its owner instead, by pinning the config: see the scope
  probes in Evidence below.

Closing the first one properly means keying the ledger on a site fingerprint,
which pushes ledger churn into every unrelated PR that touches a line carrying
baselined debt — 52 of the 240 sit in `ApiContext.tsx` alone. That cost lands
outside this cleanup, so the count model stays and the claim is written to
match it.

### Non-goals of the baseline

- It is not an ignore list. Ignored paths and disabled rules would hide
  unclassified errors; per-pair counts cannot.
- It does not downgrade errors to warnings.
- It does not cover rules that are fully fixed — those pairs are absent, so any
  regression fails as *unclassified*.

### Evidence

`compareToBaseline`, `missingRationales`, `fatalProblems` and `reportLines` are
pure functions with red-first unit tests (`ui/scripts/lintBaseline.test.mjs`,
25 cases): exact match, new rule in a new file, new rule in an already-listed
file, expanded count, reduced count, vanished pair, deleted file, empty-run
vacuity, each rationale branch, each `fatalProblems` discrimination, and — the
load-bearing one — that `reportLines` still fails when a fatal error is the
*only* problem and every tally matches.

Two of those cases pin the residual above as an *intended* property rather than
an oversight: that a one-for-one swap inside a recorded pair passes, and that
the same swap is caught the moment it changes the count, the rule or the file.

The gate script is also pinned as reviewable text. Its `(file, rule)` key joins
on NUL, which cannot occur in either half; written as a literal byte it made
the whole script binary to git (`Bin 0 -> 8114 bytes`), so the gate everyone is
asked to trust stopped appearing in diffs. `pairKey` writes it as an escape,
with identical runtime meaning, and a test asserts the source contains no raw
NUL.

The wired command was then driven end to end against all four evasion routes,
each of which failed the gate and was reverted:

| Probe | Gate output |
| --- | --- |
| `any` in a brand-new file | `NEW … no-explicit-any 1 errors (not in the baseline)` |
| one extra `any` in a listed file | `EXPANDED … 3 errors, baseline allows 2` |
| the same `any` behind a disable comment | `NEW … 1 suppressed messages (not in the baseline)` |
| one recorded `any` fixed, ledger untouched | `STALE … 1 errors, baseline still records 2` |

The *config* decisions are pinned the same way, as executable probes against the
real `eslint.config.js` rather than as prose
(`ui/scripts/eslintConventions.test.mjs`): an unmarked unused binding still
errors while an `_`-marked one does not (G1), and `react-hooks/refs` still
errors on an ordinary ref read *and* write during render, so the one blanket
exemption in `useLatestRef.ts` cannot quietly become a config-wide relaxation.

Those probes assert **severity 2**, not merely that the rule fired. The gate
tallies severity-2 messages, so a rule demoted to a warning would still appear
in `messages`, still pass a presence check, and still change no count in the
ledger — while no longer gating anything. Measured: with `react-hooks/refs` and
`no-unused-vars` demoted to `warn`, the presence-only form of these probes
passed 10/10.

### The contract pins whole subjects, not projections

A behavioural probe only defends the rule someone thought to write it for, so the
same file closes the class at its chokepoint. The first two attempts at that
chokepoint pinned the policy with *filtered views* of it, and every view has a
blind complement — which review then found one at a time:

| Projection | What it could not see |
| --- | --- |
| the rule ids at severity 1 and at severity 0 | a severity-2 rule that leaves the config outright |
| global `ignores` only | an `ignores` on a rule-bearing entry, which exempts a subtree while its `files` is unchanged |
| the union of every entry's `files` | the same scoped ignore, from the other side |
| "some rules resolve" as *linted* | a path resolving 1 rule out of 106 |

A fifth predicate would only move the blind spot. So the contract is now two
complete subjects, compared for equality:

- **`EXPECTED_SEVERITY_MAP`** — every rule id the config resolves for a
  TypeScript source, with its level. 106 entries: 81 error, 3 warn, 22 off. Both
  the `.ts` and the `.tsx` probe must resolve exactly this map.
- **`EXPECTED_SCOPE_CONTRACT`** — every entry of the flat config in cascade
  order, each entry's `files` and `ignores` kept together, nested pattern shape
  preserved rather than flattened. Listing *every* entry, not only the scoped
  ones, is what makes a rule-bearing entry with no scope at all show up — as a
  new `files: null` row.

Path classification derives from the first of those, never from a rule count:

| Path | Classified |
| --- | --- |
| an unwritten `src/**/*.ts` and `src/**/*.tsx` | *linted* — resolves exactly `EXPECTED_SEVERITY_MAP`, and an `any` in it errors at severity 2 |
| `dist/**` | *ignored* |
| `scripts/*.mjs`, `eslint.config.js` | *parsed only* — not ignored, resolves exactly the empty rule map |

Anything else is reported as a fourth state naming both counts, so a reduced rule
map cannot read as *linted*. The *parsed only* row is recorded, not aspirational:
`npm run lint` opens the gate script and the config itself, so a syntax or config
error in either fails the build through the *Fatal* branch, but no rule applies
to them. Broadening the lint scope to JS/MJS is a separate decision, and widening
`files` fails the scope contract — which makes it a visible one.

A dependency upgrade is *meant* to fail these two assertions. The diff is the
review prompt: the only place a change in enforcement policy surfaces before it
silently changes what `eslint-baseline.json` is allowed to record.

Red-first, each defect applied to the real `eslint.config.js` and reverted, with
the config confirmed byte-identical afterwards:

| Defect | Under the projections | Under the complete contract |
| --- | --- | --- |
| `ignores: ['src/context/**']` on the rule-bearing entry | 21/21 passed | 1 failure |
| an upstream preset drops `react-hooks/globals`, an error rule with no violations | 21/21 passed, and `npm run lint` printed *no drift* | 4 failures |
| a `.tsx` path re-covered by a single-rule config | 21/21 passed, classified *linted* on 1 of 106 rules | 2 failures |

The middle row is the whole argument in one line: an enforcement rule left the
gate, every test passed, and the ledger reported no drift.

## 4. Result

| Rule | Before | Fixed | Suppressed | Baselined |
| --- | ---: | ---: | ---: | ---: |
| `@typescript-eslint/no-explicit-any` | 280 | 67 | 0 | 213 |
| `react-refresh/only-export-components` | 42 | 41 | 0 | 1 |
| `react-hooks/refs` | 14 | 11 | 3 ² | 0 |
| `react-hooks/set-state-in-effect` | 14 | 0 | 0 | 17 ¹ |
| `@typescript-eslint/no-unused-vars` | 7 | 7 | 0 | 0 |
| `react-hooks/globals` | 2 | 2 | 0 | 0 |
| `@typescript-eslint/no-unused-expressions` | 2 | 2 | 0 | 0 |
| `no-useless-escape` | 1 | 1 | 0 | 0 |
| `react-hooks/preserve-manual-memoization` | 0 ¹ | 0 | 0 | 9 |
| **Total errors** | **362** | **131** | **3** | **240** |

¹ Both counts *rose* while the code got better — the frontier property (§1).
Fixing the 14 `refs` errors let the analyser reach code it had been bailing out
of, which exposed 3 more `set-state-in-effect` and all 9
`preserve-manual-memoization` findings. Neither is a regression; they were
always there, behind an error that stopped the analysis.

² Three deliberate read-a-ref-in-render sites (G3). They keep their behaviour
behind a rationaled `eslint-disable-next-line`, and the ratchet freezes them in
the suppression ledger, so they are counted rather than hidden. The suppression
tally goes 45 → 48 for exactly these three.

Warnings go 41 → 54, all `react-hooks/exhaustive-deps`, for the same reason.
`npm run lint` has never had `--max-warnings`, so they do not gate.

No product behaviour changes. Every module split in G5 is a verbatim move.

## 5. Acceptance

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

## 6. CI enforcement

`.github/workflows/lint.yml` never ran `npm run lint` — which is how the debt
reached 362 errors in the first place (§1). A gate nobody runs is not a gate,
so this cleanup would have rotted again on the same schedule.

The `build-linux-artifacts` job now runs it, after `validate:theme` and before
`npm test`:

```yaml
      - name: Build UI assets
        working-directory: ui
        run: |
          npm ci
          npm run validate:theme
          npm run lint
          npm test
          npm run build
```

One added line, in a file this lane does not otherwise own; the extension was
approved for exactly this hunk. It costs one ESLint pass in a job that has
already installed `node_modules`.
