# Backend Model Catalogs

Status: implementation contract — v1 shipped in #1814; **v2 (compose from providers) below is the current definition of done, approved by the owner on 2026-09-03; C1–C8 are normative**

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

Revision 2026-09-03 (owner review, approved): one `Add models` action that selects what already
exists (the backend's built-in list, your providers' inventories) and hands everything else to a
custom-model editor where models.dev is the lookup; built-ins are recoverable after removal and
new built-ins arrive by themselves.

v1 shipped `Manage models` as a hand-typed record editor: the user types a backend model
id, optionally asks models.dev to fill metadata, saves, then opens the Route dialog and
picks the same upstream model again. The product already owns that fact twice — every
Source lists the models it supplies, and Add Source already matches those models into
routes once — so the flow re-enters data the product holds and hides the relationship
between providers, backend menus, and routes. The asymmetry is in `model-hub.md` §4.6:
matching runs when a Source is added but never when a menu model is added, so a model
added after its provider can only be routed by hand.

### Outcome

A backend's model menu is composed primarily by **selecting models that already exist** — the
backend's own built-in list and the models your providers supply. A custom model, looked up
on models.dev or typed by hand in the editor, remains the fallback for anything neither lists. Selecting a model creates one menu entry and its initial route — every
provider that supplies that id, in the backend's Source order — so the user immediately
sees what the Agent menu will show and where requests will go. Routes remain a separate
surface and are never edited inside the picker.

Worked example. Codex is in Gateway mode with two API-key Sources, `aihub` (24 models,
including `deepseek-v3.2`) and `openrouter` (also `deepseek-v3.2`), `aihub` first in
Codex's Source order. `Manage models` → `Add models` → type `deepseek` → under `From your providers` the row
`deepseek-v3.2 · aihub, openrouter` → check → `Add 1 model`. Result: Codex's menu gains
`deepseek-v3.2` with display name and reasoning efforts taken from the supplying Source
models (both editable in the list before Save; other metadata stays empty until the user
fills it); its route is `aihub → deepseek-v3.2`, `openrouter → deepseek-v3.2`; the Codex card row
reads `aihub → deepseek-v3.2`. Nothing was typed except the search.

### One product model — the seven questions

1. **Candidate identity when several providers supply the same model.** For Claude Code
   and Codex a candidate is one distinct upstream model id across the non-retired models —
   discovered or manually added — of every Source that is configuration-eligible for the
   backend **and present in that backend's Source order**; a Source held out of the order
   supplies nothing here. That id is exactly what the menu id will be. The row lists its
   supplying providers in Source order. For OpenCode a
   candidate is the existing OpenCode identifier `vendor/model` (§4.8), so two Sources
   with the same standard vendor collapse into one candidate and two vendors offering the
   same bare model stay two candidates, as OpenCode itself would show them.
2. **Membership key and metadata owner.** Unchanged: membership is keyed by backend
   model id per backend, and the `BackendModel` row owns display name, capabilities,
   context, output, and reasoning efforts. Selection seeds display name and reasoning
   efforts from the supplying Source model(s) into the editable draft before Save; other
   metadata stays empty until the user fills it (the custom-model editor's models.dev
   lookup is the assistant for that). Every field stays editable.
3. **One selection → one menu entry → one route with N hops**, one hop per supplying
   Source in Source order, written by the existing `placement-v1` rule. The picker never
   edits an existing route; the Route dialog remains the only place that adds, removes,
   reorders, or cross-maps hops.
