# Desktop instance client v1 fixtures

These fixtures mirror the accepted Phase 1 envelope and the existing backend
field names. Values are synthetic; they authorize no live account or endpoint.
They include non-ASCII device and instance names. They are shared input for
native, UI and backend consumers, not an independent protocol or a behavioral
pass.

- `requests.json`: every enumerated bundled-page action. The Tauri call is
  `invoke('desktop_client_request', { request: envelope })`.
- `states.json`: nonsecret full bundled-page projections.
- `replies.json`: success, generation rejection, pending and direct TOTP results.
  TOTP seed/QR/recovery codes belong only to their direct trusted reply. The
  backend enrollment token is deliberately absent from all fixtures presented
  to JavaScript; native retains it privately.
- `transport.json`: account, local preparation and enrollment boundary shapes.
  Backend redemption pending is HTTP 409; Runtime pair pending is HTTP 400 with
  explicit backend `status:409`. Consumers recognize the error and bounded
  integer delay, never bare HTTP status. All responses are no-store.

The local mutation header is `X-Vibe-CSRF-Token`, obtained from `csrf_token`
and the `vibe_csrf_token` cookie with the exact local Origin. Bundled entry is
`account/index.html?view=account|instances|setup-complete|sign-out|updates`; `view`
is presentation only. Mutations are native-generation fenced. The four
`update_*` actions use only the local shell updater, even when a remote Runtime
is selected; they neither use its session nor persist an account transaction.
`update_install` requires the exact displayed `target_version`. The updater
publishes progress through `state.desktop.update`; an update reply cannot
replace that live projection with a saved account-state clone.

Runtime projection excludes catalog, enrollment transaction, second-factor
inputs and consent details. It includes account identity, connection, local
status, Desktop update facts, revision/generation and auxiliary limitation.
