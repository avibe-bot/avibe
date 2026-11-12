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
- Pending Sources use neutral Saved / 已保存 presentation, not a warning or
  health claim. Error, needs-action and cooldown states retain precedence. The
  accessible connection hint describes saved configuration and successful-call
  status updates without promising a Test entry for subscriptions/native Sources.
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
- The deadline bounds local preparation and admission as well as invocation.
  Expiration before adapter admission is a request/engine error, not evidence
  against the selected model. Reported latency covers admission through the
  outcome, excluding preparation, cleanup, metering and verification writes.
  Rejected API keys have credential-specific copy without refresh or health
  mutation. Deadline handling preserves the supported Python 3.10 runtime.
- Displayed results belong to the current effective selection and credential
  identity. Inventory-driven selection changes clear completed results and
  invalidate late results; unrelated inventory updates preserve them.
  Publish a result only after the mutation owner reconciles a current matching
  Source/model. Rejected, unread or stale reconciliation produces an unconfirmed
  status, never a model verdict or an automatic second read.
- A known invocation outcome owns finite verification, cleanup and metering
  settlement through caller cancellation. Verify the matching credential before
  cleanup, drain settlement exactly once, then propagate cancellation to the
  caller. Cancellation before a known success cannot verify a credential.
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

The owner amendment at 12:33 on 2026-09-08 supersedes the advanced-only tier
editor: remove reasoning levels, provenance badges and their editor from the
provider inventory entirely. The now-empty Advanced settings toggle is removed
too. Saved capability metadata and compatibility APIs remain available to their
other consumers; this UI change does not rewrite them. See
[Reasoning intent](model-hub-reasoning-intent.md) for the forwarding boundary.

## Background format detection amendment

Owner decision: 2026-11-12. A custom endpoint's API format is detected in the
background, inside the save-first contract above:

- Once Base URL and key are filled (an absolute http(s) address, after a short
  debounce), the dialog calls the existing non-persisting observation route
  (`POST /api/models/sources/observe`, Auto detect for `custom`) from the
  2026-09-03 detect-then-confirm design. It probes the three selectable formats
  against the typed address with the typed key only; no third-party lookup.
- A proved format is preselected with a short "Recognised as …" note. When
  nothing proves one, OpenAI Chat Completions stays selected with a note saying
  so (and that the key was refused, when the endpoint said that).
- Detection never gates saving: Save sends the format the field shows at the
  moment it is pressed, a pending probe included. A format the user picks is
  never moved by a later result; editing Base URL or key re-detects, and a
  catalog vendor, whose format is pinned, is never probed.
- An unnamed custom Source is seeded with its Base URL host (`api.example.com`,
  with a non-default port) instead of `custom`; the name field shows it as the
  placeholder. Catalog vendors keep their label.
