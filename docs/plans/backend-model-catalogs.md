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

Revision 2026-09-03 (owner review): one add action with four origins, recoverable built-ins,
and automatic arrival of new built-ins replace the earlier two-button proposal.

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
Codex's Source order. `Manage models` → `Add models` → type `deepseek` → under `From your providers` the row
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
7. **One `Add models` action, four origins, one list.** `Manage models` keeps the list with
   edit, reorder, and remove, and has exactly one add action, `Add models`. It opens a picker
   whose body is one grouped list filtered by one search box; a candidate id appears in
   exactly one group, the first that knows it:
   1. **`Codex built-in`** (`Claude Code built-in`) — the backend's own model list
      (bundled + remote backend catalog + the local CLI cache the product already merges)
      minus what is already in the menu. This is where a removed built-in is found again.
      OpenCode has no built-in list; the group is absent for it.
   2. **`From your providers`** — every configuration-eligible, non-retired Source model
      not already in the menu, deduplicated by the identity rule in question 1, with its
      supplying providers as read-only chips in Source order.
   3. **`models.dev`** — a browsable catalogue, never a blank box. With an empty query the
      group shows one row of vendor chips (`Anthropic` `OpenAI` `Google` `DeepSeek` `Zhipu`
      `Moonshot` `Qwen` `xAI` `Mistral` … `More`); choosing a vendor lists that vendor's
      models as ordinary rows (the chip stays selected; choosing it again clears). With a
      query the group lists matching models ranked first-party first (the vendor that makes
      the model), then exact-id, then name matches. One row per model: when several
      models.dev providers list the same model, only the first-party entry is shown and the
      aggregator copies are collapsed, because for Claude Code and Codex the menu id is the
      bare model id either way, and for OpenCode the id is `vendor/model` with the
      first-party vendor. A row shows the display name, the mono id, and the vendor as muted
      text (omitted when a vendor chip already filters the list); it never shows a chip.
   4. **`Add "{{query}}" by ID`** — not a group and not an input box: one fallback row that
      appears at the end of the list only while a query is typed, offering the typed text as
      a model id. Its checkbox joins the selection like any row; it is disabled when the text
      is not a valid id for the backend. With an empty query there is nothing to add by id,
      so the row is absent.
   Rules that hold across all groups: a provider chip on any row — built-in included — means
   exactly "one of your providers supplies this id", in Source order, and nothing else ever
   renders as a chip; models.dev vendor chips are the one exception and live only in the
   models.dev group's browse row, where they are filters, not suppliers. With an empty query
   the list shows only what can be added; with a query, models already in the list also
   appear in their group as disabled rows tagged `In list`, so a search never dead-ends on
   something that exists. The models.dev group shows a loading state while a query is in
   flight and `models.dev unavailable` when the catalogue cannot be reached; it is omitted
   when a query matches nothing there.
   Checked rows — including a checked `Add "{{query}}" by ID` row — commit together through the footer (`{{count}} selected`
   · `Add {{count}} models`; with nothing selected the primary button reads `Add models`
   and is disabled, so the footer never moves) into the catalog draft; the list's single `Save` still writes
   everything with one `PUT`. Whatever the origin, adding a row runs the same one-time
   matching for its initial route (C1); an id no provider supplies simply starts with an
   empty route and the row shows `No model route configured`.
8. **A removed built-in must be recoverable.** Removing a built-in row hides it rather than
   forgetting it: the backend catalog records the hidden built-in id, the row leaves the
   menu and its route follows the removal rule (C3), and the id reappears in the
   `Codex built-in` group of the picker. Picking it again clears the hidden mark. Removing
   any other row deletes it; a provider model reappears in `From your providers`, a
   models.dev or By-ID model is found again through search.
9. **New built-ins join the menu by themselves; removed ones stay removed.** When the
   backend's built-in list gains a model (a Codex update, a refreshed remote catalog), the
   product adds it to the menu unless its id carries the hidden mark, inserting it where the
   built-in list orders it among the built-ins still present (after the last one when none
   follows; at the end when none remains) and seeding its route by C1. Nothing is ever
   removed automatically: a built-in the backend withdraws stays in the menu because a
   provider may still serve it. The menu is therefore always "the backend's list, minus what
   you removed, plus what you added" — with no mode switch to explain.

### Contract deltas (normative once approved; lanes cite these by number)

- **C1 — Menu-model add runs matching once.** `PUT /api/models/agents/<backend>/models`
  treats every row added relative to the latest stored list as a one-time matching point:
  the server runs `matching-v1` for that menu model against configuration-eligible Sources
  and writes the accepted hops with `placement-v1`. `model-hub.md` §4.2 and §4.6 are
  amended so that "catalog change never repeats matching" reads "a menu-model add matches
  once at that write; refresh, restart, health, and turns still never re-match". Manual
  adds of an id no Source lists therefore still start with an empty route.
  The success response is the existing `{agent: AgentSupply}`; the seeded hops are visible
  in `agent.routes[<id>].hops` and `agent.model_supply` in the same response.
