# Mobile App Onboarding

## Goal

Ship the approved `mobile-app/*` onboarding flow as a bundled React experience
inside the iOS shell:

1. Enter a full email address.
2. Request and verify a six-digit Supabase email OTP.
3. Select an owned or permitted shared Avibe.
4. Continue through the existing instance-to-control-plane OIDC and consent
   flow.
5. Restore only a selection that still belongs to the same authorized account.

Android is outside this slice. The bridge contract stays platform-neutral, but
the implementation adds no Android framework or runtime.

## Authentication Flow

The bundled page can call only a narrow account bridge installed in its own
local `WKWebView`. Instance pages, child windows, Show Pages, and iframes never
receive these handlers.

The native account client reads the public Supabase URL and anon key from
`/api/config/auth` and runs fixed Supabase `/auth/v1/otp` and `/auth/v1/verify`
requests in `WKContentWorld.defaultClient`. The returned access and refresh
tokens exist transiently in that isolated WebKit JavaScript world and are posted
directly to the existing `/auth/callback`; they are never returned to bundled or
remote page JavaScript. The callback remains responsible for Supabase
`setSession`, invitation activation, membership email synchronization, and the
`avibe_session` cookie.

`GET /api/mobile/session` is the account-scoped discovery boundary. It accepts
only the existing website cookie session and returns the account plus owned and
permitted shared instances. That includes paired active Personal and
Organization instances admitted by the existing email, domain, or
Organization-group authorization rules. Organization management roles do not
imply runtime access. The response is limited to account id/full email and
`id`, display name, host, URL, owner/shared relationship, owner email, and
availability for each instance. Runtime availability is informational; the
instance and control plane remain authoritative when the user opens an Avibe.

`DELETE /api/mobile/session` performs current-device logout with Supabase local
scope and clears the Avibe session cookie. It does not call the existing global
logout route and does not delete remembered consent grants. The native request
sets `Origin` to the configured control-plane origin so the existing
cookie-mutation guard remains strict; another origin still receives `403`.

The iOS app depends on `avibe-backend#308`, merged to backend `main` as
`1cea1dffdc3e68c6a2cc8594064bf94367485b5c` and successfully deployed to
Production by Vercel on 2026-11-05 at 18:44:16 UTC. A public no-cookie request
to the mobile session endpoint returns the expected `401`, and health returns
200. The owner separately verified production email OTP, a real account
session, and online/offline instance discovery on the iOS 27.2 simulator on
2026-11-06; automated tests remain hermetic.

## Native Bridge Contract

The bundled page receives only these account operations:

| Method | Input | Result |
| --- | --- | --- |
| `session.bootstrap` | none | signed-out or current account, instances, and valid selected instance |
| `auth.requestOtp` | full email | provider cooldown metadata |
| `auth.verifyOtp` | full email and six-digit code | current account and instances |
| `session.refresh` | none | current account and authorized instances |
| `session.selectInstance` | instance id | accepted selected instance |
| `session.logout` | none | signed-out state |
| `app.openExternal` | HTTPS URL from the closed install/help allowlist | whether iOS accepted the handoff |

The runtime `avibeNative` bridge keeps `app.showAccount` under
`accountPresentation: 1`. Capability 2 adds the primary-only display hint
`app.setAccountActionAvailable({available})`; it does not expose account
methods, tokens, or an authenticated fetch primitive. A current AppShell
registers its Web account action and receives no native bar. An older instance
Web UI receives a delayed 56-point top fallback outside the WebView, preserving
Account and Change Instance without covering Web controls.

## Data Ownership

| Field | Owner |
| --- | --- |
| Supabase access and refresh tokens during sign-in | Transient fixed script in `WKContentWorld.defaultClient` |
| Supabase and `avibe_session` cookies | Account-scoped persistent WebKit website data store |
| Account id and full email | Control-plane mobile session response |
| Instance ownership, sharing, and current authorization | Control plane |
| Instance runtime availability | Control-plane runtime status projection |
| Selected instance id and URL | App storage, scoped by account id |
| OTP input, busy state, error, and provider/client cooldown | Bundled page state |
| Session generation | Native app state |

The app never sends control-plane cookies to an instance URL or third-party
redirect. It never exposes access or refresh tokens to the bundled page.

## Cookie Synchronization

WebKit is the only persistent cookie owner. The app does not copy a complete
cookie snapshot through `URLSession`.

Each active phone account uses one persistent named `WKWebsiteDataStore`. The
native-owned hidden transport and every runtime WebView for that account share
that store. The transport runs public Supabase OTP calls, posts the resulting
session to the existing control-plane callback, and reads or deletes the mobile
session without returning access or refresh tokens to the bundled page.

The hidden transport uses one bounded-request mechanism across readiness and
all fixed scripts. Control-plane readiness has a native deadline, account
requests use an `AbortController` deadline, and current-device DELETE uses the
same mechanism with a shorter bound. Navigation actions and responses must
remain on the configured control-plane origin before any auth script can run.
Fixed requests reject redirects, so email, OTP, tokens, and control-plane
cookies cannot follow a response to another origin.

