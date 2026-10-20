# S1b: settings and dormant Tools read

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
  missing Sources have no map entry, missing roles read as model.
- Define service candidate/vendor shapes and add model candidates' `kind`.
  One explicit internal activation switch suppresses both service candidates
  and service vendors until S2. No new UI, user toggle, chain or placement rule.
- S1a's v17 was unshipped at branch creation (latest `gh-v3.2.2rc11` carried
  v16). During implementation, `gh-v3.2.2rc12` was tagged with S1a and v17.
  S1b therefore opens v18, advancing all terminal mirrors and retaining v17
  among the readable persisted versions. Recheck the release evidence at close-out.

## Validation

Existing hosted-tools tests cover the old settings only. Extend their owner
coverage for defaults/partial values and mixed-order restart persistence,
catalog downgrades without load-time writes, field/source patch isolation and
concurrent edits, atomic validation, order-role snapshots and stale ids. Validate
both the dormant public read and the defined future service variants against
the closed schema. Exercise a pinned release-before loader against new fields.
Run focused tests through `~/.local/bin/hd1-test`, changed-file Ruff, and UI build.
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
for when capability returns; only removed Sources are forgotten.
