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

Native menu/deep-link presentation dispatches `avibe:desktop-present` through
that document's unsaved-work guard. Its optional `presentationToken` must be
forwarded unchanged across asynchronous approval as
`avibe://desktop/<view>?native_request=<token>` (including `updates`). It is a
one-intent presentation receipt, not updater authority: native validates the
original owner, view, receiver and delivery attempt again at enqueue. Stale,
replayed or superseded receipts have no uncorrelated fallback. A plain footer
click keeps its existing uncorrelated presentation URL and source checks.

Runtime projection excludes catalog, enrollment transaction, second-factor
inputs and consent details. It includes account identity, connection, local
status, Desktop update facts, revision/generation and auxiliary limitation.

## A4: account feedback

`local.instance_id` and `local.remote_access_url` are nullable current local
descriptor facts. Older missing fields mean null. Only a validated local
pairing supplies the ID; only remote access On supplies its URL. Unpaired or
failed/unreadable proof clears both without changing enrollment ownership.

The trusted `sign_in_password` action carries transient `email` and `password`.
Native validates its provider configuration, requests the password grant, and
uses the existing Desktop session/cookie and second-factor owner. A replacement
first factor retires the preceding OTP/MFA attempt even when the new attempt
fails. Passwords and provider tokens never enter state or events.

Presentation and owned account URLs accept one optional `locale=en|zh`;
unknown, duplicate and extra parameters remain invalid. Workbench captures
the locale with its guard, and bundled mounting applies it before rendering.

An admitted sign-out presentation requests direct logout. A bare
`?view=sign-out` does not authorize it. Native binds a one-use opaque intent to
the admitted source, owner, generation, physical window and document.
`get_state` may return the same unspent `{type:"sign_out_intent", intent_id,
generation}` direct result repeatedly in that document. Automatic logout
sends `sign_out {intent_id}`; native consumes the receipt once through the
existing cleanup owner. Reload, replacement, cancellation or supersession
invalidates it. Ordinary explicit trusted controls retain tokenless sign-out.
Neither the intent nor direct reply is persisted or broadcast. A receipt
binds execution, not proof of a human click.

See `docs/plans/desktop-rc19-account-feedback.md` for the owning tests and
acceptance boundaries. Synthetic fixture values authorize no real account.