4. **models.dev** proposes, never rules. The server never fills metadata on a user's save.
   In the editor the `Model` typeahead proposes models.dev matches while the user types and
   fills the form on selection, recording `models_dev_id`; the user can change any field
   before saving. No refresh ever overwrites a field the user has set; a proposal is
   reversible by editing the field.
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
7. **One `Add models` action: select what exists, define the rest.** `Manage models` keeps the
   list with edit, reorder, and remove, and has exactly one add action, `Add models`. It opens
   a picker whose body is one grouped list filtered by one search box; a candidate id appears
   in exactly one group, the first that knows it:
   1. **`Codex built-in`** (`Claude Code built-in`) — the backend's own model list
      (bundled + remote backend catalog + the local CLI cache the product already merges)
      minus what is already in the menu. This is where a removed built-in is found again.
      OpenCode has no built-in list; the group is absent for it.
   2. **`From your providers`** — every non-retired model, discovered or manual, of the
      backend's ordered and configuration-eligible Sources, not already in the menu,
      deduplicated by the identity rule in question 1, with its supplying providers as
      read-only chips in Source order.
   3. **`Add custom model…`** — the picker's footer-left action (and, when a query matches
      nothing, an inline `Add "{{query}}" as a custom model…` under `No model matches.`). It
      opens the existing model editor for a model neither the backend nor your providers
      list. models.dev lives here, as the lookup that helps fill the form: the editor's
      first field, `Model`, is a typeahead (`Search models.dev or enter a model ID`) whose
      suggestions are models.dev matches ranked first-party vendor first, deduplicated by
      model (aggregator copies collapsed), each showing name, mono id, vendor, and context;
      choosing one fills id, display name, context, output, modalities, tools, reasoning,
      and efforts, all still editable; the last suggestion is always `Use "{{query}}" as the
      model ID` for an id models.dev does not know. The old `Fill from models.dev` button is
      retired — the typeahead is the same capability, offered before typing instead of after.
      For OpenCode the filled id is `vendor/model` with the first-party vendor.
   Rules that hold across all groups: a provider chip on any row — built-in included — means
   exactly "one of your providers supplies this id", in Source order, and nothing else ever
   renders as a chip. With an empty query the list shows only what can be added; with a
   query, every model already in the list that matches — built-in, provider, or custom
   alike — appears in one search-only group, `Already in the list`, as disabled rows, so a
   search never dead-ends on something that exists and never offers a duplicate. The
   editor's typeahead
   shows a loading state while a lookup is in flight and `models.dev unavailable` when the
   catalogue cannot be reached; typing an id by hand always still works.
   Checked rows commit together through the footer primary (`Add {{count}} models`; with
   nothing selected it reads `Add models` and is disabled, so the footer never moves) into the
   catalog draft; a custom model returns from the editor into the same draft; the list's single `Save` still writes
   everything with one `PUT`. Whatever the origin, adding a row runs the same one-time
   matching for its initial route (C1); an id no provider supplies simply starts with an
   empty route and the row shows `No model route configured`.
8. **A removed built-in must be recoverable.** Removing a built-in row hides it rather than
   forgetting it: the backend catalog records the hidden built-in id, the row leaves the
   menu and its route follows the removal rule (C3), and the id reappears in the
   `Codex built-in` group of the picker. Picking it again clears the hidden mark. Removing
   any other row deletes it; a provider model reappears in `From your providers`, and a
   custom model is defined again through `Add custom model…`.
9. **New built-ins join the menu by themselves; removed ones stay removed.** When the
   backend's built-in list gains a model (a Codex update, a refreshed remote catalog), the
   product adds it to the menu unless its id carries the hidden mark, inserting it where the
   built-in list orders it among the built-ins still present (after the last one when none
   follows; at the end when none remains) and seeding its route by C1. Nothing is ever
   removed automatically: a built-in the backend withdraws stays in the menu because a
   provider may still serve it. The menu is therefore always "the backend's list, minus what
   you removed, plus what you added" — with no mode switch to explain.

### Contract deltas (normative — owner-approved 2026-09-03; lanes cite these by number)

