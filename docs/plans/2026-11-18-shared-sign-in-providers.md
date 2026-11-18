# Google, GitHub and Apple in both apps: reuse the backend's Supabase route

Status: implementation contract check, 2026-11-18 (#3946). It replaces
`2026-11-18-shared-sign-in-exchange.md`. Backend facts come from the
verification report of 17:30: avibe-backend `884a13c`, plus #370's body.

**Update 20:50 (main), after #3945 merged as `1e0851472`:** the iPhone keeps
#3945's route as merged: the backend mobile broker (`/api/mobile/auth/*`),
its `mobile_providers` display gate, and its `new_account` hint from
`Avibe-Account-Created`. The iPhone parts below that describe the Supabase
route, `auth.signInMethods` and the iPhone session-read split are superseded
and not shipped. The Desktop keeps this document's route. Both apps hand the
shared page the same shape, `{providers, passkey}`; the iPhone reads it once
per signed-out session answer (#3945's `providers`, plus `passkey`), and an
answer without it offers email alone. As of 21:00 the broker is backend #376
(open): production serves no `mobile_providers`, `Avibe-Account-Created` is
never produced, and Apple is not implemented there, so the iPhone shows email
(and passkey sign-in when switched on) until it ships; nothing is guessed.

## The route that exists

1. The client gets Supabase tokens.
   - The email code and password already do this on both apps.
   - #370 states that Desktop provider sign-in uses the same route.
2. The tokens go to an existing endpoint, which issues the Avibe cookies:
   - iPhone: `POST /auth/callback`;
   - Desktop: `POST /api/desktop/session`.
3. `GET /api/mobile/session` then decides between signed in, the two-step gate and signed out.

Provider sign-in only adds step 1 for a provider, using Supabase PKCE:
- authorize with `code_challenge`;
- the callback returns `code`;
- `POST /auth/v1/token?grant_type=pkce {auth_code, code_verifier}`.

Apple native uses `grant_type=id_token` with the raw nonce instead.

No new backend route, transaction store or page is needed.

## Layer by layer

| Layer | Changes? | Notes |
| --- | --- | --- |
| Shared flow `ui/src/sign-in` | Small | `signInWithProvider(provider)` → next step, or a closed error. The route is invisible here. The shell passes the ways in it read (`methods`). |
| iPhone JS adapter | Small | The same `auth.signInWithProvider({provider})` bridge method. The page reads `auth.signInMethods` each time the sign-in screen shows and offers only what it names. |
| Desktop JS adapter | Small | A new `sign_in_with_provider{provider}` action. The providers on offer come from native, which reads the public `google/github/apple_login_enabled` fields the web uses. |
| iPhone native, reusable as is | — | From #3945: <ul><li>verifier and state secrets with S256</li><li>constant-time state check</li><li>`ASWebAuthenticationSession` runner with cancel on a generation change</li><li>Apple sheet with email scope, hashed nonce and state check</li><li>coordinator transition (generation bump, fresh store, rotate on failure, drop late results)</li><li>closed bridge validation</li><li>session read and two-step gate</li></ul> |
| iPhone native, to adapt (transport only) | Yes | <ul><li>Start builds the Supabase authorize URL; no server call.</li><li>The browser allow-check names that URL.</li><li>The callback grammar takes Supabase's `error`, `error_code` and `error_description`.</li><li>Exchange is the PKCE token grant, then `POST /auth/callback`, as the email code does today.</li><li>Apple uses the `id_token` grant.</li></ul> |
| Desktop native (Rust) | Add | <ul><li>PKCE, the authorize URL, callback capture, and the PKCE token grant.</li><li>Then the existing `establish()` → `/api/desktop/session` → discovery and two-step gate.</li><li>Tokens stay in Rust. The cookie jar stays in the keychain.</li><li>Every account call is fenced by the existing epoch; the public switch read is not (below).</li></ul> |

## Callback and configuration (decided 17:40; GoTrue v2.198.0 read)

- **Desktop, macOS and Windows alike: loopback.** Native binds `127.0.0.1:0`
  before the browser opens and sends `redirect_to=http://127.0.0.1:<port>/callback?state=…`.
  GoTrue's `IsRedirectURLValid` accepts a loopback IP on any port without the
  allow-list (`request.go`, RFC 8252 §7.3), so no hosted allow-list entry or
  custom scheme is needed. An `EXTERNAL_REDIRECT_URL` override would send the
  browser elsewhere; the attempt then times out or is aborted (fail closed).
  Implemented in `runtime-host/src/instance_client/loopback.rs`.
- **iPhone: https on the control plane.** `redirect_to=https://avibe.bot/mobile/auth/complete?state=…`
  passes GoTrue's same-host rule when the hosted SITE_URL is `https://avibe.bot`
  (not yet read back). `ASWebAuthenticationSession`'s https callback captures
  it (iOS 17.4, `webcredentials:avibe.bot`); avibe.bot serves nothing there.
  A signed build on a device is the only proof.
- **Callback grammar.** Success adds `code` to our query; failure adds `error`,
  `error_description` and (for API errors) `error_code`, and copies them into
  the fragment with an empty `sb`. `error_code=provider_email_needs_verification`
  is the trusted unverified-email signal.
- **Apple.** Production reports Apple off. Desktop uses Supabase's web Apple;
  the iPhone's `id_token` grant needs the bundle ID among Supabase's Apple
  client IDs. Both are production configuration, not done here.

## Which ways in each app offers (19:08 and 19:09 rulings)

Both apps read avibe.bot's public `/api/config/auth`, the fields its own
login card reads, and offer a way in only when its switch is exactly `true`:
`<provider>_login_enabled` for Google, GitHub and Apple, and
`passkey_login_enabled` for passkey sign-in. Before the read answers, or when
it fails, a field is missing or not `true`, only email shows. The start of a
provider sign-in, and avibe.bot's 503 for passkey sign-in, still refuse a way
that is off.

Passkey sign-in also needs the system's passkey sheet:

| App | Offered, and run, when |
| --- | --- |
| iPhone | Always with the switch: the 17.4 floor and the `webcredentials:avibe.bot` association. (Its providers come from `mobile_providers`, see the update above.) |
| Desktop, macOS | macOS 13 or later (passkeys ship there; 12 has the classes as a developer preview) and the running signature's associated domains name `webcredentials:avibe.bot` (the Developer ID build; local and ad-hoc builds lack it). |
| Desktop, Windows | webauthn.dll at API version 1, which serves the assertion options the shell sends. |
| Desktop, other | Never. |

Where it is not offered, Desktop native also refuses it before any request,
and its system assertion refuses before any AuthenticationServices class is
touched. The same gate applies to the second factor: native drops passkey
from the published second-factor methods, so the page starts at the
authenticator code, and a passkey verify requests no challenge. Not run here:
macOS 12 or older, Windows, a signed build.

## "Use a different account" on the Desktop

As on the iPhone, it signs the unfinished account out and returns to the
first step; the window stays. An account restored at launch that still owes
its second factor is forgotten: its record first, then its keychain entry,
through the same credential owner sign-out uses, so a restart does not bring
it back. Nothing is revoked on avibe.bot or on other devices. If the keychain
entry cannot be erased, the page says so; the record no longer names the
account, so no later sign-in is written under its key, and a sign-out erases
the entry later.

Another sign-in after "continue on this computer" from such an account: while
it only owes its own second factor, nothing is written and the stored account
stays for a later launch. Once a different account signs in, its record is
saved first and then the stored account's key is erased, under the same
credential owner. If that erase fails, the page says so, the old key stays
owned for a sign-out to erase, and the new account is never written under it.
An unfinished sign-in is written only under the stored account its flow
continues.

The switches are public data, not the account's:
- Desktop reads them without the account's epoch, so restore, local binding,
  abort or a first factor do not drop them; reads show in the order they
  started; an account commit or rollback keeps them.
- iPhone reads them once per signed-out session answer, bounded at 2 s, with
  no cookie; the read changes no account and rotates no store.

## The provider session read

(Superseded for the iPhone by the 20:50 update; kept for the record.) On the iPhone, after `/auth/callback` sets the cookies, the session read is a
request of its own with its own deadline (the Desktop's grant and session
calls were already separate requests). Its classification is the session
read's: `401` the sign-in did not hold, `403 two_factor_required` the second
factor, anything else `network`. Known limit, shared with the email code and
the password: when the callback set the cookies but the read fails, the
attempt's store is still retired. The person goes through the browser again,
and avibe.bot may keep a session no one holds until it expires.

## Signals with no trusted source on this route

| Signal | What happens |
| --- | --- |
| iPhone `new_account` hint | On #3945's broker route the App shows it when `Avibe-Account-Created` is present (20:50). The broker (backend #376, open) does not produce it yet, so it does not show; nothing is inferred from email or an empty instance list. The Desktop has no such signal and shows no such hint. |
| `provider_email_unverified` | Trusted: GoTrue's `error_code=provider_email_needs_verification` in the callback. Kept on both apps. |

## Preserved on any route

- Tokens, verifier, state and nonce stay native.
- State is compared in constant time.
- Apple: hashed nonce to Apple, raw nonce to Supabase, state checked.
- An account transition cancels the window and drops late results; a failed attempt leaves a fresh signed-out store.
- The two-step gate still runs after the cookie.
- Account linking is the hosted project's existing behavior and is not changed.
- The native authorization chain gets an independent security review before release.