- **C2 — Metadata seeding.** For each added row the server fills `display_name` and
  `reasoning_efforts` from the matched Source model(s) when the client sent them empty
  (union of efforts across matched Sources, Source order), then fills remaining empty
  metadata from models.dev only when exactly one exact match exists, recording
  `models_dev_id` and `origin: "models_dev"`. A models.dev cache miss is silent; the row is
  still written. `origin` gains the value `"provider"` for rows picked from
  `From your providers`; `"builtin"`, `"models_dev"`, and `"manual"` keep their meaning.
- **C3 — Removal cascades through the guard.** `PUT /api/models/agents/<backend>/models`
  accepts the guarded-mutation fields beside `baseline` and `models`:
  `{baseline, models, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}`.
  Removing a row whose route has hops without `force` returns the existing guard-refusal
  envelope `409 {ok: false, error: "backend_model_in_route", would_remove_hops: RouteHopRef[],
  would_interrupt: SupplyGap[]}` (`guard-refusal.schema.json`; `RouteHopRef` is
  `{backend, menu_model, source_id, model_id, position}`). A retry with `force: true` that
  echoes both arrays unchanged removes the rows and their routes in one transaction and
  returns `{agent: AgentSupply, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}`.
  Removing rows whose routes are empty needs no confirmation and returns `{agent}` as
  today. The v1 hard refusal is retired.
- **C4 — Candidates come from reads that already exist.** The picker derives its groups from
  reads the page already has or the product already serves: the built-in group from the
  backend model catalog snapshot (`agent_model_options` / `fetchBackendModels`, the same
  merged remote + bundled + local-CLI snapshot every model picker uses) minus menu ids and
  including hidden built-ins; the provider group from `GET /api/models/sources` and
  `AgentSupply.sources` eligibility (the Route dialog's candidate derivation, promoted to a
  shared helper); the models.dev group from `GET /api/models/catalog/models-dev?query=`.
  No new read endpoint.
- **C6 — Hidden built-ins and built-in reconcile.** The backend catalog persists the set of
  hidden built-in ids (`agents.<backend>.hidden_builtin_ids: string[]`, absent on older
  files = empty). `PUT .../models` marks a removed row hidden when its id is in the current
  built-in snapshot and clears the mark when such an id is added. On every built-in snapshot
  change (startup, remote catalog refresh, CLI cache change) the service adds each built-in
  id that is neither in the menu nor hidden, at the position question 9 defines, running C1
  and C2 for it, and invalidates the backend's runtime projection. It never removes a row.
  Claude Code's locked `default` row is not a built-in candidate and cannot be hidden.
- **C7 — models.dev read serves browse and ranked search.** `GET /api/models/catalog/models-dev`
  gains `vendor=<provider_id>` beside `query=`, and returns `{vendors: [{id, name, model_count}],
  models: [...]}`. `models` is deduplicated by model id, ranked first-party vendor first, then
  exact-id, then name matches; each item carries `vendor_id`, `vendor_name`, `first_party:
  boolean`, and the existing metadata fields. With neither parameter the response carries only
  `vendors`, first-party vendors ordered by model count. First-party means the provider that
  makes the model; aggregator providers (openrouter, together, fireworks, groq, deepinfra, …)
  never win the ranking when a first-party entry exists.
- **C5 — Unchanged.** Source inventories, Source-side manual add, Route dialog, Source
  order, direct/gateway modes, and every runtime projection are byte-for-byte unchanged
  except through C1–C3.

### Copy (English source; `zh.json` mirrors 1:1)

| Where | String |
| --- | --- |
| Catalog dialog add action (the only one) | `Add models` |
| Picker title | `Add {{backend}} models` |
| Picker search placeholder | `Search models or vendors` |
| Group headers | `{{backend}} built-in` · `From your providers` · `models.dev` |
| models.dev browse row | vendor chips by display name, then `More` |
| models.dev group, empty query | `Type to search models.dev` |
| Fallback row (query only) | `Add "{{query}}" by ID` |
| Group with nothing to offer | the group is omitted, never an empty header |
| Picker footer count | `{{count}} selected` |
| Picker confirm | `Add {{count}} models`; disabled `Add models` when nothing is selected |
| Already-in-list row (search only) | `In list` |
| models.dev group states | vendor chips (empty query) · loading · `models.dev unavailable` |
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
- Removing a built-in row and reopening the picker shows that id under the built-in group;
  picking it restores the row. A built-in id that appears in the backend snapshot after the
  menu was saved is present in the menu on the next read unless it was removed before, and
  a previously removed id never returns on its own.
- The picker lists each candidate id exactly once across all groups.

### Delivery notes

- PR #1829 (`fix(model-hub): reveal newly saved models`) stays valid independently of
  this redesign: the picker saves through the same `PUT` and the card still collapses at
  six rows. Decision: **keep and merge on its own gates**; the v2 UI lane bases on master
  after it lands.
- Design PR avibe-docs #34 carries the earlier "Batch Add / Agent Model Picker" proposal
  frames; v2 frames are drawn on that branch with sparse copy and the proposal frames are
  renamed `Superseded —` rather than deleted.
