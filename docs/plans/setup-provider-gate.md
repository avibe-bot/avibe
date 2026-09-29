# Require a provider before assistant setup

## Problem and contract

An empty provider inventory could advance to assistant setup through the footer
hint or a declined migration. Assistant entry then required a model connection
that none of its controls could establish.

- The provider screen can continue only after its source read confirms an
  `active` or `standby` source, using the existing source policy.
- Without a source, the primary action opens the subscription/API-key dialog.
  Declining migration preserves the dismissal and keeps the Add action.
- A detected key is an import offer, not a connected source. Reading failures
  retain Retry; pending writes and readbacks cannot advance.
- If a current source read on the assistant screen finds no usable source,
  its blocked-entry hint offers Add model source and returns to providers.

## Scope and validation

Reuse the existing provider action state machine and setup navigation. Remove
the bypass rather than adding another navigation or persistence mechanism.
Backend entry readiness, migration consent, source validation and completion
remain owned by their existing flows.

AUTH-SETUP-126 covers the provider component, registered Wizard, assistant
recovery and a browser journey in English and Chinese with controlled network
responses. AUTH-SETUP-111 also checks the real source API's empty/created
inventory readback. These layers do not establish packaged desktop acceptance
against a live instance.

Required checks: affected component files, source API scenario harness, browser
provider/assistant journeys, UI lint, TypeScript checks and production build.
