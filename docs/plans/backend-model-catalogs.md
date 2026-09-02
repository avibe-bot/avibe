# Backend Model Catalogs

Status: implementation contract — v1 shipped in #1814; **v2 (compose from providers) below is the current definition of done, pending owner approval 2026-09-03**

## Outcome

Every Model Hub backend has one editable model catalog. The catalog controls the models
the Agent can select and stores one canonical capability description; each runtime adapter
consumes the subset its backend can represent. Supplier inventory and Routes keep their
current ownership.

For example, adding `deepseek-v3.2` to the Codex catalog makes that id appear in Codex.
It does not choose an upstream provider. The new row appears separately in the Route
panel, where the user can configure `aihub/deepseek-v3.2` and any fallbacks.

## One product model

The three Gateway backend cards expose the same `Manage models` action and the same
list/editor interaction. Their runtime adapters remain different:

| Backend | Catalog projection |
| --- | --- |
| Claude Code | The Gateway serves model ids while discovery is enabled. Context and output limits are injected for the selected Hub model. Claude Code's own `Default` selector remains visible and locked. Other canonical fields remain stored metadata. |
| Codex | Avibe writes a content-addressed `model_catalog_json` derived from the running binary's native schema. It projects display name, context, supported input modalities, and reasoning efforts; fields Codex cannot represent remain stored metadata. |
| OpenCode | Avibe writes the configured rows into its private runtime provider overlay, including context/output limits, modalities, tool/reasoning support, and reasoning variants. |

The shared catalog is authoritative. Adapter-specific files are derived runtime artifacts;
they are never edited by the user and never become another source of product state.

## Data ownership

`BackendModel` is defined by `model-hub-contracts/backend-model.schema.json`. The order
of `catalog_models` is the Agent menu order. Editable metadata is persisted under the
backend Agent supply configuration. `locked` and `routeable` are response-only server
projections.

The catalog deliberately contains no Source id, upstream model id, Route status, priority,
or fallback. Removing a routeable model is refused while a non-empty Route still uses its
id; the user removes the Route first, then the model. Supplier models are never removed by
a backend catalog mutation.

Existing installations migrate without losing routes:

- Claude Code and Codex seed their initial catalogs from the bundled backend catalog.
- OpenCode seeds its initial catalog from the existing checked menu, preserving order.
- Existing route keys not already present are appended as catalog rows so a configured
  Route never becomes invisible during migration.

## models.dev

models.dev is an optional metadata source, not an authority. The user enters a backend
model id, asks Avibe to fill it, selects a match when needed, and can edit every populated
field before or after saving. Avibe stores the chosen `provider/model` match and the
normalized snapshot; later models.dev changes do not silently overwrite user settings.

Avibe fetches the public catalog through a server-owned, conditional local cache. A failed
refresh may use a stale cache; no cache means the fill action fails explicitly while manual
entry remains available. Cost is not stored because no current backend catalog projection
consumes it.

## Mutations

`PUT /api/models/agents/<backend>/models` accepts `{baseline, models}`. `baseline` is the
last full list observed by the caller and `models` is its desired replacement. The server
applies the caller's adds, edits, removals, and order changes to the latest list while
preserving unrelated concurrent mutations. Duplicate ids, invalid backend-specific ids,
locked-row changes, and removal of a model with a non-empty Route are rejected.

`GET /api/models/catalog/models-dev?query=<text>` returns normalized matches. It does not
persist anything.

After a successful catalog mutation, the controller invalidates the affected backend's
runtime projection. The next turn observes the committed catalog; an already running
OpenCode server is refreshed before the mutation reports success.

Catalog storage is mode-independent so a Direct to Gateway switch preserves prior work.
The product editor is Gateway-only: Direct mode continues to use each CLI's native model
menu, so showing this editor there would falsely imply that these rows change Direct mode.

## Acceptance

- All three backend cards open the same model-list interaction.
- Add, edit, remove, search, and drag reorder work without exposing Route controls in the
  model-list dialog.
- A models.dev match fills context, output, modality, tool, reasoning, and effort fields,
  and user edits survive reload.
- A catalog change is visible in the corresponding real backend selector after runtime
  refresh.
- Existing Source inventory and Route chains remain byte-for-byte unchanged except when a
  user separately edits them.
- Direct mode preserves the catalog unchanged; switching back to Gateway restores the
  same editable list.