Logout or account switch increments the session generation, detaches all views
using the old store, replaces the persisted store identifier, and removes the
old store. A late request can therefore mutate only an abandoned store and
cannot restore credentials into the next account. XCTest with a hermetic server
and real `WKHTTPCookieStore` must prove:

- there is no native snapshot writer that can overwrite a newer WebKit cookie;
- chunk rotation and deletion are reflected by WebKit;
- delayed responses from an old generation remain confined to the abandoned
  store after logout or account switch;
- control-plane cookies are not sent to instance or third-party origins.

Before removal, the runtime controller releases its primary and child WebViews,
the hidden transport is invalidated and released, and the onboarding document
remains on its non-persistent store. The account store persists an ownership
ledger before creating each named WebKit store. App launch and every rotation
retry only ledger entries other than the current identifier; successful
removals leave the ledger, while failures remain for the next sweep. This
survives a crash between identifier persistence and cleanup without enumerating
or touching named stores created by another caller in the App container. No
released Avibe version created mobile account stores, so there is no legacy
orphan set to discover or migrate.

Each runtime primary WebView, popup child, SwiftUI-dismantled controller, and
hidden transport contributes a weak release barrier before rotation. Account
transitions share one aggregate one-second deadline across all barriers. If any
old WebView remains retained, the transition still creates and activates the
fresh store but skips old-store deletion for that rotation. The old UUID stays
in the ownership ledger for the next launch or rotation, when cleanup is
retried after the stale WebView is gone. A failed removal logs only the store
UUID and error; logout still completes against the fresh, detached store.

Cooldown has one page owner. A provider `rate_limited` result or retry duration
from send, resend, or verify replaces the prior timer and gates every control
that can call the provider until expiry. A successful send's provider
`cooldown_seconds` gates another send or resend. The bounded client fallback
when resend fails without a provider duration only dampens repeat resend
attempts on the code screen. Neither blocks verification.

The production control plane is HTTPS. A local HTTP control-plane override is
compiled only in Debug/test builds; Release ignores it.

## Lifecycle And Failure Semantics

- Sending and verifying OTP are single-flight operations. Provider throttling
  overrides the client cooldown. First delivery has no client-generated resend
  delay; a successful resend starts the approved 60-second timer, and changing
  email clears that client timer.
- The bundled page has one token-owning account operation at a time. While it
  is active, every control that can navigate, edit account inputs, open an
  external page, or start another operation is disabled and guarded. Only that
  token may apply a result or clear the lock; a native `busy` rejection is not
  rendered as the current operation's failure.
- The shared bundled-screen container owns a fixed `h-dvh` scrolling viewport
  inside the mobile document lock. Its content grows from `min-height: 100%`
  and applies both safe-area insets, so every onboarding/account state remains
  reachable on compact screens, with large text, and while the keyboard
  shrinks the native WebView through `keyboardLayoutGuide`.
- Invalid, expired, throttled, and network failures preserve the full email and
  the entered code when recovery is possible. A verification transport failure
  clears the code because the provider may already have consumed it; the
  approved network state remains on the code screen with resend available.
- Error classification follows the failed stage. Only a provider response from
  the verify stage can be `invalid_or_expired`; callback, session, malformed
  response, abort, timeout, and script failures are `network`. Provider OTP
  throttling preserves `Retry-After`, and request rejections preserve the
  provider code.
- The bundled page uses one bridge-error mapping for every catch path. Only a
  confirmed signed-out bootstrap/refresh result or a completed logout can show
  Welcome. Cold-start and `#account` discovery failures stay on a visible
  recovery screen whose retry calls only `session.bootstrap`; unknown, busy,
  stale, inactive, and unavailable failures remain network recovery rather than
  starting a new OTP flow.
- A mobile-session 401 is the only fetch result that means signed out. Other
  fetch failures preserve the current store and account state. Remote logout is
  best effort; failure or timeout never prevents local teardown and store
  rotation.
- Cold launch validates the cookie session before restoring a selection. The
  stored account id must match, and the selected instance must still appear in
  the authorized response.
- Login, logout, and account switch increment a session generation. Async
  results apply only when their generation is still current.
- The native coordinator is the single runtime admission authority. Login,
  logout, refresh, account switch, and store rotation revoke the selectable
  snapshot and detach any runtime before their first suspension. Selection,
  cold restore, and WebView registration require a current generation/store
  lease; a delayed open from the previous account is rejected.
- Runtime entry points are limited to validated cold restore and
  `session.selectInstance`. Runtime exits are the current Web account action,
  the old-Web native fallback, `app.showAccount`, and every account
  transition; all of them converge on the same AppModel route and teardown
  path.
- Revoked or stale selections return to the picker. An unavailable selected
  instance keeps account and switching controls usable.