- **C1 — Menu-model add runs matching once.** `PUT /api/models/agents/<backend>/models`
  treats every row added relative to the latest stored list as a one-time matching point:
  the server runs `matching-v1` for that menu model against the Sources present in that
  backend's Source order that are configuration-eligible, over each Source's complete
  non-retired inventory — discovered and manual models alike (§4.2's
  `observed.discovered_models` restriction belongs to Add-Source observation, not to this
  point) — and writes the accepted hops with `placement-v1`. `model-hub.md` §4.2 and §4.6 are
  amended so that "catalog change never repeats matching" reads "a menu-model add matches
  once at that write; refresh, restart, health, and turns still never re-match". Manual
  adds of an id no Source lists therefore still start with an empty route.
  The success response is the existing `{agent: AgentSupply}`; the seeded hops are visible
  in `agent.routes[<id>].hops` and `agent.model_supply` in the same response.
- **C2 — Seeding and provenance.** Seeding for a user-initiated add happens in the client
  draft before Save, never on the server at `PUT`: the picker seeds `display_name` (from the
  first ordered supplier that has one) and `reasoning_efforts` (union across suppliers, Source
  order) from the Source models it already holds; the custom editor seeds from the chosen
  models.dev suggestion. The server stores the request literally — an empty field in the
  request is an empty field — so a value the user cleared before the first Save stays
  cleared. Server-side seeding exists only for built-ins the reconcile (C6) adds without a
  user draft: label and efforts from the built-in snapshot. `origin` records the creation
  path only — `builtin` (built-in group or reconcile), `provider` (provider group),
  `models_dev` (custom editor via a models.dev suggestion), `manual` (custom editor by hand)
  — and `models_dev_id` records enrichment independently; enrichment never changes `origin`.
- **C3 — Removal cascades through the guard.** `PUT /api/models/agents/<backend>/models`
  accepts the guarded-mutation fields beside `baseline` and `models`:
  `{baseline, models, force?: boolean, would_remove_hops?: RouteHopRef[], would_interrupt?: SupplyGap[]}`.
  Removing a row whose route has hops without `force` returns the existing guard-refusal
  envelope `409 {ok: false, contract_version, error: "backend_model_in_route", detail?,
  would_remove_hops: RouteHopRef[], would_interrupt: SupplyGap[]}`; `guard-refusal.schema.json`'s
  `error` enum gains `backend_model_in_route`, registered in `mirror-registry.json`,
  `api-response.schema.json`, and the locale keys the contract guard requires (`RouteHopRef` is
  `{backend, menu_model, source_id, model_id, position}`). A retry with `force: true` that
  echoes both arrays unchanged removes the rows and their routes in one transaction and
  returns `{agent: AgentSupply, removed_hops: RouteHopRef[], interrupted: SupplyGap[]}`.
  Removing rows whose routes are empty needs no confirmation and returns `{agent}` as
  today. The v1 hard refusal is retired.
- **C4 — Candidates come from reads that already exist.** The picker derives its groups from
  reads the page already has or the product already serves: the built-in group from
  `builtin_models` and `hidden_builtin_ids` on the agent models read (C8) minus menu ids —
  not from `fetchBackendModels`, which in Gateway mode returns the persisted catalog; the
  provider group from `GET /api/models/sources` and
  `AgentSupply.sources` eligibility (the Route dialog's candidate derivation, promoted to a
  shared helper); the editor typeahead from `GET /api/models/catalog/models-dev?query=` (C7).
  No new read endpoint.
- **C6 — Hidden built-ins and built-in reconcile.** The backend catalog persists the set of
  hidden built-in ids (`agents.<backend>.hidden_builtin_ids: string[]`, absent on older
  files = empty). `PUT .../models` marks a removed row hidden when its id is in the current
  built-in snapshot and clears the mark when such an id is added. Legacy initialization: a
  persisted catalog without the field is initialized once, at load, to `built-in snapshot ids
  − menu ids` and persisted, so an upgrade from v1 changes nothing the user sees and a
  built-in removed under v1 does not return; only built-ins that enter the snapshot after that
  initialization arrive by question 9. Reconcile runs only once the field exists. On every
  built-in snapshot change (startup, remote catalog refresh, CLI cache change) the service
  adds each built-in id that is neither in the menu nor hidden, at the position question 9 defines, running C1
  and C2 for it, and invalidates the backend's runtime projection. It never removes a row.
  Claude Code's locked `default` row is not a built-in candidate and cannot be hidden.