---

## v2 — Compose from providers (2026-09-03)

### Why v1 is wrong as a product

v1 shipped `Manage models` as a hand-typed record editor: the user types a backend model
id, optionally asks models.dev to fill metadata, saves, then opens the Route dialog and
picks the same upstream model again. The product already owns that fact twice — every
Source lists the models it supplies, and Add Source already matches those models into
routes once — so the flow re-enters data the product holds and hides the relationship
between providers, backend menus, and routes. The asymmetry is in `model-hub.md` §4.6:
matching runs when a Source is added but never when a menu model is added, so a model
added after its provider can only be routed by hand.

### Outcome

A backend's model menu is composed primarily by **selecting models that providers already
supply**. Typing a model id by hand remains available as the fallback for a model no
provider lists. Selecting a model creates one menu entry and its initial route — every
provider that supplies that id, in the backend's Source order — so the user immediately
sees what the Agent menu will show and where requests will go. Routes remain a separate
surface and are never edited inside the picker.

Worked example. Codex is in Gateway mode with two API-key Sources, `aihub` (24 models,
including `deepseek-v3.2`) and `openrouter` (also `deepseek-v3.2`), `aihub` first in
Codex's Source order. `Manage models` → `Add from providers` → type `deepseek` → the row
`deepseek-v3.2 · aihub, openrouter` → check → `Add 1 model`. Result: Codex's menu gains
`deepseek-v3.2` with display name and reasoning efforts taken from the supplying Source
models and context/output/modalities filled from models.dev when one exact match exists;
its route is `aihub → deepseek-v3.2`, `openrouter → deepseek-v3.2`; the Codex card row
reads `aihub → deepseek-v3.2`. Nothing was typed except the search.

### One product model — the seven questions

1. **Candidate identity when several providers supply the same model.** For Claude Code
   and Codex a candidate is one distinct upstream model id across all
   configuration-eligible, non-retired Source models — that id is exactly what the menu
   id will be. The row lists its supplying providers in Source order. For OpenCode a
   candidate is the existing OpenCode identifier `vendor/model` (§4.8), so two Sources
   with the same standard vendor collapse into one candidate and two vendors offering the
   same bare model stay two candidates, as OpenCode itself would show them.
2. **Membership key and metadata owner.** Unchanged: membership is keyed by backend
   model id per backend, and the `BackendModel` row owns display name, capabilities,
   context, output, and reasoning efforts. Selection seeds those fields — display name
   and reasoning efforts from the supplying Source model(s), the rest from models.dev —
   and every field stays editable afterwards.
3. **One selection → one menu entry → one route with N hops**, one hop per supplying
   Source in Source order, written by the existing `placement-v1` rule. The picker never
   edits an existing route; the Route dialog remains the only place that adds, removes,
   reorders, or cross-maps hops.
4. **models.dev** proposes, never rules. At add time the server fills empty metadata only
   when exactly one exact match exists (`provider/model` or model-id equality) and records
   `models_dev_id`. The editor's existing `Fill from models.dev` re-proposes on demand.
   No refresh ever overwrites a field the user has set; a proposal is reversible by
   editing the field.
5. **A supplying model disappears, is retired, or its Source is disabled or deleted.**
   Routes are configuration and are not rewritten by health (existing rule). The menu
   entry stays; its row shows the existing states (`Supply paused`, `No model route
   configured`). Source deletion removes hops through the existing guard. The picker shows
   such an entry as already added and never removes anything on its own.
6. **Claude locked/default and backend id constraints.** Claude Code's `default` row stays
   locked at position 0. Claude candidates are only upstream ids Claude accepts as menu ids
   (bundled builtins or `claude-` / `anthropic-`); any other upstream model reaches Claude
   Code only as an explicit mapped hop under a Claude-named menu model, configured in the
   Route dialog, where the mapping is visible on the row. Codex accepts any canonical id.
   OpenCode requires `vendor/model`, which the picker produces; the manual editor keeps
   its existing validation.
7. **Interaction without route controls.** `Manage models` keeps the list with edit,
   reorder, and remove. Its primary action becomes `Add from providers`, which opens the
   picker: search over model id, display name, and provider name; one row per candidate
   with a checkbox; already-added models shown checked and disabled; footer `Add N
   models`. `Add manually` is the secondary action and opens the existing editor. Duplicate
   prevention is by id. Removing an entry removes its route too: with a non-empty route the
   dialog first shows the hops that go with it and asks once (existing guard shape
   `would_remove_hops`); with an empty route removal is immediate. An empty catalog offers
   `Add from providers` when any eligible provider has inventory and otherwise says no
   provider supplies this backend yet and offers `Add manually`.

