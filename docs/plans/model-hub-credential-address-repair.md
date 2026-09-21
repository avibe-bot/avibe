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

What remained was **files already written**. This document is the design for
repairing those, and the repair ships in the same PR: the sections below are
what was built, with the two places the implementation diverged from the first
draft called out where they occur.

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

## What refresh already covers

One **refresh models** on the Source re-discovers through the fixed boundary and
`_apply_discovered_models` (`service.py:1904`) replaces the inventory rows with
bare-named ones. What refresh does not do is move a route hop or a route key
that already points at an addressed spelling — those are the positions this
repair owns, and it owns them without the user having to know to click anything.

## Design

### Where it runs

A service-level repair over the whole config, not a config-load rewrite and not
a per-Source call. Three pieces:

- `EngineAdapter.credential_address(credential_ref)`
  (`vibe/model_hub_runtime/adapter.py`) answers whichever address the engine
  would actually compose, settled the same way `bind_source` settles it: the
  credential's own recorded `prefix`, else the prefix the bound `SourceRecord`
  holds. Only an OAuth credential records one of its own — `store_api_key`
  writes none — so an answer drawn from the credential alone reported *no*
  address for a Source the engine addresses perfectly well, and the repair then
  declined exactly the ids that needed it. `None` when nothing records one,
  which is the honest answer and means the caller must leave the id alone.
- `core/handlers/model_hub/address_repair.py` is pure: given a `model_hub`
  payload and `{source_id: proven_address}`, it returns the repaired payload and
  the counts. It resolves nothing and writes nothing.
- `ModelHubService._repair_credential_addresses` joins the two under the
  mutation lock and persists through `_commit_synced`, the same projection owner
  every other mutation uses, so the engine is reconciled with what was written.

**Divergence from the first draft.** The draft had the repair take a
`source_id`. It takes none: a route key, a menu row, a hidden-model entry and a
checked entry record no Source, so a per-Source repair cannot reach them without
guessing. Resolving every Source's address first and then rewriting once removes
the guess — an address names exactly one credential and a credential binds to
exactly one Source, so an id carrying a proven address came from that Source
whatever collection it sits in.

### What it rewrites, in one transaction

The addresses are proven first; then every position is rewritten in one config
write:

| Position | Rule |
| --- | --- |
| `source.models[].id` | Renamed. A row landing on a name another row already holds is dropped; the holder keeps its display name, reasoning efforts, and provenance. |
| `source.models[].reasoning_efforts` and the engine record's `model_reasoning_efforts` | Follow their row. |
| Route **hops** | Unwrapped against the address of the Source the hop itself names, since a chain crosses Sources and each has its own address. Two hops collapsing onto one `(source_id, model_id)` pair are merged; a route left with zero hops keeps its key and reports the ordinary unavailable-target path. |
| Route **keys** (menu ids a route is keyed by) | Unwrapped against any proven address. Where that lands on a key another route already holds, the two merge hop-wise behind the key already spelled bare — whichever order the dict happened to list them in. |
| Backend menu rows (`ModelHubBackendModelConfig`) | Renamed when the id carries *any* proven address. **Divergence from the first draft**, which restricted this to `provider`-origin rows: the proof is the address, not the origin. A `builtin` or `models_dev` id is never addressed, so the restriction excluded nothing real, and it would have left a `manual` row the user pasted an addressed name into broken forever. |
| `removed_model_ids` | Renamed, so a model the user hid stays hidden. |
| `menu.checked` (opencode) | Renamed, so a model the user ticked stays ticked. |
| Usage ledger keys | **Not** rewritten. See below. |

Everything is computed first and written once, so a partial rename cannot be
persisted.

### The same id outside this config

An id also leaves the Model Hub. A user picks one from the menu, and the value
is copied into whichever row records that choice. A turn then reads those rows
back, in this precedence, to decide what it asks for
(`message_handler.py:505`):

| Position | Read as | Rule |
| --- | --- | --- |
| `agent_sessions.model` | The session's pin, highest precedence | Renamed |
| `scope_settings.settings_json` → `routing.{model,model_override,opencode_model,claude_model,codex_model}` | A channel's routing override; read **before** the column | Renamed |
| `scope_settings.model` | The same override, column form | Renamed |
| `agents.model` | The Vibe Agent's own model | Renamed |
| `agent_runs.model` | A record of a call that was made | **Not** renamed |
| `skill_usage_daily.model` | A metering key | **Not** renamed |

