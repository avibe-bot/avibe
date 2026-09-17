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

The prefix permission is structurally one-way: `containersFor` is consulted only by PARITY and RESIDUE, never by EXISTENCE, which reads the call. A fixture asserts a plain `t()` on a pinned prefix still fails, and an app-level test asserts both directions on the real bundles — every pinned name is still a node with copy under it, and no pinned name is named by a copy-demanding call site.

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

## Validation

- `vitest run src/i18n/keyCoverage.test.ts` — 19 passed (15 pre-existing, 4 added), 3.6s; the pinned `ts.Program` cost is unchanged.
- `vitest run` (whole UI suite) — 301 files, 4113 tests passed.
- `tsc --noEmit` on the guard directly under the app's strict options, including `noUnusedLocals` — clean. No new tsconfig was added: a second typecheck project for one file is a concept this change does not need.
- `npm run lint` — baseline check passed, no drift. `npm run build` — succeeded.

## Residual limitations

Unchanged by this PR and stated rather than rounded away: 51 genuinely dynamic template sites remain invisible; 852 bundle leaves are reached by no name in `src/` (#1968); a consumer's own TypeScript assertion on a `t()` result is not read (#1967). The prefix pin is a hand-maintained list of 2, which is the cost of a prefix being exactly a name no call site names — an unclassified new prefix fails loudly at the residue pin rather than passing quietly.
