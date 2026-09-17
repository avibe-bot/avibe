# i18n leaf and prefix consumption contract

## Intent and boundary

Fix #1969: every candidate that represents displayed copy must have the shape its consumer can render. A name with a valid subtree is not automatically a valid leaf. Preserve explicit key prefixes, array consumers, plural families and supported fallback behavior. This is guard correctness, with no visual/UI changes.

## Evidence

Audited `master@e8a7cb4a94e93b5413a977d70816b01560e3d156`: baseline keyCoverage has 15 passing tests. Independently changing `harness.createDialog.kindTask` (defaultValue-only) and `telegramConfig.proxyUrl` (carrier-only) to nonempty objects in BOTH bundles leaves all 15 tests passing (M21 and M22). The existing branch was clean and contained no implementation; it has now fast-forwarded to the audited master.

## Acceptance invariants

- Candidate keys are leaf copy by default. A small explicit set of intentional prefixes may satisfy the subtree contract; discover the actual set from BOTH literal and finite-template candidates, not historical counts.
- A readable call carrying `defaultValue` may omit the key entirely, but a present resolved value must satisfy the same consumption-shape requirement as the call without that fallback. Absence, presence, renderability and shape must not be conflated.
- Explicit prefix permission must not weaken a direct plain-call requirement on that same name. Carrier-only names must reject object or array values unless an existing explicit consumer contract justifies the shape.
- Existing nonempty list consumers, all applicable plural categories, whitespace/blank rejection, locale parity and non-key residue protections remain meaningful.
- M21/M22 must fail through the real application guard pipeline after mutation; nonmutated bundles, intentional prefixes and absent-key fallback cases pass. Use the existing synthetic invariant fixtures and negative controls; do not just test a helper disconnected from the consuming assertions.
- Guard TypeScript is checked directly or via a minimal applicable test project; focused i18n tests, required UI build and lint pass. No new test framework.

## Scope and deliberate residuals

