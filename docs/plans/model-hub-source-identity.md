# Model Hub source identity

## Change contract

- When a new subscription would receive an existing vendor default name,
  allocate the first available numbered name (`OpenAI`, `OpenAI 2`, …) under
  the existing source mutation lock. Preserve existing names, explicit custom
  names, source IDs, routes, and OAuth retry idempotency.
- Reuse `Source.account_label` for optional subscription display metadata.
  Hub metadata must come from the credential bound to that exact source,
  vendor, and engine prefix, with email preferred over a provider username.
  Read existing subscriptions as well as new ones, without another login,
  engine startup, upstream request, or credential export.
- Missing, malformed, or unreadable display metadata means no account label;
  it does not prevent source listing or imply authentication failure. Never
  substitute another source's identity or a credential/account ID.
- Show the account on its own line in source cards and details. One browser
  preference controls visibility across both surfaces; persist only the
  preference. Hidden text must be absent from DOM text, accessible labels,
  and hover titles. The eye control must not open the source detail.
- Order source-card badges as kind then protocol. API key is cyan, subscription
  retains its semantic accent, and protocol is neutral with no vendor/domain
  prefix. Endpoint details remain available separately.

## Validation

Use hermetic tests for allocation, OAuth replay, source-bound metadata reads,
provider shapes, missing/unsafe metadata, account switching, and privacy.
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