- Instance entry continues through existing OIDC and consent. The app does not
  bypass first-instance consent or convert availability into authorization.
- Logout clears phone-owned control-plane and instance cookies, selection,
  private cache, and late results without revoking other devices.
- Signed-out bootstrap, account mismatch, failed verification, logout, and
  account switch rotate away from the previous account store.
- Cold restore accepts a stored selection only when the current account id
  matches and the instance still appears in the latest authorized response.

## Evidence Boundary

Backend authorization and response behavior, React state/accessibility, Swift
session state, cookie handling, and simulator UI use hermetic fixtures.
The owner separately verified production Supabase email OTP, a real account
session, and online/offline instance discovery on the iOS 27.2 simulator on
2026-11-06. Hosted token refresh competition, physical-device autofill, and
real first-instance consent remain explicitly unverified until an isolated
integration environment is available.

### Known-by-design ledger

- The repository's Python auth/setup harness does not drive the bundled React
  document, native message handler, real `WKWebView`, or `WKHTTPCookieStore`.
  `AUTH-SETUP-148` and `AUTH-SETUP-149` therefore cite the collected Vitest
  contract cases that own page behavior, while `AUTH-SETUP-150` records the
  Swift/WebKit boundary as partial without inventing a pytest wrapper that would
  not execute it. `AUTH-SETUP-151` through `AUTH-SETUP-154` keep live email,
  device autofill, first-instance consent, and hosted refresh competition
  visible as manual or gap evidence. `AUTH-SETUP-155` records the Swift/WebKit
  selected-instance history boundary and leaves physical-device gesture feel
  as manual acceptance.

## Bundled UI Build Ownership

`ui/mobile.html`, `ui/vite.mobile.config.ts`, and `npm run build:mobile` are the
single source and producer for the local React application. The generated
`ios/Avibe/Resources/MobileApp` directory is ignored build output because it
also incorporates shared UI modules, the full translation catalogs, global
styles, and an out-of-tree mascot asset. Committing that output would make
unrelated Web UI changes churn iOS hashes and require every Web contributor to
regenerate native resources.

The `ios-shell` job builds the bundle from the checked-out commit before either
simulator `xcodebuild`, verifies `mobile.html` and its referenced assets, and
then runs the native build and XCTest. The Xcode target keeps the generated
folder as its application resource and has an early verification phase that
checks the generated entry and asset directory without invoking or discovering
Node. CI performs the full referenced-asset check. The bundled-page XCTest
loads `MobileApp` from `Bundle.main`, so Debug and Release tests execute the
same generated bytes copied into the built application rather than a test
fixture or stale source directory.

The WebKit transport suite is serialized. Its hanging-response fixtures wait
for request/response events instead of racing the app deadline, and
cross-origin cases use a numeric loopback host rather than DNS fallback.
`ios-shell` uploads the `.xcresult` bundle when XCTest fails so the exact
assertion and attachment evidence remain available.

The validation matrix covers the CI-fixed Xcode 26.6 / iOS 26.4.1 simulator,
local Xcode 27.2 / iOS 26.4.1 full Debug XCTest and repeated transport runs,
and local Xcode 27.2 / iOS 27.2 Debug and Release-macro runs. Cold WebContent
startup on iOS 26.4.1 can exceed ten seconds, so the product readiness bound is
30 seconds while request and best-effort logout deadlines stay independently
bounded. `ios-shell` triggers on the browser input set declared by
`BROWSER_CHECKOUT` (except `Dockerfile`), with a structural test pinning both
workflow events to that source.

Focused lifecycle coverage injects named-store removal failures, relaunches
with persisted owned cleanup, ignores registry identifiers outside the
ownership ledger, and drives a real `window.open` child through the production
`WKUIDelegate` before verifying that the presented controller is dismissed and
both runtime WebViews are released before removal begins. A real WebKit
integration case snapshots the process-wide named-store registry before and
after rotation and cleanup, proving that no identifier outside the test-owned
set disappears. A retained-WebView case proves the release wait is bounded,
the current store rotates without attempting unsafe deletion, and a later
ledger sweep removes the detached store.

The native test inventory is hermetic by construction:

- coordinator tests use suite-scoped `UserDefaults` and an in-memory named-store
  registry;
- WebKit integration tests use suite-scoped ledgers, real named stores tracked
  by a test-owned registry, and loopback fixture servers;
- WebView factory and bundled-page tests use non-persistent stores;
- no onboarding test uses `UserDefaults.standard`,
  `WKWebsiteDataStore.default()`, shared cookie/cache/session storage, Keychain,
  production network, or a production symbol whose only purpose is testing.

The page failure matrix also covers provider rate limits with and without a
retry duration for send, resend, and verify.

Any future release, archive, signing, or TestFlight workflow must perform
`npm ci`, `npm run build:mobile`, and the same bundle verification in the job
that produces the archive. Release automation is outside this implementation
slice.
