# Model Hub source identity

## Change contract

- When a new subscription would receive an existing vendor default name,
  allocate the first available numbered name (`OpenAI`, `OpenAI 2`, …) under
  the existing source mutation lock. Carry whether the create request defaulted
  the name into the commit path; preserve explicit names even when they equal
  the vendor default. Preserve existing names, source IDs, routes, and OAuth
  retry idempotency.
- Reuse `Source.account_label` for optional subscription display metadata.
  Hub metadata must come from the credential bound to that exact source,
  vendor, and engine prefix, with email preferred over a provider username.
  Read existing subscriptions as well as new ones, without another login,
  engine startup, upstream request, or credential export.
- Missing, malformed, or unreadable display metadata means no account label;
  it does not prevent source listing or imply authentication failure. Never
  substitute another source's identity or a credential/account ID.
  Reject non-UTF-8-encodable labels and compare labels and stored tokens after
  the same whitespace normalization.
- Show the account on its own line in source cards and details. A single eye
  control in the source-list header hides account identities, endpoint URLs,
  and masked API keys together. Source details expose the same shared control
  in their header; do not add per-source or per-field eye buttons. Persist only
  the browser preference. Hidden metadata must be absent from DOM text,
  accessible labels, and hover titles, including endpoint-derived protocol
  prefixes in source details. Explicit editing forms remain editable.
  Keep each source row as one full-card opener and graph endpoint, so desktop
  wires retain the card's right-edge midpoint. The global eye must not open
  a source or highlight one source's wires.
  Let native button naming expose the rendered kind, protocol, state and
  visible metadata; do not replace that content with a name-only ARIA label.
- Order source-card badges as kind then protocol. API key is cyan, subscription
  retains its semantic accent, and protocol is neutral with no vendor/domain
  prefix. Endpoint details remain available separately.

## Validation

Use hermetic tests for allocation, OAuth replay, source-bound metadata reads,
provider shapes, missing/unsafe metadata, account switching, and privacy.
Run the config/contract suite, including byte-identical adapter-interface
mirrors and live-file authority closure, whenever changing the adapter boundary.
Exercise the React consumers and browser fixture in English and Chinese,
desktop and mobile, light and dark. Run the UI build and changed-file lint.
GitHub CI and the exact-head Codex review remain delivery gates.

The design editor is unavailable in this session. Reuse the established
Model Hub tokens and the supplied screenshot; compare actual browser renders.
Real-provider OAuth and local Incus acceptance remain separate from fixtures.

## Pinned provider evidence

CLIProxyAPI `v7.2.149` (`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`)
writes a top-level `email` for Codex, Claude, Antigravity, and xAI. Its Kimi
token storage has no account email or username: `device_id` is not an account
identity and remains undisplayed. The reader also accepts an explicit
`username` supplied through the engine's flattened metadata hooks.

These are presentation fields only. Account IDs, subject IDs, organization
IDs, project IDs, device IDs, token claims, and filenames are not fallbacks.
The existing source contract and persisted config schema are unchanged.