Allowed: `ui/src/i18n/keyCoverage.test.ts` and this plan. A small colocated helper/test or narrowly scoped typecheck config may be justified to the orchestrator before editing. No `TranslationKey` rollout (#1967), orphan-leaf pin (#1968), copying text into bundles, component rewrites, package/lockfile updates or new framework. The sibling IPv6 lane owns Python/auth setup files; do not touch them.

## Design decision

Prefer the proposed leaf-by-default plus small explicit prefix contract, after measuring current candidate shapes. Reuse existing `consumable`/resolution ownership and simplify misleading comments when semantics change. Keep the exception set about intentional prefixes, not an enumeration of carrier prop names. Stop and report the whole class if this requires materially expanding the scanner model.

## Implementation

The root cause was one predicate answering two questions. `isRenderable` asked "is there copy UNDER this name", which is the prefix question, and PARITY, RESIDUE and the opaque-options branch of EXISTENCE all fell back to it. A leaf someone replaced with an object answers that question yes.

The container a value must arrive in is now a property of the NAME, with three answers and one shape table (`ARRIVES` / `arrivesIn`) shared by all three properties, so a shape closed in one is closed in all:

- `copy` — the default. A plain `t()`, a carrier prop, an `i18nKey`.
- `list` — READ off the app: a call site passing `returnObjects`. No prop names or positions enumerated, so a second list consumer is covered the day it is written.
- `subtree` — the enumerated exception, `KEY_PREFIXES`, reached by nothing else.

`isRenderable` is renamed `hasCopyUnder` and is now reachable only through the `subtree` container. The opaque-options (`any`) demand asks for `copy` or `list` — the weakest honest demand, since whatever options that call hides, i18next returns a string it renders or a list it maps, never a node. `consumable` is re-expressed through the same table and keeps its behaviour.

The prefix permission is structurally one-way: `containersFor` is consulted only by PARITY and RESIDUE, never by EXISTENCE, which reads the call. A fixture asserts a plain `t()` on a pinned prefix still fails, and an app-level test asserts both directions on the real bundles — every pinned name is still a node with copy under it, and no pinned name is named by any call site at all.

### Correction: presence is not container fit

The first reviewed head asserted that one-way rule and did not hold it. Two escapes, found independently by the PM and by Codex on the same head, were one root cause: EXISTENCE partitioned its work with `residueOf`, so "this value does not fit the container I chose" was read as "the bundles never had this name".

- A plain `t()` on a key the app also reads with `returnObjects` was skipped, because the array looked unresolved under `AS_COPY` while the name side allowed it. A name-level classification was cancelling a per-call demand — the thing the one-way rule promises cannot happen.
- Every `demand === 'none'` reference was filtered out of EXISTENCE outright. That is sound only where the key is missing; where it is present i18next never reaches the fallback, so `t('harness.runStatus', { defaultValue })` rendered a node's diagnostic in silence.

The correction is one concept, not two patches: **EXISTENCE partitions on PRESENCE, which is container-free.** `containersFor` is now unreachable from EXISTENCE, so the one-way claim holds structurally rather than by inspection, and a key present in any form reaches the per-call check for every call that names it. Codex proposed passing `appContainers` into that `residueOf` call instead; that repairs the symptom while leaving the name-level allowance inside EXISTENCE, so it was not taken.

Presence is asked of the resolver, per selected form:

- `instance.exists()` answers it, and nothing else can. A bundle may store the key AS its value — `card.echo: 'card.echo'` is present and invalid, `card.absent` is absent and legitimately covered by a fallback — and `t()` hands back the same string for both. The earlier ruling against `exists()` was against it as a VALIDITY predicate, which it still is not; `rendersAsCopy` and `consumable` keep that job.
- Per form, not per call: with only `card_one` present, `{ count: 1 }` exists and `{ count: 2 }` does not. A defaulted call reaching the second renders its own fallback and is owed nothing; reaching the first it is owed the same shape as a call carrying no default. So `defaultValue` buys exactly one thing, absence, and buys it one category at a time.

## Measured prefix census

Measured on `e8a7cb4a9` through the guard's own candidate pool (dotted literals plus finite-template expansions), not from historical counts:

| Measure | Value |
| --- | --- |
| Candidate names | 3454 (3198 literal, 256 template-only) |
| Resolving to an object node | 2 — `harness.runStatus`, `harness.statusFilter` (both literal, neither template-only) |
| Resolving to an array | 2 — `memory.settings.disclosure`, `memory.settings.cloudDisclosure`, both already `returnObjects`-modelled |
| Call-site demands | 3124 plain-exact, 19 opaque (`any`), 7 `defaultValue` (`none`) |

The issue's census named a third prefix, `workbench.modules.agents`. It is no longer one: `CapabilityTabs` names `workbench.modules.agents.title`, a leaf, and the bare prefix survives only in a `WorkbenchModulePlaceholder` doc comment, which the collector does not read. No template-only candidate is a prefix, closing that "unmeasured" item from the issue. None of the 7 `defaultValue` keys and none of the 19 opaque keys resolves to an object today, so tightening those paths changes no passing result on this head.

## Mutation evidence

Each probe mutates the real bundles, runs the real suite, and restores the bundles between runs. Unmutated: 19/19 pass.

| Probe | Mutation | Result |
| --- | --- | --- |
| M21 | `harness.createDialog.kindTask` → nonempty object in both bundles | FAILS — residue pin, key named in the diff (was: 15/15 pass) |
| M22 | `telegramConfig.proxyUrl` → nonempty object in both bundles | FAILS — residue pin, key named in the diff (was: 15/15 pass) |
| C1 | `telegramConfig.proxyUrl` → object in `zh` only | FAILS — parity, not residue |
| C2 | `harness.createDialog.kindTask` deleted from both bundles | FAILS — residue pin only; EXISTENCE stays silent, so the `defaultValue` call still demands no copy |
| C3 | `harness.runStatus` subtree deleted from `zh` | FAILS — prefix pin and parity; the exemption does not excuse a lost subtree |

C2 is the invariant that absence and a broken present value stay different facts: an omitted defaulted key is still classified by hand in the residue pin, exactly as before this change, while a present object value is now rejected.

After the presence correction all five behave as above, with M21 additionally failing EXISTENCE — its call carries a `defaultValue` and its key is present, so the call is now asked for the shape it renders. The escapes of the review round were reproduced the same way, by adding a real consuming component to `src/` and running the real guard:

| Probe | Reproduction | Before | After |
| --- | --- | --- | --- |
| E1 | a component rendering `t('memory.settings.disclosure')`, the key's array and its `returnObjects` consumer untouched | passes | FAILS EXISTENCE in both locales, naming the key |
| E2 | a component rendering `t('harness.runStatus', { defaultValue: 'Status' })` | passes | FAILS EXISTENCE in both locales AND the prefix pin |
| E3 | a counted `returnObjects` consumer of a key whose `_one` is a valid array and whose `_other` is empty | passes | FAILS EXISTENCE, naming `probe.rows (count, returnObjects)` |

E3 is Codex's witness for the same class: the name side resolves through `resolvesIn`'s `some` probe, and only the per-call check asks every reachable category. None of the three probe files or bundle edits is committed; the positive controls are the unmutated suite, which still passes with the real `returnObjects` consumer and the real defaulted call sites in place. The focused controls added for this round fail against the pre-correction `missingCopy`, which is how they are known to be load-bearing rather than merely green.

## Review ledger

One findings-bearing head so far, one root-cause class, so the circuit breaker does not trip.

| Head | Source | Findings | Class |
| --- | --- | --- | --- |
| `650db4d4e` | Codex review 5232356759, threads `…jPy2D` / `…jPy2H` | 2 | container classification suppressing a per-call requirement |
| `650db4d4e` | PM negative controls, executed against the real guard | 2 | the same class, independently found |

The PM's two controls and the bot's two threads describe the same defect from different ends, so they are one class on one head rather than two findings-bearing heads. The #1963 history that preceded this issue is recorded separately and is not counted against this rewrite.

## Validation

- `vitest run src/i18n/keyCoverage.test.ts` — 20 passed (15 pre-existing, 5 added), 3.8s; the pinned `ts.Program` cost is unchanged.
- `vitest run` (whole UI suite) — 301 files, 4114 tests passed.
- `tsc --noEmit` on the guard directly under the app's strict options, including `noUnusedLocals` — clean. No new tsconfig was added: a second typecheck project for one file is a concept this change does not need.
- `npm run lint` — baseline check passed, no drift. `npm run build` — succeeded.

## Residual limitations

Unchanged by this PR and stated rather than rounded away: genuinely dynamic template sites remain invisible; bundle leaves reached by no name in `src/` remain unpinned (#1968); a consumer's own TypeScript assertion on a `t()` result is not read (#1967). The prefix pin is a hand-maintained list of 2, which is the cost of a prefix being exactly a name no call site names — an unclassified new prefix fails loudly at the residue pin rather than passing quietly.

No count is claimed for the first two. The figures quoted while #1963 was open were measured on a different head, and the orphan one is not comparable to a name census taken over bundle paths: terminal arrays, plural suffix paths and source-use coverage are three different denominators, and mixing them produces a number that describes nothing. Remeasuring either population belongs to the issue that owns it.