The split is selection versus record. The first four decide a future request;
leaving one addressed keeps the Agent broken after the catalog is repaired —
the failure only changes from a double-addressed `model_not_found` to a
`mapping_target_unavailable`. The last two describe calls already made, and
rewriting them would restate history.

`run_definitions` has no model column at all: a scheduled run resolves its
model live through its Agent, so it inherits the repair for free.

The hub proves which addresses are addresses; it does not own these rows. It
hands the proven set to `repair_model_selections`, which the controller wires
to `storage/model_selection_addresses.py`. Selections move **before** the
config is committed, because the config is the witness that anything needed
repairing at all — committing it first would clear that witness while a copy of
one of its ids was still stored as somebody's selection, and the retry the next
demand schedules would never fire. Both halves are idempotent, so the retry
costs nothing.

### A backend already running

Reconciling the engine projection says nothing about a backend process that is
already up: it answers from the catalog it was started with and keeps offering
the ids that were just renamed. Every backend whose menu, routes, or hops moved
is therefore handed to `_refresh_backend_catalog` after the commit — the same
follow-up `set_agent_models` makes. A refresh that fails is logged, not raised:
the repair has already landed on disk, and a stale in-memory catalog is not
worth failing the demand that triggered it.

Every one of these collections is uniqueness-checked on load, so a rename that
creates a collision it does not absorb would turn a loadable file into one that
fails config load — strictly worse than the addressed id it set out to fix. Two
rules settle every collision: a repaired entry landing on a name already spelled
bare gives way to the holder (the row the product has been listing, metering and
resolving against, which names nothing the repaired one does not); and among two
repaired entries meeting only because the address came off, the first wins,
which is discovery's own policy.

### An addressed menu entry never routed

Renaming a menu id looks like it could move which Source a turn lands on, so it
is worth stating what the addressed spelling could do before the repair: it
could not complete a call.

`effective_model_route` (`resolver.py:261`) takes the manual route first.
Without one it requires the id to be present in `agent.models`, then builds a
hop per eligible Source in `agent.sources.order`, each hop carrying that
Source's **own** inventory id. Two shapes are reachable and both fail:

- the Source's inventory carries the same addressed spelling — the shape this
  bug produces, since discovery wrote the addressed name into both — so the
  hop's `model_id` is addressed, the unconditional composition at
  `client.py:368` sends `<prefix>/<prefix>/<model>`, and the engine answers
  `model_not_found` (400);
- the inventory carries the bare name, so the literal comparison in
  `matching_v1_model_id` matches nothing and the request ends as zero hops
  (`mapping_target_unavailable`, 409) — or, through a passthrough-eligible
  `api_key` Source (`source_supports_passthrough`, `resolver.py:257`), as the
  same double-addressed 400.

So an addressed menu entry with no manual route has no working Source binding
for the rename to move. Where such an entry *did* work, it worked **through** a
manual route, whose hop names its Source and a bare model id; that route is
carried through the rename intact, merging hop-wise behind a bare key where one
already exists. This is why the justification is mechanical rather than a
product judgment about what a user meant by checking that box.

What does change: someone who was aiming at a particular Source through an
entry that only ever errored now gets a working call, which may resolve to the
first eligible Source in `agent.sources.order` rather than the one they had in
mind. That is a broken selection becoming a working one, not a pin being
silently moved, and the mechanism that does pin a Source — a manual route — is
unchanged and still available.

Two falsifiable claims, one per direction. Everything above guards a single
one: that a selection which never worked is not being misread as one that did.
The direction that costs more for a *repair* is the opposite — a working id
renamed into a broken one. An aggregating relay names its models
`openai/gpt-5.5` natively; that id goes out as `<prefix>/openai/gpt-5.5`, the
engine strips its own layer and forwards the rest, and the call **succeeds**: a
slashed, unpinned, working menu entry. Nothing about it is repaired, and that is
closed structurally rather than by argument. The authority is the *proven set*,
never a spelling: a segment is removed only when it equals an address recorded
for a credential this installation holds. The minted shape
(`_CREDENTIAL_ADDRESS_SHAPE`, `avibe-[0-9a-f]{24}` under `fullmatch` per
segment) is the fallback for the one caller with no owner to compare against,
and wherever the owning prefix is in hand the comparison is literal.

1. **An addressed menu entry, with no manual route, that completed a call
   before the repair.** If one exists, the rename does move a live binding and
   the reasoning above needs replacing.
