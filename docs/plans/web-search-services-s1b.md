# S1b: settings and prepared Tools read (historical)

This records S1b's gated delivery. S2b-2 now activates the read and execution together;
see `web-search-services.md` §9 for current release ordering.

Source of truth: `web-search-services.md` §3.2, §3.5, §8–9; S1a is merged as
`68a59d11e9dea070f5fe4d231e90d0acbc2aca64`.

## Change contract

- Extend only V2 web-search settings and the Models → Tools read/PUT contracts.
  Preserve model routing, native tool declarations, turn freezing and execution.
- Optional `prefer_native`, Source-keyed partial `services`, and server-written
  `executor_order_kinds` survive a restart. Absence keeps defaults, including
  opt-in for a model vendor without `auto_enroll`. Removed Sources are ignored.
- Loading clamps obsolete catalog bounds in memory without rewriting disk. The
  next authorized write persists the normalized values. Defaults remain absent
  rather than being frozen as explicit user choices.
- PUT merges only supplied Sources/fields under the existing mutation lock;
  null removes an override. Reject invalid current-catalog values atomically.
  Every web-search order save snapshots the resulting settings' Source roles;
  missing Sources have no map entry, missing roles read as model. A damaged
  derived role map warns and loads as absent without discarding consent settings;
  S2 owns interpreting roles at read time, with no dormant test-only helper here.
  Loaded roles for removed Sources are ignored before validation and dropped at
  the next authorized write, including normal Source deletion. Extant retargeted
  Sources retain their saved role. The Tools read skips removed `service_order`
  ids without mutating the saved order; `executor_order` remains as saved.
- Define service candidate/vendor shapes and add model candidates' `kind`.
  One explicit internal activation switch suppresses both service candidates
  and service vendors until S2. No new UI, user toggle, chain or placement rule.
- S1a's v17 was unshipped at branch creation (latest `gh-v3.2.2rc11` carried
  v16). During implementation, `gh-v3.2.2rc12` was tagged with S1a and v17.
  S1b therefore opens v18, advancing all terminal mirrors and retaining v17
  among the readable persisted versions. Recheck the release evidence at close-out.

## Validation

Scenarios: **MH-HOSTED-002** (compatibility) and **MH-HOSTED-004** (Tools read/patch).
Existing hosted-tools tests cover the old settings only. Extend their owner
coverage for defaults/partial values and mixed-order restart persistence,
catalog downgrades without load-time writes, field/source patch isolation and
concurrent edits, atomic validation, order-role snapshots and stale ids. Validate
both the dormant public read and the defined future service variants against
the closed schema. Exercise the pinned rc12 loader against new fields and its
lossy downgrade write. The damaged-map regression fails before the fix and checks
real V2 load, unrelated write, re-enable and restart without losing an opt-out.
Removed-id regressions fail before the fix and cover the Tools projection and
live role-map loading followed by normal Source deletion, preserving survivor
order, retargeted roles and opt-outs.
Run focused tests through `~/.local/bin/hd1-test`, changed-file Ruff, UI test
typechecking (`npm run typecheck:tests`) and UI build.
No paid search, live runtime restart or regression update is part of this lane.

## Delivery

Fresh independent branch from master. Deliver to exact-head Codex/CI gates,
zero unresolved threads and CLEAN; report to the orchestrator, do not merge.
A SHA-bearing Codex no-findings receipt is a pass without a reaction; inspect
receipts directly instead of relying only on a coalesced Watch success event.

## Intentional non-changes

The dormant projection helpers define payloads, not S2's unified candidate chain,
placement or cooldown owner. S2 connects those while enabling the one gate. The
existing model-only Tools price renderer narrows out the new dormant variant;
there is no service renderer or new copy. Free-tier metadata remains null because
the bundled catalog makes no verified free-tier promise. Vendor facts are
structured booleans, so S4 can localize its own copy without backend prose.
Retargeted/retired but extant Sources retain their overrides (especially opt-out)
for when capability returns; the Tools read hides them while incapable, and only
removed Sources are forgotten.

Downgrade through rc12 preserves the whole-tool switch and both orders, but its
next config write drops new fields, including `services.<id>.enabled: false`.
Re-upgrading uses service defaults again: DeepSeek's opt-out must be reapplied
before service execution is enabled. The rc12 fixture records this limitation,
not a cross-version consent guarantee; S2/S6 must carry it into their guidance.
