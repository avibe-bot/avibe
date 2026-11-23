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

The five `instance_update_*` actions act on the committed remote instance, not
this computer: `refresh` reads its `/api/session` and `/api/version`; `check`,
`set_prereleases`, `set_auto_update` and `install` send that instance's own
update requests with the sign-in the app made when it opened the instance
(kept in memory, never persisted), its `Origin` and CSRF double-submit. Only
the bundled account page and the update popup may call them, never a
Workbench. `install` requires the exact `snapshot.latest` on display.
`state.instance_update` (null while the committed connection is local) is
display data rebuilt from allow-listed fields with validated versions,
enumerated phases and error codes; it is in the full state only, never the
Runtime projection. Its `request` is the last action this shell sent. A
standalone install answers when it ends; the shell follows `install` and
`current` in later reads. `request.following` is exactly when the shell
refuses another action for the instance because of that request (on its way,
or an install still followed); it is republished when time alone ends it.
`request.installed_from` is the version an install started from (null for
other actions). A standalone Runtime still answering on that version, with no
`install` report of its own, five minutes after accepting is no longer
followed.

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

## A5: retained Workbench and display metadata

The physical main window contains one committed Runtime WebView and, during
account/list presentation, one separately admitted bundled account WebView.
Back and native-owned successful login reveal the same retained document.
Initial/restored state, errors and new recovery codes do not trigger return.
Explicit recovery acknowledgement uses the existing trusted Cancel action.

`local.slug` is nullable in full state and the minimal projection. Its only
producer is the signed-in catalog's authoritative name joined to validated
current local ID. Full-state-only `current_instance` reuses `Instance`, bound
to the current remote ID, validated origin and account. It may retain the
successful selection's display snapshot when a later catalog omits it. Neither
field authorizes a connection or changes persisted schema. Old missing fields
decode as null; new producers emit them. Unknown relationship remains unknown.

Account capability selects only `main-account-*` WebViews, plus Rust ownership,
document and exact bundled URL checks. Physical-window selectors grant no
account authority. Reload retires account admission and one-use receipts.
Full DOM events check their URL and ephemeral document nonce at execution;
retained Runtime views receive only minimal state.

A candidate loads in a separate hidden store before terminal retirement.
Native erase/close/show and credential/record persistence are awaited before
role publication and successful-only MRU. A preterminal failure retains the
source. Once retirement begins, failure uses visible trusted recovery and
cannot revive the old cookies/document or publish success. OS cleanup refusal
remains a blocking obligation until explicit cleanup succeeds.
