# Repairing model ids an earlier release stored with a credential address

## Background

The engine reaches one credential through the **model field of one outbound
request** and through no other field, so a call spells its target
`<source prefix>/<model>`. That prefix (`avibe-` + twelve random bytes) addresses
a credential. It is not part of what the model is called.

Releases before PR #2099 persisted what the engine's management API answered
with — names the engine had *already* addressed — so a stored inventory id could
carry the address of the credential that served it. `invoke` then addressed it a
second time (`client.py:368` composes unconditionally) and the engine answered
`model_not_found`; independently, `matching_v1_model_id` compares a menu id
against `source.models[].id` literally, so an addressed row matched no route
target and every hop through it was refused as `mapping_target_unavailable`.

PR #2099 closed the boundary the address enters through:

- **Discovery** (`vibe/model_hub_runtime/adapter.py`) removes the address of the
  credential it is discovering, compared literally against that credential's own
  prefix. Nothing newly written carries one.
- **Engine state records** (`vibe/model_hub_runtime/state.py`) heal against their
  own `SourceRecord.prefix`, re-keying `model_reasoning_efforts` with the ids.

What remains is **files already written**. This document is the design for
repairing those.

## Why the config layer is the wrong place

PR #2099 first attempted the repair at the config load seam
(`_migrate_model_hub_credential_addresses`), and review rejected it twice for
two reasons that are both structural, not fixable by a better heuristic:

1. **The config layer cannot prove ownership.** `ModelHubSourceConfig`
   (`config/v2_config.py:3020`) has no prefix field; `credential_metadata` lives
   only in `vibe/model_hub_runtime/state.py`. A repair there can only recognise
   an address by its *minted spelling*, which means renaming any upstream
   identity that happens to be spelled that way. Provenance narrows the blast
   radius but does not close it: a `discovered` row on a passthrough source is
   whatever upstream named it.
2. **An id is a join key, and config sees only part of the join.** The same id
   appears in the source inventory, route hops, **route keys**, the persisted
   backend menu rows (`ModelHubBackendModelConfig`), `removed_model_ids`,
   per-source reasoning maps, engine state records, and usage ledger keys.
   `agent_model_candidates` (`service.py:4289`) derives `provider`-origin picker
   candidates directly from source model ids, so an addressed id can structurally
   reach the persisted menu and a route key. A migration that renames the
   inventory row alone leaves every other position naming something gone.

So the repair needs both halves that the config layer lacks: the credential
(to prove the prefix) and the service (to move every position together).

## Functional relief that already exists

An affected user is not stuck waiting for this. Once #2099 lands, one **refresh
models** on the Source re-discovers through the fixed boundary and
`_apply_discovered_models` (`service.py:1904`) replaces the inventory rows with
bare-named ones. What refresh does not do is move a route hop or a route key
that already points at an addressed spelling — those are the positions this
repair owns.

## Design

### Where it runs

A service-level repair, invoked once per Source from the runtime, not a
config-load rewrite. Shape:

```
ModelHubService.repair_credential_addresses(source_id) -> RepairOutcome
```

The runtime resolves the owning prefix through the existing
`credential_metadata(...)["prefix"]`, exactly as discovery does, and hands the
service a **proven** prefix. A Source whose credential no longer resolves is
skipped and reported — never guessed at.

### What it rewrites, in one transaction

Given `(source_id, prefix)`, build the rename map from the source's own rows —
`{stored_id: model_id_without_credential_address(stored_id, prefix)}`, keeping
only entries that actually changed — then apply it to every position in one
config write:

| Position | Rule |
| --- | --- |
| `source.models[].id` | Renamed. A row landing on a name another row already holds is dropped; the holder keeps its display name, reasoning efforts, and provenance. |
| `source.models[].reasoning_efforts` and the engine record's `model_reasoning_efforts` | Follow their row. |
| Route **hops** (`hop.source_id == source_id`) | Renamed only where the map has the spelling. Two hops collapsing onto one pair are merged; a route left with zero hops keeps its key and reports the ordinary unavailable-target path. |
| Route **keys** (menu ids a route is keyed by) | Renamed only when the key matches a renamed id *and* no route already exists under the bare name; otherwise the routes are merged hop-wise, bare-name-first. |
| Backend menu rows (`ModelHubBackendModelConfig`) | Renamed only for rows whose origin is `provider` (the origin `agent_model_candidates` produces from inventory) and whose id matches a renamed id. `builtin` / `models_dev` / `manual` rows are never touched. |
| `removed_model_ids` | Renamed, so a model the user hid stays hidden. |
| Usage ledger keys | **Not** rewritten. See below. |

Everything is computed first and written once, so a partial rename cannot be
persisted.

### The ledger is deliberately excluded

An addressed inventory id cannot have produced a ledger row: composition is
unconditional, so such a call went out double-addressed and came back an error
envelope; `TurnExecution.reached_model` classifies that as having reached no
model (`turn_gateway.py:226-267`), `owes_metering` is then False (`:280-282`),
and `_record_usage` returns before writing (`:1169`). Verified against the live
local regression ledger: 31 rows, 10 distinct model ids, **zero** addressed —
while `gpt-5.6-luna` / `gpt-5.6-terra`, whose inventory rows *were* addressed on
that host, already metered under their bare names.

Falsifiable claim: a usage row whose `model_id` matches `avibe-[0-9a-f]{24}/…`
on an upgrading installation. If one turns up, `usage_summary`'s label join
orphans it and a ledger re-key joins this design.

### When it is triggered

Preference order, cheapest first:

1. **On Source refresh / reachability check** — the runtime already holds the
   credential there, so the repair costs one extra config write and only on a
   Source that actually has addressed rows.
2. **On engine start, per bound Source** — covers a user who never clicks
   refresh. Guarded by a fast "does any row carry the minted shape" pre-check so
   the common case does no work.

Not a config-load migration, and not a one-shot script: it must run where the
credential is resolvable.

### Reporting

The outcome is reported, not silent: the number of rows, hops, keys, and menu
rows moved, per Source, in the runtime log, and a Source whose credential could
not be resolved is named so the user can reconnect it.

## Validation plan

- Unit: rename map built from a proven prefix; foreign address left alone;
  upstream slashes (`x-ai/grok-4.6-latest`, `anthropic/claude-sonnet-4`,
  `accounts/fireworks/models/…`) left alone; row-collision precedence; hop
  merge; route-key merge; menu row renamed only for `provider` origin;
  `removed_model_ids` followed.
- Property: a repaired config must serialize to one this product loads again —
  the failure mode is writing a surviving name twice and refusing the file on
  the next load.
- Integration: a source whose credential does not resolve is skipped whole, and
  its config bytes are unchanged.
- Regression (local Incus only): load a snapshot of an affected `config.json` +
  `sources.json`, refresh the Source, and confirm the six subscription models
  become selectable and invocable end to end.

## Out of scope

- Any change to `client.py:368` / `modules/agents/model_hub.py` composition. The
  outbound request is where the address belongs — it is the envelope, written at
  the moment of sending.
- Any change to `normalized_model_id` / `usage_ledger_key`.
- Widening `source_supports_passthrough`.
