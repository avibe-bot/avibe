# Desktop Phase 1: native account and client selection

## Change contract

Keep RuntimeHost and LoopbackOrigin local. Add one native account and client
selection coordinator, reached only through the enumerated main-window bundled
account page. Runtime documents receive a nonsecret projection and presentation
actions. Account cookies and enrollment/OIDC material remain native; TOTP
seed/QR/recovery display uses direct trusted replies only.

A target is resolved by ID from a fresh authorized catalog. Require canonical
HTTPS and current matching remote session identity before projecting host-scoped
cookies to a fresh disposable WebView store. All asynchronous completions are
fenced by account/selection generation. Local lifecycle continues while remote
is selected. Sign-out erases native credentials and remote stores even offline,
returns local, and preserves pairing and remote-access intent.

## Validation boundaries

The first native probe establishes cookie attributes and isolation using only
test-owned stores. Then coordinator tests exercise actual HTTP producers and
shared fixtures. A reusable integration runner joins exact candidate backend,
Runtime and bundled UI artifacts without importing peer commits into this PR.

AUTH-SETUP-171/175/176/177/179 cover full authentication, selected identity,
late completion/local continuity, privileged caller isolation and explicit
remote auxiliary disable. Existing shell tests only cover a local Runtime;
these tests protect the newly introduced account and selection boundaries.

All tests use private HOME/XDG/config/credential/DB/endpoints. Signed-package
Keychain retention, passkey association/system sheet, WebView platform behavior
and hit testing remain RC acceptance. No signing, installation, merge or
service restart belongs to this change.

## Shared handoff

`desktop/contracts/instance-client-v1/` provides JSON fixtures for the approved
inner envelope, direct TOTP responses, native projection and HTTP boundaries.
The bridge's outer command argument is `request`. Compatible bundles expose
`account/index.html`; view is enumerated presentation only.

The existing updater remains owner. A trusted Phase 1 `avibe://updates`
presentation must reach its native confirmation directly, without redispatching
to the same Workbench footer. Cancellation and stale generations cannot install.
