# Save-first API-key providers and explicit model tests

Owner decision: 2026-09-08. This supersedes the add-dialog observation gate,
not subscription OAuth admission or credential replacement.

## Behavior contract

- Saving a valid provider configuration does not require upstream evidence.
  The dialog supplies the catalog protocol or the user's concrete protocol
  selection (custom defaults visibly to OpenAI Chat Completions).
- The save-first create path attempts discovery once with the provisioned
  credential. Unavailable or invalid inventory cannot reject the save, erase
  manual entries, or establish invocation success. Discovery occurs before the
  new Source's single commit, so no existing-inventory destructive guard applies.
- `verification_pending` remains until a real invocation succeeds for that
  credential identity. Existing explicit observation APIs remain available to
  older callers; the dialog does not call them.
- Test is a separate, explicitly initiated action on a saved API-key Source.
  It offers all non-retired discovered or manual models without reordering the
  inventory. A centrally maintained ordered preference list selects the first
  exact ID present; absent any match, select the first offered model. An existing
  valid user selection wins over subsequent list updates.
- An empty inventory does not disable saving. The existing manual-model editor
  supplies IDs when discovery is unavailable; tests accept only a currently
  present, non-retired model.
- `POST /api/models/sources/<id>/probe` accepts exactly `{model: string}`.
  It invokes that Source/model through the managed invocation adapter, using
  the Source's protocol. It never resolves an Agent route, falls back, changes
  provider health, or adds models. Failure describes this test, not the whole
  provider. Success clears only the matching pending-verification identity.
- Explicit testing is runtime demand and may start the managed engine through
  the existing adapter. It does not change the saved runtime-enabled intent or
  the Agent supply configuration.
- Results contain Source ID, model ID, protocol, success, latency, and a
  credential-free error key. Usage is metered by the existing Source/model
  ledger. The test has a finite deadline and output budget and can incur a small
  upstream charge.
- Source deletion/credential replacement during a test cannot resurrect a
  Source, overwrite current configuration, or verify a replacement credential.

## Evidence

Unit and HTTP/RPC contract tests cover save independence, inventory ordering,
exact-source invocation, error isolation, and credential-identity settlement.
UI tests cover default selection, optional testing, and asynchronous lifetime.
No live credentials or running user instance are used for development.

## Inventory presentation amendment

Owner follow-up: 2026-09-08. Manual model addition performs no upstream
discovery. Its loading indicator belongs to the add operation; only explicit
refetch can animate the refetch button. Post-write local projection reads are
not upstream model fetching.

Reasoning levels are capability declarations consumed by managed model
registration, not the effort setting for a conversation. Keep the data and
editor, but show them only after opening Advanced settings. The default
inventory shows model identities and ordinary model actions. In the advanced
view, "Avibe preset levels" identifies capability metadata provenance without
implying that a discovered model itself came from a built-in inventory.
