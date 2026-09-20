# Model Hub: Reasoning-Effort Tier Provenance

Status: approved by owner 2026-09-03. Amends the tier-declaration behavior in
`docs/plans/model-hub.md` (§ "discovered models carry no capability
metadata"); that spec's "user owns the declaration" rule is superseded by the
provenance ladder below for reasoning-effort tiers only. Everything else
(route ordering, placement, guard semantics) is untouched.

## Owner decisions (recorded verbatim, 2026-09-03)

1. If the upstream carries tier information, the upstream values win — highest
   priority; user input may be wrong. Only when the upstream carries nothing
   may the user declare tiers.
2. The Avibe builtin catalog counts as "carried" knowledge for official model
   ids, ranking below live upstream metadata and above user input.
3. Auto-provided tiers are read-only for users: no delete, no add. The escape
   hatch for wrong declarations (either side) is runtime evidence, not user
   opinion: a tier the upstream provably rejects gets auto-quarantined (v2).

## Provenance ladder

For each source model, `reasoning_efforts` is populated by the FIRST rung that
applies, and the rung is recorded:

| Rung | `reasoning_efforts_source` | When | Refresh behavior |
| --- | --- | --- | --- |
| 1 | `upstream` | Discovery response carries a recognizable reasoning capability signal for this model | Re-applied on every refresh; authoritative |
| 2 | `catalog` | Model id matches an entry in `vibe/data/backend_models.json` that declares `reasoning_efforts` | Re-applied on every refresh |
| 3 | `user` | Neither rung above applies and the user typed tiers | Preserved across refreshes (existing behavior) |
| — | `null` | No rung applies and the list is empty | — |

Editing rules:

- `PATCH .../models/{id}` tier edits are REFUSED with error code
  `source_model_tiers_managed` (HTTP 409) when
  `reasoning_efforts_source ∈ {upstream, catalog}`. The refusal detail names
  the provenance. i18n keys for the refusal MUST land in all four bundles
  (`vibe/i18n/{en,zh}.json` AND `ui/src/i18n/{en,zh}.json`) — the missing-key
  defect class (B1/D-3) must not grow.
- Tier edits remain allowed when the source is `user` or `null`.
- When a refresh's rung-1/2 application REPLACES a non-empty user-typed list,
  the change is recorded as a resolution event (info severity, no credential
  material) so the override is visible, never silent.

## Upstream metadata capture (rung 1), v1 scope

Reality check: official OpenAI/Anthropic `/v1/models` carry no tier data.
Relay-style endpoints do carry capability signals. v1 recognizes exactly one
shape and treats everything else as "not carried":

- OpenRouter-shape `supported_parameters` arrays: if the model entry contains
  `"reasoning"` (or `"reasoning_effort"`), the model is reasoning-capable.
  Upstream signals are capability booleans, not level enums, so the applied
  tier list is the UNIFIED VOCABULARY for the source's protocol family (below).
- Any other/unknown metadata shape → rung 1 does not apply. No guessing from
  model-id patterns (family inference was explicitly deferred; wrong claims
  turn into upstream 400s).

The capture point is the discovery pipeline (`probe_models` /
`discover_models`), which today returns bare id tuples and discards all other
fields. The pipeline's return type grows a per-model metadata record; the
`SourceObservation` contract and `source.schema.json` gain the new fields.
This is a Model Hub contract change: follow the CONTRACT_VERSION bump protocol
(all mirrored constants + `scripts/check_model_hub_authorities.py` +
`docs/plans/model-hub-contracts/mirror-registry.json`), and keep
`_runtime_payload`'s hard-coded literal in sync (known drift trap).

## Unified vocabulary

One authority for effort values, exported from the backend catalog module and
mirrored (with a contract test) to the UI:

- `minimal, low, medium, high, xhigh, max, ultra` — full ordered vocabulary
  (ruling 2026-09-03: the vocabulary is the ordered SUPERSET of every value a
  catalog row legitimately declares; it exists for ordering/display and rung-1
  defaults, and must never truncate per-model truth. `ultra` was added because
  catalog rows for gpt-5.6-sol/terra declare it.)
- Rung 2 applies the catalog row's EXACT per-model list, verbatim — never a
  family default and never filtered through the vocabulary. If a future
  catalog row introduces a value outside the vocabulary, the vocabulary
  expands (with its contract test), not the row.
- Protocol-family defaults are used ONLY by rung 1 (capability boolean, no
  enum from upstream):
  `openai_responses` / `openai_chat` → `minimal, low, medium, high, xhigh`;
  `anthropic` → `low, medium, high, xhigh, max`. Family defaults deliberately
  exclude `ultra`: an unknown relay model must not be over-claimed (a wrong
  claim becomes an upstream 400).
- `ui/src/components/settings/models/tierSuggestions.ts` and
  `ui/src/lib/effortOptions.ts` stop disagreeing: both derive from (or are
  contract-tested against) the single vocabulary. Ghost suggestions for
  user-editable models use the protocol-family default list.

## Persisted-shape and migration rules

- New optional field `reasoning_efforts_source` on persisted models; absent in
  files written by older releases → loader treats existing non-empty lists as
  `user`, empty lists as `null`. Startup never fails on old shapes.
- First refresh after upgrade applies the ladder (catalog backfill happens
  then, with the override event when it replaces user values). No offline
  one-shot migration of tier values.
- OAuth-materialization and migration paths stop hardcoding
  `reasoning_efforts: []`: they run the same ladder (rung 2 catalog backfill
  applies immediately for official ids).

## Silent-strip observability

`_request_for_exact_reasoning_effort` keeps its exact-match forwarding rule,
but a strip is no longer invisible: record the stripped value and the hop's
declared tiers in turn provenance and a logger line. No user-facing chat copy,
no new event kind (noise control).

## v2 (documented now, NOT in v1): evidence quarantine

When a forwarded effort is rejected by the upstream with an
unsupported-parameter-shaped 400, quarantine that (source, model, tier):
stop forwarding it, record a resolution event, grey it in the UI with
"upstream rejected in practice". Quarantine outranks every declaration rung.
Design and contract for the quarantine store are follow-up work; v1 must not
paint itself into a corner (the provenance field naming above leaves room for
a `quarantined` marker per tier).

## Test obligations

- pytest e2e (`tests/e2e/`): B10 grows locked-provenance cases (catalog-backed
  model refuses tier edit; user-declared model still edits; refresh re-applies
  catalog over user with event). D10 asserts the strip is recorded in
  provenance. Scenario catalog rows updated accordingly.
- Playwright (`ui/e2e/`): tier editor shows provenance and locks for
  `upstream`/`catalog` models (no add input, no delete affordance, badge with
  provenance); ghost suggestions match the unified vocabulary; error copy for
  `source_model_tiers_managed` renders as human text in zh and en.
- Unit/contract: schema round-trip for the new field; old-shape load fixture;
  vocabulary contract test between backend export and both UI tables.
- `docs/plans/model-hub-e2e-test-plan.md`: B10/D10 rows and the §5 decision
  ledger gain this decision (D-5: tier provenance ladder, approved
  2026-09-03).