2. **A model id this repair rewrote whose removed segment was not a proven
   address** — not the prefix recorded for any credential or Source here. Note
   the wording: *not* "carried no `avibe-<hex>` segment". `prefix` is settled
   from `credential["prefix"]`, else `previous.prefix`, and only then minted —
   in `StateStore.sync_sources`, the same order `bind_oauth_credential` and
   `stage_oauth_credential` mint under. (Cited by symbol on purpose: the #2098
   lane read these on pre-repair `master`, where the three mint sites sit at
   different lines than on this branch, and a bare line number does not survive
   the ref it was taken from.) So an installation can legitimately hold a
   prefix the mint would not produce today and removing it is correct —
   `test_only_the_owning_address_is_unwrapped` pins `legacy-prefix/gpt-5.5`
   unwrapping for the credential addressed by `legacy-prefix`. Where every
   recorded prefix is mint-shaped, the ordinary case, this reduces to one
   `grep`.

Both directions are test invariants rather than prose.
`test_only_the_minted_address_is_unwrapped` holds `x-ai/grok-4.6-latest`,
`meta-llama/Llama-3-70b-instruct`, `accounts/fireworks/models/mixtral` and
`anthropic/claude-sonnet-4` unchanged, along with the near-misses that make the
shape rule load-bearing: `avibe-short/…`, an uppercased address, and
`avibe-openai/gpt-5.5`, which reads as minted until the hex is counted.

The second direction is owed to the #2098 lane, which went looking for
counterexamples to the first and found the missing direction instead.

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

`ModelHubService._prepare_engine_for_demand`, the single path every demand for
the engine passes through — startup recovery (`recover_runtime_intent`), a
source probe, and an invocation all reach it. An installation upgrading into
this release is therefore repaired the first time it needs a model, without a
config-load migration and without a one-shot script.

`payload_carries_credential_address` gates it: a cheap walk of the same
collections the repair rewrites, matching the minted shape. It decides only
whether to pay for the credential reads — it never renames anything on its own
say-so, which is the distinction that makes the shape safe to use here and not
at the config layer.

### Reporting

One log line with the counts moved — source models, route hops, routes, agent
menu entries, and persisted model selections. A Source whose credential cannot
be resolved is logged at debug
and skipped whole; its unreachable credential is already surfaced through the
Source's own state, and the repair declining to touch it leaves exactly the
state the previous release was in.

The whole repair is best effort: a failure is logged and the demand continues.
An id left alone is a state the product already tolerates, and the next demand
tries again.

## Validation

`tests/test_model_hub_credential_address.py`:

- every collection moves together for one id — inventory, hops, route key, menu
  row, `removed_model_ids`, `menu.checked`;
- nothing is renamed without proof: an unresolvable Source and a foreign minted
  address both leave the file alone, alongside the upstream slashes
  (`x-ai/grok-4.6-latest`, `accounts/fireworks/models/…`);
- collision precedence both ways — the bare row keeps its place, and two
  repaired rows meeting here resolve first-wins;
- two routes meeting on one model merge hop-wise behind the bare key, and a hop
  is unwrapped against the address of the Source *that hop names*, not the
  Source whose row it sits under;
- the terminal property: a repaired config parses again, with no addressed id
  left anywhere the pre-check can see;
- custody is what proves an address — the runtime adapter answers the minted
  prefix for a bound credential and `None` for one it has no record of;
- end to end through `_prepare_engine_for_demand`: the stored file is repaired
  and the engine is sent the bare names, and with no provable address the file
  is left byte-for-byte as the previous release left it;
- the proof reaches the owner of persisted selections before the config is
  committed, a selection repair that raises leaves the config unwritten so the
  next demand repairs both halves, and a backend whose catalog moved is
  refreshed — while a refresh that fails does not fail the demand.

`tests/test_model_selection_addresses.py`:

- all three selections a turn resolves lose the address, including every key a
  scope's routing payload can spell one under;
- an identity no address proves is left alone, including a foreign minted
  address and the upstream slashes;
- a second pass finds nothing left to move, an empty proof set touches nothing,
  and one unreadable routing payload does not stop the rest.

Still to do before close-out: a local Incus regression on a snapshot of an
affected `config.json` + `sources.json`, confirming the subscription models
become selectable and invocable end to end.

## Out of scope

- Any change to `client.py:368` / `modules/agents/model_hub.py` composition. The
  outbound request is where the address belongs — it is the envelope, written at
  the moment of sending.
- Any change to `normalized_model_id` / `usage_ledger_key`.
- Widening `source_supports_passthrough`.