### Contract deltas (normative once approved; lanes cite these by number)

- **C1 — Menu-model add runs matching once.** `PUT /api/models/agents/<backend>/models`
  treats every row added relative to the latest stored list as a one-time matching point:
  the server runs `matching-v1` for that menu model against configuration-eligible Sources
  and writes the accepted hops with `placement-v1`. `model-hub.md` §4.2 and §4.6 are
  amended so that "catalog change never repeats matching" reads "a menu-model add matches
  once at that write; refresh, restart, health, and turns still never re-match". Manual
  adds of an id no Source lists therefore still start with an empty route.
- **C2 — Metadata seeding.** For each added row the server fills `display_name` and
  `reasoning_efforts` from the matched Source model(s) when the client sent them empty
  (union of efforts across matched Sources, Source order), then fills remaining empty
  metadata from models.dev only when exactly one exact match exists, recording
  `models_dev_id` and `origin: "models_dev"`. A models.dev cache miss is silent; the row is
  still written.
- **C3 — Removal cascades through the guard.** Removing a row whose route has hops returns
  the guarded `409 backend_model_in_route` with `would_remove_hops`; a `force: true` retry
  echoing that plan removes the row and its route in one transaction. The hard refusal in
  v1 is retired. Removing a row with an empty route needs no confirmation.
- **C4 — Candidates are derived, not served.** The picker derives candidates from
  `GET /api/models/sources` and `AgentSupply.sources` eligibility already loaded on the
  page (the same derivation the Route dialog's candidate popover uses), deduplicated by
  the identity rule in question 1 and minus ids already in the catalog. No new read
  endpoint.
- **C5 — Unchanged.** Source inventories, Source-side manual add, Route dialog, Source
  order, direct/gateway modes, and every runtime projection are byte-for-byte unchanged
  except through C1–C3.

### Copy (English source; `zh.json` mirrors 1:1)

| Where | String |
| --- | --- |
| Catalog dialog primary action | `Add from providers` |
| Catalog dialog secondary action | `Add manually` |
| Picker title | `Add {{backend}} models` |
| Picker search placeholder | `Search models or providers` |
| Picker columns | `Model` · `Providers` |
| Already-added row state | `Added` |
| Picker footer count | `{{count}} selected` |
| Picker confirm | `Add {{count}} models` |
| Picker empty (no eligible inventory) | `No provider supplies models for {{backend}} yet.` |
| Picker no match | `No model matches.` |
| Remove with route | `Also removes its route: {{hops}}` · button `Remove` |

Every other existing string in `settings.models.gateway.catalog.*` and
`settings.models.gateway.modelEditor.*` stays. No string explains mechanism.

### Acceptance (properties, not enumerations)

- Every menu entry created through the picker has a route whose hops are exactly the
  Sources supplying that id at add time, in that backend's Source order.
- The picker offers exactly the configuration-eligible, non-retired Source models for the
  backend that are not already in its catalog, deduplicated by the identity rule, and a
  search matches on id, display name, or provider name.
- A manual add of an id no Source lists still succeeds and leaves an empty route.
- Removing an entry with hops requires the guarded confirmation and removes both; without
  hops it is immediate.
- Source inventories, unrelated routes, and Source order are byte-identical before and
  after any catalog mutation.
- After a picker add, the real backend selector shows the new id after runtime refresh
  (Codex `codex debug models`, OpenCode overlay, Claude discovery), and the Codex card
  reveals a seventh-plus row (the #1829 behaviour is retained).
- Direct mode still hides the editor and preserves the catalog unchanged.

### Delivery notes

- PR #1829 (`fix(model-hub): reveal newly saved models`) stays valid independently of
  this redesign: the picker saves through the same `PUT` and the card still collapses at
  six rows. Decision: **keep and merge on its own gates**; the v2 UI lane bases on master
  after it lands.
- Design PR avibe-docs #34 carries the earlier "Batch Add / Agent Model Picker" proposal
  frames; v2 frames are drawn on that branch with sparse copy and the proposal frames are
  renamed `Superseded —` rather than deleted.