- **C7 — models.dev read serves the editor typeahead.** `GET /api/models/catalog/models-dev?query=`
  returns `{models: [...]}` deduplicated by model id and ranked first-party vendor first, then
  exact-id, then name matches; each item carries `vendor_id`, `vendor_name`, `first_party:
  boolean`, and the existing metadata fields; results are capped at 8. `first_party` is derived
  from a repo-owned, versioned vendor map (`vibe/data/model_vendors.json`: model-id family
  prefix → models.dev provider id, e.g. `claude-` → `anthropic`, `gpt-`/`o` → `openai`,
  `gemini-` → `google`, `deepseek-` → `deepseek`, `glm-` → `zhipuai`, `kimi-` → `moonshotai`,
  `grok-` → `xai`, `qwen` → `alibaba`, `mistral-`/`codestral-` → `mistral`): a provider is
  first-party for a model exactly when the map names it. When no listed provider is
  first-party, the winner is the first provider in the map's ordered aggregator list
  (`openrouter`, `together`, `fireworks-ai`, `groq`, `deepinfra`), and providers the map does
  not know rank after those alphabetically. The map is the closed rule; extending it is a
  versioned data change covered by a test.
- **C8 — Built-in snapshot on the agent models read.** `GET /api/models/agents/<backend>/models`
  returns, beside `catalog_models`, `builtin_models: [{id, display_name, reasoning_efforts}]` —
  the merged remote + bundled + local-CLI snapshot for that backend, served regardless of
  Gateway/Direct mode — and `hidden_builtin_ids: string[]`. OpenCode returns an empty
  `builtin_models`. No new endpoint.
- **C5 — Unchanged.** Source inventories, Source-side manual add, Route dialog, Source
  order, direct/gateway modes, and every runtime projection are byte-for-byte unchanged
  except through C1–C3, C6, and C8.

### Copy (English source; `zh.json` mirrors 1:1)

| Where | String |
| --- | --- |
| Catalog dialog add action (the only one) | `Add models` |
| Picker title | `Add {{backend}} models` |
| Picker search placeholder | `Search models or providers` |
| Group headers | `{{backend}} built-in` · `From your providers` |
| Already-in-list group (search only) | header `Already in the list`; rows disabled |
| Picker no match | `No model matches.` · inline action `Add "{{query}}" as a custom model…` |
| Picker footer, left | `Add custom model…` |
| Picker confirm | `Add {{count}} models`; disabled `Add models` when nothing is selected |
| Editor first field | label `Model` · placeholder `Search models.dev or enter a model ID` |
| Editor typeahead item | `{{name}}` · `{{id}}` · `{{vendor}}` · `{{context}}` |
| Editor typeahead last item | `Use "{{query}}" as the model ID` |
| Editor typeahead states | loading · `models.dev unavailable` |
| Remove with route | `Also removes its route: {{hops}}` · button `Remove` |

Every other existing string in `settings.models.gateway.catalog.*` and
`settings.models.gateway.modelEditor.*` stays, except `Fill from models.dev`, which is removed.
No string explains mechanism.

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
- A field the user cleared before the first Save is empty after Save.
- Loading a v1 catalog shows exactly the same menu before and after the first load, and a
  built-in the user removed under v1 does not return on its own.
- In the editor, a models.dev suggestion fills every metadata field it knows and leaves each
  one editable; an id models.dev does not know can still be entered and saved.

### Delivery notes

- PR #1829 (`fix(model-hub): reveal newly saved models`) stayed valid independently of this
  redesign — the picker saves through the same `PUT` and the card still collapses at six
  rows — and was merged on its own gates (`aa6d4d99e`). The v2 lanes base on master after it.
- Design PR avibe-docs #34 carries the earlier "Batch Add / Agent Model Picker" proposal
  frames; v2 frames are drawn on that branch with sparse copy and the proposal frames are
  renamed `Superseded —` rather than deleted.
