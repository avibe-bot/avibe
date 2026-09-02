# Backend Model Catalogs

Status: implementation contract

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
