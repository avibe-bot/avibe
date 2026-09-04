# Backend Model Catalogs

Status: implementation contract — v1 shipped in #1814; **v2 (compose from providers) below is the current definition of done, approved by the owner on 2026-09-03; C1–C7 are normative**

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
   with the same standard vendor collapse into one candidate, two distinct standard vendors
   offering the same bare model stay two candidates, and Sources whose vendor §4.8 normalizes
   to `custom/` collapse into one `custom/<model>` candidate with several suppliers — exactly
   the identities OpenCode itself would show.
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
      minus what is already in the menu, served by C4. This is where a removed built-in is
      found again. OpenCode has no built-in list; the group is absent for it.
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
      model ID` for an id models.dev does not know (for OpenCode it offers `custom/<query>`
      unless the query is already an admissible `vendor/model` id). The old `Fill from models.dev` button is
      retired — the typeahead is the same capability, offered before typing instead of after.
      For OpenCode the filled id follows C7's provider rule; the manual path applies three
      buckets — a bare model id gains exactly one `custom/` prefix, a complete valid
      `provider/model` identity (any syntactically valid provider) is kept verbatim, and a
      malformed identity (`openai/`, `/model`) is rejected, never repaired into another id.
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
8. **A removed built-in must be recoverable, and nothing removed comes back by itself.**
   Removing any row records its id as removed (C6); the row leaves the menu and its route
   follows the removal rule (C3). A removed built-in reappears in the `Codex built-in` group
   of the picker for as long as the backend still lists it, a removed provider model in
   `From your providers` for as long as an ordered Source supplies it, and picking either
   again clears the mark; anything else — a removed custom model, or a built-in the backend has
   since withdrawn — is re-created through `Add custom model…`. The product keeps no history
   of candidates it no longer serves.
9. **New built-ins join the menu by themselves; removed ones stay removed.** When the
   backend's built-in list gains a model (a Codex update, a refreshed remote catalog), the
   product adds it to the menu unless its id is in the removed set (C6), inserting it where the
   built-in list orders it among the built-ins still present (after the last one when none
   follows; at the end when none remains) and seeding its route by C1. Nothing is ever
   removed automatically: a built-in the backend withdraws stays in the menu because a
   provider may still serve it. The menu is therefore always "the backend's list, minus what
   you removed, plus what you added" — with no mode switch to explain.

### Contract deltas (normative — owner-approved 2026-09-03; lanes cite these by number)

Authority rule for this section: it states *what must hold*; the exact schema, route-table,
and mirror-registry edits belong to the lane that owns `docs/plans/model-hub-contracts/`, with
the contract guard as the test. Nothing here introduces a second vocabulary for a shipped shape.

- **C1 — Menu-model add runs matching once.** `PUT /api/models/agents/<backend>/models` keeps
  its existing optimistic three-way merge: the caller's additions are `desired − baseline`,
  merged onto the latest stored list, so a concurrent removal of an unrelated row survives.
  Each addition that is still absent from the latest stored list at commit time is a one-time
  matching point (an id another caller added meanwhile keeps its existing route untouched): the server runs the existing `matching-v1`
  (including its Claude alias rule) for that menu model against the Sources present in that
  backend's Source order that are configuration-eligible, over each Source's complete
  non-retired inventory — discovered and manual alike (`model-hub.md` §4.2's
  `observed.discovered_models` restriction belongs to Add-Source observation, not here) — and
  writes the accepted hops with `placement-v1`. §4.2 and §4.6 are amended so that "catalog
  change never repeats matching" reads "a menu-model add matches once at that write; refresh,
  restart, health, and turns still never re-match". An id no ordered Source supplies starts
  with an empty route. The picker echoes what it showed: `PUT` accepts
  `expected_suppliers?: {<id>: [{source_id, model_id}]}` for caller-added ids, and when the
  server's matching result differs for any listed id it commits nothing and refuses with
  `409 {ok: false, contract_version, error: "candidate_suppliers_changed", detail, changed:
  {<id>: [{source_id, model_id}]}}` — its own registered response shape (with the `detail` key
  every `ModelHubError` carries), not a guard refusal, since nothing is removed or interrupted; the picker then refreshes its candidates, keeps each still-offered chosen id with its new
  suppliers, drops any id no longer offered (the changed count is the signal; no copy), and asks
  again; unchecking a re-asked row removes it from the selection and nothing else; the picker refreshes its candidates and asks again. The success response is the existing
  `{agent: AgentSupply}`.
- **C2 — Proposed values travel in the candidates read; the save is literal.** The server
  proposes `display_name` and `reasoning_efforts` for every candidate (C4): for a built-in
  from the built-in snapshot, for a provider model from its suppliers (display name from the
  first ordered supplier that has one; efforts = union in Source order). The picker copies
  those proposals into the editable catalog draft; the custom editor copies the chosen
  models.dev suggestion. A proposal contains only values the catalog can store — an effort
  value that violates `backend-model.schema.json` is left out of the proposal, never copied. `PUT` stores the request literally — an empty field in the request
  is an empty field — so a value the user cleared before the first Save stays cleared.
  Server-side seeding at write time exists only for built-ins the reconcile (C6) adds without
  a user draft, from the same snapshot values. `origin` records the creation path only —
  `builtin` (built-in group or reconcile), `provider` (provider group), `models_dev` (custom
  editor via a models.dev suggestion), `manual` (custom editor by hand); `models_dev_id`
  records enrichment independently and never changes `origin`.
- **C3 — Removal cascades through the guard.** `PUT /api/models/agents/<backend>/models`
  accepts the guarded-mutation fields beside `baseline` and `models` (`force?`,
  `would_remove_hops?`, `would_interrupt?`). Removing a row whose route has hops without
  `force` is refused with the guard-refusal envelope, `error: "backend_model_in_route"`, and a
  nonempty `would_remove_hops` (`RouteHopRef` = `{backend, menu_model, source_id, model_id,
  position}`); a retry with `force: true` echoing the plan removes the rows and their routes in
  one transaction and returns `{agent, removed_hops, interrupted}`; rows with empty routes are
  removed without confirmation and return `{agent}`. The client never constructs a plan it
  sends: the in-row confirmation may preview the consequence from the routes it already holds,
  but the arrays it echoes with `force` are always the server's own refusal arrays, verbatim
  and in the server's order; when the server's plan equals the previewed one as a set the retry
  is automatic, otherwise the server's plan is shown and confirmed. The confirmation shows both
  the hops and any interrupted Agents with the existing guard-impact surface. Property: every response this route
  emits — the plain success, the forced success with `removed_hops` and `interrupted`, and
  the refusal — validates against the response registry after the artifact changes below,
  the 409 refusal alone also validates against `guard-refusal.schema.json`, and the response
  guard exercises each of them. The v1 hard
  refusal is retired.
- **C4 — The server serves the picker's candidates; the client only renders.** One read,
  `GET /api/models/agents/<backend>/models/candidates`, returns
  `{candidates: {builtin: Candidate[], providers: Candidate[], in_list: Candidate[]}}` where `Candidate` is
  `{id, display_name, reasoning_efforts, suppliers: [{source_id, source_name, model_id}],
  origin, group_if_removed?: "builtin" | "providers" | null}` — `group_if_removed` is set on
  `in_list` candidates only and names where the server would offer the id if it left the menu
  (in the current built-in snapshot → `builtin`; else supplied → `providers`; else `null`), so
  the client never infers current availability from `origin`. `builtin` is the merged remote + bundled + local-CLI snapshot for the backend
  (served regardless of Gateway/Direct mode; empty for OpenCode) minus menu ids, removed ones
  included, and filtered by the backend's admission rule — configured or cache-only values the
  backend would not accept as menu ids (an `ANTHROPIC_MODEL` override, an over-long Codex id)
  are neither candidates nor reconcile input; `providers` is the deduplicated (question 1) inventory of the backend's ordered,
  configuration-eligible Sources minus menu ids and minus ids already in `builtin`, and minus
  any id the backend's admission rule would reject (length, canonical form, Claude prefix,
  OpenCode `vendor/model`) — a candidate is by definition addable; `in_list`
  is every current menu row with the same projection, so an already-added model is found by
  its supplier's name too and its disabled row shows authoritative chips.
  `suppliers` is the server's own `matching-v1` projection for that id — so a Claude alias hop
  appears here exactly as C1 would seed it — in Source order, possibly empty. No client
  re-implements matching, aliasing, eligibility, or snapshot merging. The read is member-tier
  (it names Sources); `Add models` is offered only to roles that may read Sources, exactly as
  the page already gates its Source surfaces. The picker filters both
  arrays by the query on id, display name, and supplier name, rendering `in_list` matches as
  the disabled `Already in the list` group.
- **C6 — Removed ids never return on their own.** The backend catalog persists
  `agents.<backend>.removed_model_ids: string[]`. Every row the user removes leaves its id in
  the set, whatever the row's origin was and whether or not the backend lists the id right now;
  adding a row with that id by any path clears it. Reconcile is a controller-owned mutation
  under the same lock every catalog write takes, comparing the snapshot's generation with the
  last one reconciled inside that critical section. It runs at four moments and nowhere else:
  controller startup; completion of a controller-side snapshot refresh; a controller periodic
  tick (five minutes) that re-reads the snapshot inputs — the catch-all for refreshes another
  process performed, so no IPC is needed and arrival lags a refresh by at most one tick; and,
  as the one read that is defined as refresh-then-project (precedent: the agents read's
  `refresh_cli_presence`), the picker's candidates read, which first runs that same locked
  reconcile when the generation changed and then projects. Every other read is pure. Reconcile
  tasks are tracked and gated on shutdown: Model Hub stop cancels or joins them before the
  service stops, and a completion arriving during shutdown schedules nothing. It adds each snapshot id that is neither in the menu nor in the set,
  at the position question 9 defines, seeds it per C1 and C2, and invalidates the backend's
  runtime projection; it never removes a row, and it never persists or raises while the config
  store is not writable (recovery mode serves the stored configuration unchanged). A partial
  snapshot (remote cache or CLI cache not yet read) only means fewer built-ins are known at
  that moment; nothing is ever tombstoned by inference, so no completeness gate exists. The
  snapshot's CLI input is read whenever the controller's executable presence probe finds the
  CLI, independent of `agents.<backend>.enabled`. Remote-catalog cache state (payload,
  validators, last success, last failure/backoff) is keyed by the catalog `source_key`; a
  record for another key is never read for the current one. A catalog file written before
  this field existed loads with an empty set — the v1 shape shipped in no release, so there is
  no legacy initialization, marker, or pending window. Claude Code's locked `default` row is
  not a built-in candidate and cannot be removed. The picker's `builtin` group lists removed
  built-ins so they are one click away.
- **C7 — models.dev read serves the editor typeahead, additively.** The existing
  `GET /api/models/catalog/models-dev?query=` keeps its shipped shape — `matches`, `provider_id`,
  `provider_name`, and the metadata fields — and gains: deduplication by model id, ranking
  (first-party provider first, then exact-id, then name matches), `first_party: boolean`, and a
  cap of 8. `first_party` is derived from a repo-owned, versioned vendor map
  (`vibe/data/model_vendors.json`: model-id family prefix → models.dev provider id, plus an
  ordered aggregator list): a provider is first-party exactly when the map names it; when none
  is, the first listed aggregator wins and unknown providers rank after it alphabetically. The
  map is the closed rule; extending it is a tested data change. For OpenCode the filled id is
  `<provider>/<model>` where `<provider>` is the selected match's `provider_id` passed through
  the existing OpenCode vendor normalization (§4.8) — a standard vendor keeps its id, anything
  else becomes `custom/`.
- **C5 — Unchanged.** Source inventories, Source-side manual add, Route dialog, Source order,
  direct/gateway modes, and every runtime projection are byte-for-byte unchanged except
  through C1–C4 and C6.

### Contract artifact changes (complete; the contracts lane's checklist)

Every shape C1–C7 introduces maps to exactly one row here; a delta that needs an artifact not
listed is a spec defect to report, not a lane decision. The closure property is normative: the
contract guard, the response guard, authorization coverage, and version closure all pass with
the v2 shapes on the same head.

| Artifact | Change |
| --- | --- |
| `docs/plans/model-hub-contracts/backend-model.schema.json` and its TypeScript mirror (`ui/src/components/settings/models/types.ts`) | `origin` enum gains `provider`; the mirror also gains the `Candidate` type, `first_party` on the models.dev match, and the guarded / `expected_suppliers` fields on the models `PUT`, with the API client's response and error projections (`modelsApi.ts`) |
| `PUT /api/models/agents/opencode/menu` (route, `api.md` row, response-registry entry, `set_opencode_menu`, tests) | retired — no UI consumer since #1814; the models `PUT` is the single catalog mutation, so C1/C3/C6 cannot be bypassed |
| `docs/plans/model-hub-contracts/api.md` route table | `PUT /api/models/agents/<backend>/models` row: optional `force`, `would_remove_hops`, `would_interrupt`, `expected_suppliers`; refusals `backend_model_in_route`, `candidate_suppliers_changed`; success `{agent}` or `{agent, removed_hops, interrupted}`. New row `GET /api/models/agents/<backend>/models/candidates`. `GET /api/models/catalog/models-dev` row: additive `first_party`, dedupe, ranking, cap 8 |
| `docs/plans/model-hub-contracts/api-response.schema.json` | the models `PUT` accepts the forced-success shape beside `AgentResponse`; new candidates response (`Candidate` shape from C4); models-dev response gains `first_party` |
| `docs/plans/model-hub-contracts/guard-refusal.schema.json` | `error` enum gains `backend_model_in_route`; a `detail` property; the nonempty-`would_remove_hops` relation extends to `backend_model_in_route` |
| `docs/plans/model-hub-contracts/api-response.schema.json` (stale-candidate refusal) | new registered shape for `409 candidate_suppliers_changed` — `{ok, contract_version, error, detail, changed}` — outside the guard-refusal `anyOf` |
| `contract_version` closure (every registered location listed in `docs/plans/model-hub-contracts/README.md` "Version closure", server and UI mirrors alike) | 6 → 7, because v1 shipped closed shapes (`additionalProperties: false`) that v2 extends |
| `vibe/authorization.py` role table and its coverage test (`tests/test_instance_authorization.py`) | `GET /api/models/agents/<backend>/models/candidates` is **member-tier**, the same boundary as Source inventory reads, because it carries supplier ids and names; the editor-tier catalog read stays supplier-free |
| `docs/plans/model-hub-contracts/README.md` (product model) and `docs/plans/model-hub-contracts/opencode-overlay.md` | Add Source is no longer the sole matching/placement point: a menu-model add (C1) and a reconcile add (C6) are the other one-time points; runtime still never re-matches |
| `docs/plans/model-hub-contracts/mirror-registry.json` | registers the two new error codes, the new `origin` value, and the new response shapes with their consumers |
| `vibe/i18n/*.json` and `ui/src/components/settings/models/serverCopy.ts` | `detail` keys for the two new error codes |
| `config/v2_config.py` persisted shape | `agents.<backend>.removed_model_ids` (C6); a file without the field loads with an empty set (one fixture) |
| `vibe/data/model_vendors.json` (new, versioned) | the vendor map and aggregator order C7 defines, covered by a test |
| `docs/plans/model-hub.md` §4.2 / §4.6 and its **Guard error-plan relation** table (authoritative per `mirror-registry.json` D18) | matching-point wording (C1); removed-id and reconcile rules (C6); a new relation row for `backend_model_in_route` requiring a nonempty `would_remove_hops` plan |
| `config/v2_config.py` → `AgentSupply` projection (`_agent_payload` / `to_payload` split) and the response guard | `removed_model_ids` is persisted only and never appears in any `AgentSupply` response |

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
  picking it restores the row. A built-in id that enters the backend snapshot after the menu
  was saved is in the menu after the next reconcile trigger — startup, a snapshot refresh
  completing, the periodic tick, or the picker's candidates read — unless a row with that id
  was removed before; an ordinary read reconciles nothing and observes whichever of those ran
  last. An id the user removed never returns on its own, whatever the removed row's origin
  was.
- The picker lists each candidate id exactly once across all groups.
- A field the user cleared before the first Save is empty after Save.
- Every supplier chip the picker shows for a candidate equals the hop set C1 seeds when that
  candidate is added (the server projects both from the same rule), or the add is refused and
  re-asked with the current chips.
- Two callers adding the same id concurrently leave exactly one row and one route, matched once.
- A built-in removed while the backend had withdrawn it does not return when the backend
  lists it again.
- No Model Hub read or mutation fails or writes while the config store is in recovery mode.
- Reads never write, with the candidates read as the single defined exception: any other read
  observed concurrently with a mutation leaves the stored configuration byte-identical to what
  that mutation alone would have produced, and the candidates read's reconcile is serialized
  by the mutation lock like every write.
- While the config store is writable, a built-in that enters the snapshot through any
  process's refresh is in the menu within one controller tick, and immediately when the
  picker's candidates read runs. Recovery mode is the same guarantee under its own
  precondition, not an exception to it: reconcile changes nothing, so the menu holds its
  last reconciled state until the store is writable again, and the bound resumes there.
- No reconcile task outlives Model Hub shutdown.
- Every producer shares one admission predicate, and it is stricter than the persisted shape
  alone: a proposal is admissible exactly when the backend's identity policy admits the id —
  it equals its own canonical form, an OpenCode id is a valid menu identity, and a Claude id
  is either a current built-in or `claude-`/`anthropic-` prefixed — and
  `ModelHubBackendModelConfig.from_payload` then accepts the row it would create, with the id
  it persists unchanged. The validator alone is the weaker half: it accepts ids that the
  identity policy, and therefore the shipped API, refuses.
- A catalog file without `removed_model_ids` loads with an empty set and reconciles normally.
- In the editor, a models.dev suggestion fills every metadata field it knows and leaves each
  one editable; an id models.dev does not know can still be entered and saved.

### Delivery notes

- PR #1829 (`fix(model-hub): reveal newly saved models`) stayed valid independently of this
  redesign — the picker saves through the same `PUT` and the card still collapses at six
  rows — and was merged on its own gates (`aa6d4d99e`). The v2 lanes base on master after it.
- Design PR avibe-docs #34 carries the earlier "Batch Add / Agent Model Picker" proposal
  frames; v2 frames are drawn on that branch with sparse copy and the proposal frames are
  renamed `Superseded —` rather than deleted.

### Delivery record

v2 shipped in four lanes off this spec: #1830 published it as the normative contract, #1837
built the server side (candidates read, `removed_model_ids` with its reconcile, the guard
plan the client echoes), #1839 built the UI (picker groups, the id and row chokepoints, the
guard re-ask), and #1843 follows up with a distinct close label for the catalog dialog and
the e2e allow-list for tier provenance — open at the time of writing, so it is the one lane
this record does not describe as merged. The frames are avibe-docs #34 (v2 frames drawn over
the superseded proposal set) and #35 (picker search grouping in-list matches). Two
design-fidelity deltas found by #1839's render are ruled Known-by-design there and await an
owner decision, because both belong to surfaces wider than this feature: `.model-hub-pill`
renders 23px tall against the frame's 19px chip, which stacks a picker row to 60px where
frame B‴ measures 52 — retuning the pill would move every pill in the Model Hub — and the
narrow-viewport footer orders its two buttons the other way, which is the shared `Dialog`
primitive's own narrow layout. Neither hides nor clips anything.
