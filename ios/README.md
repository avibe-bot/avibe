# Avibe iOS shell

This directory contains the SwiftUI shell for Avibe's bundled mobile account
flow and Web Workbench. The app signs in through local React pages, discovers
the current account's permitted instances, loads the selected HTTPS origin in
a persistent `WKWebView`, and owns the platform boundaries that browser content
cannot own: navigation handoff, downloads, native dialogs, and microphone
permission.

Phase 0a deliberately keeps the Web composer. The shell and Web UI ship the
versioned `avibeNative` bridge together. The explicit
`keyboardResizesViewport` capability coordinates keyboard ownership so native
and Web resizing cannot both run.

## Current contract

- Bundle identifier: `bot.avibe.ios`
- Display name: `Avibe`
- Deployment target: iOS 17
- The shell follows WebKit browser behavior except for the closed deviation
  list below. Origin-specific exceptions are not added for individual Web UI
  features.
- Configured instance and authentication origins stay in the primary WebView.
  Other primary-main-frame HTTP(S) navigation opens in
  `SFSafariViewController`.
- The primary runtime enables WebKit's interactive back gesture only when the
  immediate history item belongs to the selected instance origin. A
  back-forward traversal to authentication or another origin is rejected.
  Child windows retain their existing toolbar and do not enable this gesture.
- Subframes use WebKit's browser behavior for HTTP(S), `about:blank`,
  `about:srcdoc`, `blob:`, and `data:` navigation. A subframe cannot launch
  `mailto:`, `tel:`, or another external application scheme.
- Every ordinary `window.open` and `target="_blank"` target opens in a
  closable child WebView created with WebKit's supplied configuration,
  regardless of origin. This preserves `window.opener`; children apply the same
  rule recursively, and `window.close()` closes the matching child subtree.
  Each child shows its current host plus Done and Open in Safari controls; the
  latter opens the current HTTP(S) URL in the Safari app.
- Ordinary child-main-frame navigation stays in that child regardless of
  origin. Google authorization remains intercepted, and external application
  schemes use the system.
- Microphone capture is granted only to the configured instance main frame.
  Authentication, external, and arbitrary subframes receive neither microphone
  access nor a native bridge.
- Google authorization is intercepted with an email-code explanation.
- Instance WebViews (primary and child) show no form accessory bar
  (previous / next / Done) above the keyboard: `FormAccessoryBar` gives
  WebKit's content view a runtime subclass whose `inputAccessoryView` is nil.
  This uses a private WebKit class, an App Review risk the owner accepted on
  2026-11-08 (`docs/plans/ios-app.md` section 5). Keyboard QuickType
  suggestions are unaffected. The bundled sign-in page keeps the default bar.
- External application schemes from top-level browsing contexts use
  `UIApplication`.
- JavaScript alerts, confirmations, and prompts use native alerts. Their
  completion handlers are completed exactly once when the user responds or
  when the owning page, WebView, or content process goes away.
- Trusted instance HTTPS downloads, instance-origin `blob:` downloads, and
  instance-frame `data:` downloads use `WKDownload`, a unique temporary
  destination, and the system activity sheet. Other HTTP(S) download targets
  open in `SFSafariViewController`; all other download sources are rejected.
- Each phone account uses one persistent named `WKWebsiteDataStore`. The hidden
  account transport and runtime WebViews use that store at different lifecycle
  phases, so WebKit remains the only cookie owner and sessions survive app
  launches without native cookie snapshots. Account transitions release the
  runtime primary and child WebViews plus the hidden transport before rotating.
  Every created identifier enters a persisted ownership ledger before WebKit
  creates the store. Launch and post-rotation sweeps remove only non-current
  ledger entries, retaining failed UUIDs for later retry and never enumerating
  or touching stores outside the app account owner's ledger. There is no legacy
  orphan migration because no released Avibe version created these mobile
  stores. Runtime primary WebViews, popup children, SwiftUI-dismantled
  controllers, and the hidden transport contribute weak release barriers.
  Rotation gives all barriers one shared one-second deadline. If any remain
  retained, the app activates the fresh store without deleting old stores in
  that rotation; their ledger entries are retried at the next launch or
  rotation.
- Primary and child WebViews are created through one factory and pinned to the
  container safe area at the leading and trailing edges. Vertically, the
  factory's `install` takes one extent for both edges, because a document
  reserves the safe areas itself only by declaring `viewport-fit=cover`, which
  covers every edge at once. The primary runtime WebView spans the container
  top and bottom, and the SwiftUI browser route lets that container reach the
  screen top and bottom, so the instance page draws behind the status bar and
  the Home indicator and reserves them through `env(safe-area-inset-*)`. Under
  the old-Web account fallback toolbar the container starts below the toolbar,
  so the page does too. A child window shows arbitrary origins under native
  chrome and spans the safe area, below its navigation bar. One shared native
  viewport owner consumes keyboard end-frame notifications for runtime, popup, and
  bundled onboarding WebViews. It moves the bottom constraint only for a
  docked frame that overlaps the safe-area bottom, measures the overlap from
  the resting edge so the WebView ends at the keyboard top, follows the system
  animation duration and curve, and returns to the resting edge when the
  notified frame leaves that bottom. Floating and undocked frames never move
  it. Geometry and app lifecycle callbacks recompute the constraint from the
  last frame, discarding that frame only when the window bounds changed.
- The shell never sets `contentInsetAdjustmentBehavior`: once an app sets it,
  WKWebView stops deriving it from each document's `viewport-fit`. WebKit
  therefore gives an ordinary document the automatic insets below the status
  bar and above the Home indicator with `env(safe-area-inset-*)` at 0, and
  gives a `viewport-fit=cover` document no inset and the real values through
  `env()`.
  The instance `ui/index.html` does not declare `viewport-fit`, and Safari and
  the Home Screen app keep that declaration. While an instance document holds
  the outer-scroll lock described below, the bridge script appends
  `viewport-fit=cover` to its viewport declaration, unless the document
  already declares `viewport-fit`, and restores the declaration on release.
  Only the Workbench AppShell requests that lock, and it reserves every
  safe-area edge through `env()`, so the locked document's headers, tab bar,
  composer, drawers, and dialogs pad the status bar and the Home indicator
  exactly once. The declaration
  lives in the shell's bridge script, so already-deployed instances receive it
  without a Web change. Authentication, consent, and other unlocked documents
  keep their own declaration and WebKit's automatic inset.
- The status bar text follows the effective theme of the surface under it, not
  the system appearance. SwiftUI's root hosting controller hands
  `childForStatusBarStyle` to its newest representable controller, as long as
  nothing in the app sets `preferredColorScheme`. Over a runtime that is
  `WebViewController`, which returns `.darkContent` for a light page,
  `.lightContent` for a dark page, and `.default` while the page's scheme is
  unknown or the load-failure view covers it. When Account covers the runtime,
  the bundled page's controller is newer and keeps `.default`, matching that
  page's system theme. The shell's bridge script reads the computed
  `color-scheme` of the element the primary document draws at the top of the
  viewport. That inherits the root's, which the Web stylesheet sets per
  theme, unless a layer that is dark in every theme, such as the image viewer
  or the search palette, declares `color-scheme: dark` itself. The script
  reports the result through the internal `appearance.colorSchemeChanged`
  signal described below. Deployed instances need no Web change for the
  theme; the dark-layer declarations arrive with the Web UI. Only native presentation follows the page: the status
  bar, the strip behind it, and the old-Web fallback toolbar. No WebView's
  appearance is overridden, so `prefers-color-scheme` stays the system's and a
  page that follows the system keeps following it. Before the first report,
  the opaque WebView draws the system background under a `.default` status
  bar. An instance reload keeps the last scheme until the new document reports
  its own, and a commit to another origin resets it.
- Outer scrolling uses WebKit defaults until an instance-origin Workbench
  document requests `viewport.setOuterScrollLocked({locked: true})` for its
  own WebView. Each WebView keeps independent lock state. The lock is released
  on AppShell teardown, `pagehide`, or that WebView's next main-frame
  navigation commit, and reacquired on `pageshow` when WebKit restores the
  Workbench from its page cache. The Web UI disables `useIosKeyboardInset`
  only when the bridge declares `keyboardResizesViewport: 1`. On a phone, that same capability lets
  the Workbench hide its fixed bottom tab bar and matching clearance only while
  a focused editor has caused a software-keyboard-sized viewport contraction.
  Home quick tasks reveal the complete shared composer after the native
  viewport settles. Hardware-keyboard focus and phone rotations rebase the
  keyboard-free geometry without hiding chrome; a known software keyboard stays
  active across rotation until the resized viewport recovers. Safari and
  installed Web apps keep their existing chrome behavior.
- `AppIcon.appiconset` contains one opaque 1024-by-1024 icon. It is a temporary
  upscale of the existing 512-pixel iOS Home Screen Vibey artwork and should be
  replaced when a native 1024-pixel source is delivered.
- The user agent includes `AvibeiOS/<CFBundleShortVersionString>`.

The closed deviation list is:

1. Primary-main-frame HTTP(S) navigation outside the configured instance and
   authentication origins opens in `SFSafariViewController`.
2. Google authorization endpoints are intercepted because Google rejects
   embedded browsers.
3. Top-level external application schemes use `UIApplication`; subframes
   cancel them.
4. Microphone capture is automatically granted only to an instance-origin
   main frame.
5. Admitted downloads use `WKDownload` and the system activity sheet.
6. The origin-checked native bridge is available only to an instance main
   frame.
7. The primary runtime enables WebKit's interactive back gesture only for an
   immediate selected-instance history item and rejects cross-origin
   back-forward traversal.

## Native bridge v1

`NativeBridge.swift` injects `window.avibeNative` at document start. The Web UI
accesses it only through `ui/src/lib/native.ts`.

```ts
interface AvibeNative {
  contract: 1;
  platform: 'ios';
  shellVersion: string;
  capabilities: Record<string, number>;
  call<T>(method: string, params?: object): Promise<T>;
  on(event: string, handler: (payload: unknown) => void): () => void;
}
```

Each message is accepted only when `frameInfo.isMainFrame` is true and
`frameInfo.securityOrigin` exactly equals the configured instance origin.
Instance-origin child main frames receive the bridge because WebKit supplies
them the shared configuration; authentication and external child documents,
all subframes, and direct transport calls from those frames remain rejected.
The viewport method additionally requires the source to be a WebView managed
by the shared factory. Both the primary WebView and an instance-origin child
can therefore lock their own outer scroll state; an unknown WebView receives
`forbidden`. Before the bridge script posts a lock or release, it adds or
restores `viewport-fit=cover` in the calling document's viewport declaration,
as the current contract describes. Account-action registration requires the
source to be the selected-instance primary WebView; a child receives
`forbidden`.
The document-start bridge also reports the internal
`navigation.historyChanged` signal after `history.pushState` or
`history.replaceState`. It is not an advertised capability and carries no
history data. The trusted primary-main-frame handler uses it only to reread
WebKit's current and immediate back items; the controller rejects child
WebViews, and those native origin checks remain the gesture authority.
The same script reports the internal `appearance.colorSchemeChanged`
signal with `{scheme: "light" | "dark" | null}` when the computed
`color-scheme` at the top of the viewport changes. It rereads it, at most
once per frame, on root attribute mutations, body child insertions and
removals (the layers dialogs, viewers, and palettes portal in), stylesheet
loads, and `prefers-color-scheme` changes, and directly on document load and
a page-cache restore. A layer that declares its own scheme is therefore a
body-level layer. It is not an advertised capability either. Only the primary
WebView's report drives the status bar; a child receives `forbidden`.

The native bridge method surface is fixed:

| Capability | Method | Result |
| --- | --- | --- |
| `accountPresentation: 1` | `app.showAccount()` | `null`; covers the runtime with the bundled account page, closing child windows; the primary keeps its document for Account Back |
| `accountPresentation: 2` | `app.setAccountActionAvailable({available})` | `null`; primary Workbench display hint used to suppress or restore the old-Web native fallback |
| `app: 1` | `app.info()` | `{contract, platform, shellVersion, capabilities}` |
| `app: 1` | `app.openExternal({url})` | `{opened}` for `http`/`https` URLs with a host and `mailto`/`tel` URLs with a target |
| `haptics: 1` | `haptics.impact({style})` | `null`; styles are `light`, `medium`, `heavy`, `soft`, or `rigid` |
| `haptics: 1` | `haptics.notify({type})` | `null`; types are `success`, `warning`, or `error` |
| `haptics: 1` | `haptics.selection()` | `null` |
| `keyboardResizesViewport: 1` | No method | Declares that UIKit shrinks this WebView to the docked keyboard frame and returns it to its resting edge (the screen bottom for the primary, the safe-area bottom for a child) when the keyboard leaves |
| `push: 1` | `push.status({vapidPublicKey?: string})` | `{state: "unavailable" | "off" | "on", revokeState: "active" | "absent" | "pending", endpoint?, keys?}`; read-only and scoped to the current account, bind generation, and instance origin. `state: "on"` and `keys` require a matching VAPID key; cleanup facts remain visible across VAPID rotation, and a pending revoke remains `off` with its concrete endpoint. |
| `push: 1` | `push.subscribe({vapidPublicKey, forceResubscribe?: boolean})` | `{endpoint, keys: {p256dh, auth}}`; `forceResubscribe` requests a concrete replacement even when one matching active registration exists; the Web UI submits this subscription through the existing instance API |
| `push: 1` | `push.unsubscribe()` | `{revoked: boolean, revoke: "confirmed" | "pending" | "not_needed"}`; native confirms the concrete registration revoke, reports no scoped target, or retains a pending revoke for recovery |
| `voice: 1` | `voice.permission()` | `{state: "granted" | "denied" | "prompt"}` without prompting |
| `viewport: 1` | `viewport.setOuterScrollLocked({locked})` | `null`; applies to the calling managed WebView |
| `direct: 1` | `direct.status()` | `{transport: "tunnel" \| "direct", sequence}`; read-only, takes no parameters, and describes the instance the calling WebView was made for. This shell has no direct transport, so the answer is always `{transport: "tunnel", sequence: 0}` |

After a validated notification tap, the native shell emits
`push.opened({url})` to the selected instance's primary WebView. The event
contains only a same-origin path, query, and fragment; invalid, unavailable,
or mismatched notification context opens the instance recovery surface.

When the path that carries the selected instance changes, the shell emits
`direct.transportChanged({transport, sequence})` to every instance-origin
document of the runtime, the primary and instance-origin child windows, after
the data store's route has changed. `sequence` grows with every change while
the App process lives. This shell never emits it: see the next section.

Covering the runtime instead of closing it did not change any capability
number: `app.showAccount()` keeps its parameters and `null` result, and no Web
UI decision depends on whether the runtime survives, so current and older Web
UI both return to the same document without feature detection. The return path
belongs to the bundled page, which ships with the shell and learns it from the
`#runtime-account` start fragment; `#account` keeps the instance-list Back.

Replies use `{ok: true, result}` or
`{ok: false, error: {code, message}}`. The compiled errors are
`invalid_request`, `invalid_argument`, `forbidden`, and `unsupported`.
Unknown methods never expand the native surface.

The production Supabase Auth origin is an explicit primary-navigation
allowlist entry. If Avibe Cloud moves to another Supabase project, update that
origin and its navigation test in the same change.

## Remote direct path (inactive)

The design is `docs/plans/remote-direct-ios.md`. The App can carry the
selected instance's origin over a direct transport instead of Cloudflare
Tunnel, without changing the origin: WebKit's connections for that one host
go to a loopback HTTP CONNECT listener inside the App, which relays them over
transport streams to the instance. TLS stays between WebKit and the instance;
the shell adds no trust override and sees only ciphertext.

**Nothing in this shell implements a transport**, so the path never
activates: no listener is created, `proxyConfigurations` is never written
(not even cleared), `direct.status()` answers `tunnel`, and no event is
emitted. `AppModel` takes the transport as an optional argument that
`AvibeApp` does not pass.

That is not the same as nothing existing. From launch, `AppModel` holds a
`DirectPathController` and its route owner. The controller reads the
application state once, sets `AccountWebsiteDataStore.onWillRotate`, and
observes the App entering the background and the foreground; without a
transport each of those callbacks returns without doing anything. The bridge
advertises `direct: 1`.

The tests hold the inactive state by counting, not by looking at the end
state: `DirectRouteOwner.writeCount` counts every write of
`proxyConfigurations` at the only assignment in the App, and
`DirectConnectListener.socketsCreated` counts every listening socket at the
only place one is made. Both must stay where they were across launch,
opening and switching instances, background and foreground, rotation, and
sign-out. A scan of the process's listening sockets, on any address of
either family, is the second line, and the tests also observe the real
setter on the account's stores. The tests that install a route show that the
counters, the scan and the observer do move.

- `DirectTransport.swift` is the seam: `DirectTransporting` is told which
  instance is selected, reports when its path is usable or lost, and opens
  byte streams. A report is bound to the selection it was requested for.
- `DirectConnectListener.swift` binds `127.0.0.1` on a port the system
  chooses, only while a route is being installed or is installed. It answers
  `CONNECT <instance host>:443` and refuses everything else: another host, a
  subdomain (which `matchDomains`, a suffix match, also sends it), another
  port, no port, userinfo, an IP literal, any other method, a body, and a
  head over 4 KiB or slower than 5 seconds. It writes the ingress contract's
  canonical head to the stream, never WebKit's bytes, passes the instance's
  status back, and then copies both directions with backpressure and
  half-close. At most 64 connections: one more is closed at once, with no
  answer, and returns the path to the tunnel. No idle timeout. When the path
  cannot serve a connection, the status is written to WebKit before the
  failure is reported, so stopping the listener does not cost WebKit the
  answer.
- `DirectPathController.swift` holds the state. It is `tunnel` unless a
  transport reported a usable path for the current tags, the listener is
  bound, and the route is installed. A stream that cannot be opened, a
  refused or unanswered CONNECT, a failed listener, the connection limit, a
  lost path, and the App entering the background each return to `tunnel` at
  once: the route is removed first, then the listener and its relays close,
  then the event is emitted.
- `DirectRouteOwner` is the only writer of `proxyConfigurations` on an
  account data store, which the runtime WebViews and the hidden account
  transport share. The route matches the instance's exact host, with
  `allowFailover` off.
- `AppModel` ends the selection wherever the browser for a runtime goes
  away: in `stopRuntime()`, and in `registerRuntimeController` when it
  refuses a WebView and returns to the picker.
- Every route carries its tags: the selection, the `MobileRuntimeContext`
  lease (account, instance, account generation), the data-store identifier,
  the instance authority, and the rendezvous generation. Before a route is
  installed the controller asks `MobileSessionCoordinator.isCurrentRuntime`,
  the check the route and the WebView registration already use, and the route
  owner compares the store identifier. A late report, or a listener that
  becomes ready after its selection ended, installs nothing.
- The route leaves with the store it was installed on:
  `AccountWebsiteDataStore.rotate()` runs `onWillRotate` before it replaces
  the store. `AppModel` also stops the selection wherever it stops the
  runtime, so an instance switch removes the route without a rotation.

## Mobile account onboarding

The app bundles the approved `mobile-app/*` React flow and exposes a separate
`window.avibeMobile` bridge only to its top-level
`avibe-app://bundle` document. A read-only `WKURLSchemeHandler` serves the
Vite build generated from the same source commit so ES modules, styles, and
image assets share one fixed local origin. Its fixed
surface is `session.bootstrap`, `auth.requestOtp`, `auth.verifyOtp`,
`auth.signInWithPassword`, `auth.verifySecondFactor`, `auth.verifyPasskey`,
`session.refresh`,
`session.selectInstance`, `session.logout`, `app.returnToInstance`, and the
closed `app.openExternal` allowlist (the two install guides and avibe.bot's
Account security page). Instance documents, child windows, Show Pages, and
iframes do not receive these handlers.

A complete six-digit email or authenticator code starts verification as soon
as an edit completes it, whether typed, pasted, or offered above the keyboard
through `autocomplete="one-time-code"`. One edit sends at most one request: the
page's operation lock absorbs a duplicate input and change event, a re-render
never resends an unchanged code, and a failed code is sent again only after an
edit or a press of the button, which stays for retry. A code completed while
another operation runs, during a provider cooldown, or inside avibe.bot's
second-factor throttle window waits for the button. A recovery code is free
text and always waits for the button.

The bundled surface fills the container safe area so its gradient reaches the
status bar and Home indicator. Its shared `h-dvh` viewport owns vertical
scrolling inside the mobile document lock, while the content column applies
top and bottom safe-area padding and grows from `min-height: 100%`. Every
screen therefore keeps long content reachable, including OTP with the software
keyboard, long email addresses, offline recovery, account details, and large
instance lists. The WebView reaches the container's top and bottom edges, and
the page declares `viewport-fit=cover`, so its `max(1.5rem, env(...))`
padding is the only inset on either edge. The shared native viewport owner
gives onboarding the primary runtime's container-bottom resting edge and the
same docked-keyboard frame handling.

The hidden account transport is a native-owned `WKWebView` with no message
handler. It loads only the configured control-plane auth configuration and runs
fixed scripts in `WKContentWorld.defaultClient`: public Supabase OTP requests,
the Supabase password grant, the existing `/auth/callback`, the mobile session
GET/DELETE endpoints, and the
second-factor `POST /api/two-factor/passkey-challenge` and
`POST /api/two-factor/verify`. The
isolated world is defense in depth, not a separate security process. The actual
boundary comes from the fixed control-plane origin, narrow compiled scripts,
no page bridge, and lifecycle separation from runtime WebViews. Access and
refresh tokens are transient script arguments and are never returned to the
bundled page or instance content.

The app's sign-in email is code-only. The OTP request names
`redirect_to=https://avibe.bot/auth/callback?source=ios-app`, and the
production Supabase email templates show only the code for exactly that
redirect; other control-plane origins, such as a Debug override, still receive
the link-and-code email. A sign-in link in the email opened Safari, which
consumed the one-time code the app was waiting for.

Readiness navigation has a native 30-second deadline. Fixed account scripts
share one `AbortController` deadline mechanism: OTP/session operations use 15
seconds and best-effort current-device DELETE uses 5 seconds. Both navigation
actions and responses must remain on the configured control-plane origin.
Fixed requests reject redirects, so email, OTP, tokens, and cookies cannot
follow another origin.
Timeout, abort, malformed response, and script failures map to `network`.
Only a Supabase verify-stage rejection maps to `invalid_or_expired`; provider
throttling preserves `Retry-After`, and OTP request rejections preserve the
provider error code.
The bundled page consumes that classification through one mapping shared by
every catch path. Only an explicit signed-out bootstrap/refresh result or a
completed logout enters Welcome. A failed cold-start or `#account` bootstrap
shows the existing retry presentation and retries only bootstrap; unknown,
busy, stale, inactive, and unavailable failures remain network recovery.

WebKit owns every persistent cookie. The account transport and runtime share one
named store, but the account transport is invalidated before an instance opens.
Account opened from an instance (`app.showAccount()` or the old-Web fallback
bar) covers the runtime instead of stopping it: the root view keeps the
primary WebView with its document, history, and scroll position, dismisses
the keyboard, and closes child windows, which would otherwise sit above the
account page as sheets.
That bundled page starts with `#runtime-account` and creates no hidden
transport. It reads the coordinator's session snapshot, and the first account
transition (refresh or sign-out) releases the runtime before it creates the
transport, so the two never overlap. Account Back calls `app.returnToInstance`,
which shows the same runtime controller again while the coordinator still
admits it. Selecting an instance (including the same one) and every account
transition release the runtime as before; `app.returnToInstance` then answers
`runtime_unavailable` and Back shows the instance list. The covered runtime
keeps running and receives no touches or accessibility focus. A JavaScript
dialog or finished download it raises meanwhile still uses the runtime's
ordinary UIKit presentation, above the account page; that path grants no
account operation.
The native coordinator is the single runtime admission authority. Login,
logout, refresh, account switch, and store rotation increment the generation,
revoke the selectable snapshot, and detach any runtime before their first
suspension. Selection and cold restore issue a generation/store runtime lease;
the AppModel route and WebView registration both reject a delayed old lease.
Logout can preempt an older account request, attempts current-device server
logout under its short bound, clears the stored selection, rotates to a new
named store, and removes the old store even when DELETE fails or times out.
Late results can mutate only the abandoned store and cannot restore the next
account.
The bundled page also admits one token-owning account operation at a time.
Every interactive control consumes the derived lock, and only the current token
may apply an async result or release the page for another operation.
Signed-out bootstrap, a stored-selection account mismatch, and a failed OTP
verification or password sign-in also rotate to a fresh store before another
account attempt.
Successful first delivery leaves resend available unless the provider returns
`Retry-After`; successful resends use the approved client cooldown, while any
provider cooldown takes precedence. A verification network failure keeps the
email and approved network presentation but clears the possibly consumed code,
leaving resend as the recovery path. A mobile-session 401 alone means signed
out; every other fetch failure preserves the current store and state.

Cold launch validates `/api/mobile/session` before restoring a selection. The
stored account id must match, the instance must still be in the authorized
list, and it must be available. Owned, explicitly shared, and permitted
Organization instances come from the backend's existing authorization rules;
availability is informational and the instance OIDC/consent flow remains the
entry authority. The app depends on backend PR #308, merged as
`1cea1dffdc3e68c6a2cc8594064bf94367485b5c` and successfully deployed to
Production by Vercel on 2026-11-05 at 18:44:16 UTC. Public no-cookie and health
probes return the expected 401 and 200. The owner separately verified production
email OTP on the iOS 27.2 simulator on 2026-11-06; this implementation lane used
no real account in automation.

The hermetic WebKit transport tests run serially, wait for fixture events with
budgets separate from the app's deadlines, and use numeric loopback origins for
cross-origin redirect checks. The hanging-readiness case uses a 3-second
readiness deadline, which a cold WebContent or GPU process launch on a loaded
simulator can exceed before the request is sent; it retries such an attempt
within the fixture budget instead of counting it. When `ios-shell` XCTest fails, CI uploads
`AvibeTests.xcresult` as `ios-shell-xcresult`.

### Two-step verification

An account with avibe.bot two-step verification passes its second factor in
the app after the email code (`docs/plans/2026-11-07-mobile-two-factor.md`,
design frames `mobile-app/two-factor/*`). The flow depends on avibe-backend's
`403 two_factor_required` session answer and its
`apple-app-site-association` file. A password sign-in reaches the same
second factor.

- `GET /api/mobile/session` answers `403 {"error":"two_factor_required",
  "email","gate","methods"}` for a session that passed the email code or
  password and owes the second factor; `401` still alone means signed out. The
  transport reports it as a signed-in discovery, so `auth.verifyOtp`,
  `auth.signInWithPassword`, `session.bootstrap`, and `session.refresh` return
  `{status: "twoFactorRequired", email, gate, methods}` and nothing rotates the
  store. `email` falls back to the address
  typed in this sign-in and is `null` when neither is known. Unknown method
  kinds are skipped.
- `auth.verifySecondFactor({method: "totp" | "recovery", code})` posts the code
  to `/api/two-factor/verify`, then reads the session again. Its errors are
  `invalid` (any `400`), `rate_limited` with `retry_after_seconds`, and
  `network`. A `401` from either step is the explicit signed-out answer: the
  coordinator rotates to a fresh store and returns `{status: "signedOut"}`.
- `auth.verifyPasskey()` fetches WebAuthn request options from
  `/api/two-factor/passkey-challenge` (`404` is `passkey_unavailable`), runs
  `ASAuthorizationPlatformPublicKeyCredentialProvider` for relying party
  `avibe.bot` with user verification required, and posts the assertion as
  WebAuthn `AuthenticationResponseJSON` (unpadded base64url) with the
  challenge token. Native code asserts only for `avibe.bot`; options naming
  another relying party are `passkey_unavailable`. Dismissing the system sheet
  is `passkey_cancelled`; every other system failure is
  `passkey_unavailable`. The options, token, and assertion pass only between
  the transport and native code, never to the bundled page.
- Both routes require a trusted Origin on cookie mutations; the transport's
  same-origin `fetch` supplies it. Success re-issues `avibe_session` with the
  proof into the account's WebKit store, which the runtime WebView shares, so
  instance OIDC on avibe.bot sees the proof. The proof belongs to this app's
  Supabase session, so the app asks once per sign-in.
- An account that must turn the feature on (`gate: "enroll"`) opens
  `https://avibe.bot/app/account/security` in Safari; "I've turned it on"
  runs `session.refresh`, which then answers with the `challenge` gate.

`Avibe.entitlements` adds `com.apple.developer.associated-domains` with
`webcredentials:avibe.bot` to Debug and Release. avibe-backend's
`/.well-known/apple-app-site-association` lists `68B2SQ6YK3.bot.avibe.ios`.
The owner enabled the Associated Domains capability for the `bot.avibe.ios`
App ID (Team ID prefix `68B2SQ6YK3`, confirmed) on 2026-11-07; its existing
provisioning profiles were invalidated and regenerate on the next automatic
signing. Simulator CI builds without signing, so it compiles the entitlement
but cannot exercise a system passkey. That check needs a signed build on a
device (or a simulator with the domain in developer mode) and an account with a
passkey, and remains manual.

### Protected Vault passkeys

The entitlement also lists `webcredentials:sandbox.avibe.bot`, the relying
party of the Protected Vault passkey
(`docs/plans/2026-11-10-ios-protected-vault-passkey.md`). WKWebView offers a
passkey to a third-party app only when the relying party is one of the app's
associated domains, so without this entry the system refuses every Vault
ceremony before a passkey sheet appears. `sandbox.avibe.bot` serves an
`apple-app-site-association` listing `68B2SQ6YK3.bot.avibe.ios` (vault-sandbox
`hosting/`).

The shell adds no Vault code and no bridge method. Setup, unlock, approvals,
signing, and reveal run the Web flow unchanged: the sandbox iframe in the
instance page, and the sandbox's own top-level setup and authorization
windows, which the shell shows as ordinary child WebViews. The sandbox calls
`navigator.credentials`, and WebKit presents the system passkey sheet. Native
code never receives the assertion or the PRF output, and
`MobilePasskeyAuthenticator` still asserts only for `avibe.bot`.

The protected tier needs the PRF extension, which iOS provides from 18.0; the
deployment target does not change. `ios/scripts/testflight.py` requires the
archived app to carry exactly the two domains. The device check is manual and
listed in the plan: passkey creation and assertion in the top-level sandbox
window, the window's result reaching the requesting iframe, and a PRF
assertion inside the sandbox iframe.

### Password sign-in

An account that set a password on avibe.bot (Account security) can sign in
with it instead of the email code (`docs/plans/2026-11-08-mobile-password-sign-in.md`,
design frames `mobile-app/password/*`). The email code stays the primary path
and the only way to create an account in the app.

- The email step offers "Sign in with password" under Send verification code.
  It switches the same form to password mode at once, without asking for the
  email first: the visible email field (now `autocomplete="username"`, typed
  text carried over, focused when empty) sits above the
  `autocomplete="current-password"` field, so iCloud Keychain can match the
  saved avibe.bot login through the existing `webcredentials:avibe.bot`
  association. "Sign in with an email code" returns to code mode with the email
  kept. Return in the email field moves to the password; Return in the
  password field submits; nothing auto-submits. Sign in, Forgot password, and
  "Use an email code instead" check the email first and show the page's own
  copy (the form sets `novalidate`, so WebKit shows no validation bubble);
  Sign in also needs a non-empty password. This one-form layout is the owner's
  change of 2026-11-09, replacing the first identifier-first password step.
- `auth.signInWithPassword({email, password})` returns the same session state
  as `auth.verifyOtp`, plus `weak_password: {reasons}` (`length`,
  `characters`, `pwned`) when Supabase accepted a password that no longer
  meets avibe.bot's rules. Its errors are `invalid_credentials` (a grant
  `400`, or any other grant `4xx`; Supabase also answers this for an account
  that never set a password), `email_not_confirmed` (a grant `400` with that
  code), `rate_limited` with `retry_after_seconds` from `Retry-After` when
  present (the page then waits 60 seconds), and `network`. A session `401`
  stays the explicit signed-out answer. Every failed attempt rotates to a
  fresh store, as a failed OTP verify does.
- The transport's fixed script posts `{email, password}` to
  `{supabase_url}/auth/v1/token?grant_type=password` with the anon key as
  `apikey` and bearer and `credentials: "omit"`, then posts the session pair
  to `/auth/callback` and runs the shared `/api/mobile/session` read, which
  reports `403 two_factor_required` to the existing two-step flow. It shares
  the OTP scripts' deadline, origin, and redirect rules. The password is a
  transient script argument: it is sent only in the grant's body and never
  returned, persisted, or logged, and errors carry fixed native messages
  rather than provider text. A bridge request's description, dump, and any
  log line built from it name parameters, never their values. The page keeps
  the password only in the email step's field state: returning to code mode
  clears it, and success or leaving the step unmounts it. Editing the email
  keeps it.
- "Use an email code instead" (shown for `invalid_credentials` and
  `email_not_confirmed`) and the forgot page's Send verification code call
  `auth.requestOtp` for the same email and enter the code step. Forgot
  password's Open in Safari opens `https://avibe.bot/app/account/security`,
  already on the `app.openExternal` allowlist; avibe.bot has no separate
  reset page.
- A weak password shows one non-blocking notice after any second factor and
  before the instances. Its `pwned` copy names the data breach; other reasons
  name the 8-character rule.

The page follows the approved default composition on every height and lets
the shared viewport scroll, as the shipped code and two-step screens do; the
password form scrolls its Sign in action above the keyboard while the field
has focus.

Coordinator tests inject an in-memory named-store registry. Integration tests
that require real named stores use suite-scoped `UserDefaults`, track every UUID
they create, and remove only that set. One integration case snapshots WebKit's
live registry around rotation and cleanup to prove unrelated identifiers
survive. Popup teardown coverage loads a hermetic page in the production
runtime WebView, creates the child through the real `window.open` /
`WKUIDelegate` path, and observes controller dismissal, WebView release, and
navigation callback shutdown without test-only production hooks. Another case
retains a WebView past the release bound and proves that logout still rotates
the store, deletion is deferred, and a later ownership-ledger sweep completes
cleanup.

## WebKit API inventory

This inventory is organized by the finite WebKit API surface instead of by
individual Web UI call sites. It includes the iOS methods present through the
local iOS 27 SDK; iOS 27-only methods are documentation-only while CI remains
pinned to Xcode 26.6.

### `WKNavigationDelegate`

| Method | Handling | Verification |
| --- | --- | --- |
| `decidePolicyFor navigationAction` | Implemented. Download actions use the download admission rule. New contexts continue to `WKUIDelegate`. Subframes follow WebKit except external application schemes. The primary main frame externalizes HTTP(S) destinations outside instance/authentication origins; child main frames use WebKit except for Google and external application schemes. | Pure tests cover primary and child top-level destinations, `about:srcdoc`, blank, blob, data, cross-origin HTTPS, external schemes, and trusted/untrusted download sources. |
| `decidePolicyFor navigationAction:preferences:` | Intentionally not implemented because WebKit calls this overload instead of the implemented action-policy method. `defaultWebpagePreferences` remains the single preference owner. | Simulator build verifies the selected overload. |
| `decidePolicyFor navigationResponse` | Implemented. Attachment and non-displayable responses use the same download admission rule; displayable responses use WebKit. | Simulator XCTest and build cover response conversion to `WKDownload`. |
| `didStartProvisionalNavigation` | Implemented. Cancels stale JavaScript dialogs and starts primary loading state. | Simulator build; exactly-once dialog cleanup is a manual interaction check. |
| `didReceiveServerRedirectForProvisionalNavigation` | Uses WebKit default. Final action/response policy and download redirect admission remain authoritative. | Redirected downloads are rechecked by `WKDownloadDelegate`; normal redirects are a manual navigation check. |
| `didFailProvisionalNavigation` | Implemented. Reports non-cancellation primary failures. | Simulator build; failure UI is a manual check. |
| `didCommit` | A commit in any managed WebView releases that WebView's document-requested outer-scroll lock before the new document runs. | Per-WebView outer-scroll state XCTest plus simulator build. |
| `didFinish` | Implemented. Completes primary loading state. | Simulator build. |
| `didFail` | Implemented. Reports non-cancellation committed failures. | Simulator build; failure UI is a manual check. |
| `didReceive challenge` | Implemented with `performDefaultHandling`. The shell adds no certificate, HTTP-auth, or client-certificate override. | Standard HTTPS loads in simulator CI; custom challenge UI is unsupported. |
| `webViewWebContentProcessDidTerminate` | Implemented. The primary view closes children and reloads the configured instance; a child closes itself. | Simulator build; forced termination is a manual simulator/device check. |
| `authenticationChallenge:shouldAllowDeprecatedTLS` | Intentionally not implemented, so deprecated TLS is not admitted by shell policy. | API ledger; no deprecated-TLS fixture is maintained. |
| `navigationAction:didBecome download` | Implemented and attaches the shared download context/delegate. | Simulator build and download policy tests. |
| `navigationResponse:didBecome download` | Implemented and attaches the shared download context/delegate. | Simulator build and download policy tests. |
| `shouldGoTo backForwardListItem` | Uses WebKit default continuation. The ordinary action-policy delegate still rejects a primary `.backForward` target outside the selected instance origin. | Hermetic SPA/auth-history XCTest on iOS 26.4.1 and 27.2; gesture feel remains a device check. |
| `willSubmitForm` (iOS 27) | Uses WebKit default continuation. The pinned Xcode 26.6 toolchain does not expose this method. | Revisit when CI moves to an iOS 27 SDK. |

### `WKUIDelegate`

| Method | Handling | Verification |
| --- | --- | --- |
| `createWebViewWith` | Implemented. Every ordinary new-window target creates a closable child WebView with WebKit's exact configuration, preserving `window.opener` across origins and recursive child creation. Google is intercepted and external application schemes use the system. | Pure new-window tests cover instance, authentication, Vault, external HTTP(S), `about:blank`, Google, and application schemes; simulator evidence covers the host toolbar and same-origin opener round trip. |
| `webViewDidClose` | Implemented and closes the matching child subtree. | Simulator build; `window.close()` is an interactive check. |
| `runJavaScriptAlertPanelWithMessage` | Implemented with an initiating-WebView `UIAlertController` and exactly-once completion. | Simulator build; interaction and removal cleanup are manual checks. |
| `runJavaScriptConfirmPanelWithMessage` | Implemented with OK/Cancel and exactly-once completion. | Simulator build; existing Web UI `confirm()` callers are manual checks. |
| `runJavaScriptTextInputPanelWithPrompt` | Implemented with a text field and exactly-once completion. | Simulator build; interaction is a manual check. |
| `requestMediaCapturePermissionFor` | Implemented. Only microphone access from the configured instance main frame is granted; camera and every other origin/frame are denied. | Origin policy is compiled in simulator CI; microphone persistence is a device check. |
| `requestDeviceOrientationAndMotionPermissionFor` | Uses WebKit/system prompting. The shell adds no broader permission grant. | Manual device check if a Web UI caller is added. |
| `shouldPreviewElement`, `previewingViewControllerForElement`, `commitPreviewingViewController` | Deprecated APIs are intentionally not implemented; current WebKit link-preview behavior is retained. | WebKit default; long-press preview is a manual check. |
| `contextMenuConfigurationForElement`, `contextMenuWillPresentForElement`, `contextMenuForElement:willCommitWithAnimator:`, `contextMenuDidEndForElement` | Use WebKit's default context menu and commit behavior. | Manual long-press check. |
| `showLockdownModeFirstUseMessage` | Uses WebKit's default Lockdown Mode message. | System-owned behavior; no shell test. |
| `willPresentEditMenuWithAnimator`, `willDismissEditMenuWithAnimator` | Use WebKit's default edit menu. This preserves selection, copy, paste, and the Web UI clipboard fallback. | Existing Web UI clipboard tests plus manual copy/paste check. |
| `runOpenPanelWith` | Uses WebKit's Safari-matching iOS file picker. The shell declares localized camera and photo-library-add usage descriptions. | Simulator build validates resources; Files, Camera, and Save Image remain device checks. |
| `requestGeolocationPermissionFor` (iOS 27) | Uses WebKit/system prompting. The pinned Xcode 26.6 toolchain does not expose this method. | Revisit when CI moves to an iOS 27 SDK. |
| `insertInputSuggestion` | Uses WebKit's default keyboard insertion behavior. | Manual keyboard suggestion check. |

### `WKDownloadDelegate`

| Method | Handling | Verification |
| --- | --- | --- |
| `decideDestinationUsing` | Implemented. Cleans the suggested filename and writes into a unique temporary directory. | Simulator build; asset-independent destination logic is exercised by download CI. |
| `willPerformHTTPRedirection` | Implemented. Reapplies admission to every redirect; external HTTP(S) moves to Safari and all other rejected targets cancel. | Pure download tests and simulator build. |
| `didReceive challenge` | Implemented with `performDefaultHandling`; Phase 0a adds no download-specific credential UI or credential override. | Simulator build; custom authenticated-download UI is unsupported. |
| `downloadDidFinish` | Implemented. Queues the completed file for the system activity sheet. | Simulator build; sharing is an interactive check. |
| `didFailWithError` | Implemented. Cleans temporary data and suppresses expected cancellation errors. | Simulator build; failure alert is a manual check. |
| `decidePlaceholderPolicy`, `didReceivePlaceholderURL`, `didReceiveFinalURL` | Intentionally not implemented. System Downloads placeholders are disabled because the shell owns a private temporary destination and shares the completed file. | API ledger; the custom destination/cleanup path is compiled in simulator CI. |

### Configuration and preferences

| Property | Shell value and reason | Verification |
| --- | --- | --- |
| `websiteDataStore` | One persistent named store belongs to the current phone account. Runtime primary/child WebViews and the hidden account transport use it in non-overlapping phases; logout/account switch rotates the identifier and deletes the old store. | Coordinator lifecycle XCTest plus real `WKHTTPCookieStore` handoff, chunk rotation/deletion, redirect isolation, and delayed-response tests. |
| `websiteDataStore.proxyConfigurations` | Never written by this shell, which has no direct transport. With one, `DirectRouteOwner` is the only writer: one HTTP CONNECT route to a loopback listener for the selected instance's exact host, removed on any failure, on background, when the runtime stops, and when the store rotates. | Lifecycle XCTest with and without a stand-in transport on real `WKWebsiteDataStore` objects, including that no listening socket and no route exist without one; one WebKit load through the route on the simulator. |
| `userContentController` | The root installs the document-start `avibeNative` script and reply handler. Children receive WebKit's exact configuration through the shared WebView factory, preserving the same controller and `window.opener`; each message still repeats the main-frame and exact-origin check. | XCTest covers instance main-frame discovery and calls, child discovery, subframe absence, external-origin absence, and unknown-method rejection. |
| `applicationNameForUserAgent` | `AvibeiOS/<CFBundleShortVersionString>`, appended by WebKit to its standard user agent. | Configuration XCTest and Debug/Release simulator builds. |
| `allowsInlineMediaPlayback`, `mediaTypesRequiringUserActionForPlayback` | WebKit defaults. Phase 0a adds no media playback policy. | API ledger; playback is a device check. |
| `javaScriptCanOpenWindowsAutomatically` | WebKit default. WebKit decides whether a script may request a new context; accepted requests use the child-WebView rule. | New-window policy tests and simulator build. |
| `allowsBackForwardNavigationGestures` | Disabled for child windows. The primary runtime enables it dynamically only when the immediate back item belongs to the selected instance origin. | Hermetic SPA/auth-history XCTest on iOS 26.4.1 and 27.2; gesture feel remains a device check. |
| `allowsLinkPreview`, `dataDetectorTypes` | WebKit defaults. | API ledger; interactions are manual checks. |
| `isElementFullscreenEnabled` | WebKit default. Fullscreen product work remains deferred. | API ledger; the deferral is recorded below. |
| `isInspectable` | Enabled for managed runtime WebViews only in Debug so device investigations can use Safari Web Inspector. Release keeps the WebKit default. | Factory XCTest plus Debug and Release simulator builds. |
| `upgradeKnownHostsToHTTPS`, `limitsNavigationsToAppBoundDomains` | WebKit defaults. Configured instance input independently requires HTTPS; the shell does not opt into App-Bound Domains. | Simulator build and origin policy tests. |
| `defaultWebpagePreferences.preferredContentMode` | WebKit `.recommended`, which selects the iPhone-appropriate content mode. | API ledger and simulator build. |
| `defaultWebpagePreferences.allowsContentJavaScript` | WebKit `true`; the Workbench requires JavaScript. | The simulator capability probe executes JavaScript. |
| `defaultWebpagePreferences.lockdownModeEnabled` | System/WebKit default. The shell does not weaken Lockdown Mode. | System-owned behavior. |
| `defaultWebpagePreferences.preferredHTTPSNavigationPolicy` | WebKit keep-as-requested default; configured instance input already requires HTTPS and known-host upgrades remain enabled. | Origin parsing tests. |
| `minimumFontSize`, `fraudulentWebsiteWarningEnabled`, `textInteractionEnabled`, `siteSpecificQuirksModeEnabled`, `inactiveSchedulingPolicy` | WebKit defaults. These preserve browser text interaction, compatibility, fraud warnings, and lifecycle scheduling. | API ledger and simulator build. |
| `allowsAirPlayForMediaPlayback`, `allowsPictureInPictureMediaPlayback` | WebKit `true` defaults. | API ledger; media UI is a device check. |
| `suppressesIncrementalRendering`, `ignoresViewportScaleLimits` | WebKit `false` defaults. Pages render incrementally and retain author viewport limits. | API ledger and simulator build. |
| `allowsInlinePredictions`, `supportsAdaptiveImageGlyph`, `writingToolsBehavior` | WebKit defaults. The shell does not add text-system behavior beyond Safari/WebKit. | API ledger; keyboard behavior is a device check. |
| Managed WebView `scrollView.isScrollEnabled`, `bounces`, `contentInsetAdjustmentBehavior`, content offset, and keyboard viewport | WebKit defaults apply independently in every primary or child WebView until its Workbench AppShell requests `viewport.setOuterScrollLocked({locked: true})`. While locked, the shell disables that WebView's outer scrolling and bounce and resets its outer offset on keyboard frame/hide notifications. Unmount, `pagehide`, or that WebView's commit restores the captured defaults; `pageshow` reacquires the lock after a WebKit page-cache restoration. The shell never sets `contentInsetAdjustmentBehavior`, because WKWebView stops deriving it from `viewport-fit` once an app sets it. The bridge script declares `viewport-fit=cover` for a document while it holds the lock, so WebKit uses `.never` for the locked Workbench, whose `env(safe-area-inset-*)` values are the window insets, and `.always` for every document without a cover declaration, such as authentication pages. The primary spans the container top and bottom and a child the safe area; the bottom of each moves to the converted docked keyboard top only while that frame overlaps the safe-area bottom. Geometry and app lifecycle changes recompute from the last frame; a window-bounds change invalidates it before recomputation. The Web UI disables `useIosKeyboardInset` only when `keyboardResizesViewport: 1` is present. | Notification-driven geometry and animation XCTest for both resting edges; key-window XCTest on a Dynamic Island simulator for the primary and child top and resting edges across a docked keyboard, the SwiftUI browser route's top and bottom, a presented child below its navigation bar, a locked Workbench fixture's top and bottom `env()` padding and zero adjusted insets, the automatic insets after unlock and on instance and authentication documents after a commit, and the bundled page's single padding; per-WebView state XCTest; focused Web lifecycle and capability tests; Debug/Release builds. |
| iOS 27 webpage preferences (`securityRestrictionMode`, `alternateRequest`, `overrideReferrer`, `allowsJSHandleCreationInPageWorld`, `globalPrivacyControlEnabled`) | WebKit defaults. No Phase 0a requirement justifies changing requests, referrers, page-world handles, or privacy signals. | Documentation-only until the pinned toolchain exposes the full iOS 27 set. |

## Web platform outcomes

| Capability | Outcome |
| --- | --- |
| New windows and `window.close()` | Supported by `createWebViewWith` and `webViewDidClose`; every ordinary target opens in a child WebView, children recursively use the same delegates, and the toolbar exposes host, Done, and Open in Safari. The Safari action is enabled only for HTTP(S) URLs. |
| JavaScript `alert`, `confirm`, and `prompt` | Supported with native dialogs and exactly-once completion on every exit path. |
| Instance HTTPS, matching `blob:`, and instance-frame `data:` downloads | Supported through the action/response policies and `WKDownloadDelegate`; non-displayable MIME and attachment responses use the same rule. |
| File selection, camera, and photo saving | Uses WebKit's Safari-style picker and the system activity sheet. `NSCameraUsageDescription` and `NSPhotoLibraryAddUsageDescription` are localized. |
| External application schemes | Trusted top-level contexts use `UIApplication.open`; subframes cancel them without leaving the app. |
| Web content process termination | The primary WebView reloads the configured instance; a terminated child closes. |
| `navigator.share` | Available in the Xcode 26.6 simulator secure-context probe: `{"secureContext":true,"navigatorShare":"function"}`. Presentation remains system-owned. |
| Clipboard | Uses WebKit edit/clipboard behavior. The Web UI feature-detects writes, falls back to `execCommand`, and reports blocked reads. No native clipboard bridge is added. |
| Notification and Service Worker | Browser Service Worker and `PushManager` delivery remain unsupported in this shell. Native notification controls use the `push` bridge and the instance's existing authorization, preference, and reconciliation paths. |
| Fullscreen | Deferred. Phase 0a leaves `isElementFullscreenEnabled` at WebKit's default. |
| Installation guidance | `InstallHint` and Show Page "Add to Home Screen" guidance are hidden in the native shell. |
| PWA app launch suppression | Not reused. New-context launches use the shell's recursive child WebViews. |
| PWA loopback guard | Retained for `localhost`, `127.0.0.0/8`, and `::1`; same-origin `_blank` rewriting is disabled so WebKit creates a child context. |
| PWA media download sharing | Not reused. Download anchors reach `WKDownload` and the native activity sheet. |
| PWA remote-auth deferral | Not reused. Automatic login follows the existing WebView authentication-origin flow. |
| Haptics | Hold-to-talk start/release/cancel and accepted typed/attachment sends use bridge haptics. Browser contexts retain `navigator.vibrate` as the hold fallback. |
| Voice permission probe | Hold-to-talk reads `voice.permission()` first in the native shell, then retains the browser Permissions API fallback elsewhere. |
| Account presentation | `accountPresentation: 1` opens the bundled account surface over the runtime, and Account Back returns to the same document. Capability 2 lets the trusted primary AppShell declare its own account action. Current Web UI renders the Account-style initial avatar in `MobileBrandHeader` and uses no native chrome; an older Web UI receives a 56-point WebView-external fallback bar after load, with the account initial and Account/Change Instance actions. |
| Runtime back gesture | The primary WebView traverses only selected-instance history. SPA `pushState` entries participate, including same-URL entries synchronized by the internal trusted-main-frame history signal. Authentication history disables the gesture, and forced cross-origin back-forward navigation is cancelled. |
| Workbench outer-scroll lock | An AppShell in either primary or child requests the `viewport: 1` lock for its own WebView while mounted and releases it on unmount or `pagehide`. Authentication pages, Show Pages, shared pages, previews, errors, and other ordinary documents keep WebKit scrolling by default. |

## Primary and child behavior inventory

The tables below cover primary and child WebViews while they load the configured
instance origin. Authentication and external child documents do not receive the
bridge and do not run these Web UI branches.

### Native behavior

| Area | Primary instance WebView | Child instance WebView | Reason for any difference |
| --- | --- | --- | --- |
| Construction and configuration | Shared `WebViewFactory`; native bridge, message handler, delegates, extent constraints, keyboard layout, and scroll-lock state are installed together. | Same factory using WebKit's supplied configuration, which preserves `window.opener` and the shared user content controller. | None. |
| Extent and keyboard viewport | Spans the container top and bottom, so the page covers the status bar and the Home indicator strip and reserves them through `env()`; a docked keyboard ends it at the keyboard top. | Spans the safe area below its navigation bar, with the same docked-keyboard handling. | A child shows arbitrary origins under native chrome, which do not reserve the safe areas. |
| Status bar | Its reported page scheme sets the status bar text while it is the newest representable controller. | Its report is rejected; the status bar keeps following the primary page under the sheet. | The primary page is what the status bar sits over. |
| Outer-scroll lock | The instance main frame can lock only its own WebView; commit resets only that state. | Same, with independent state. | None. |
| Navigation policy | Instance/authentication stay in the primary; other top-level HTTP(S) destinations open in `SFSafariViewController`. Interactive back is enabled only for an immediate instance-origin item, and cross-origin back-forward traversal is rejected. | Main-frame HTTP(S) navigation stays in the child; the primary interactive gesture remains disabled. | The primary owns selected-instance history admission; a child is already the requested browsing context and keeps its explicit close/back controls. |
| Subframes and external application schemes | WebKit handles ordinary subframes; external application schemes are cancelled. Trusted top-level application schemes use the system. | Same. | None. |
| Dialogs, downloads, microphone, and nested windows | Shared delegates and origin checks. | Same, including recursive child creation. | None. |
| Window chrome and close | A current AppShell renders an Account-style avatar in `MobileBrandHeader`, registers that reachable action, and receives no native bar. An old Web UI receives a 56-point top fallback after the load grace period; it sits outside the WebView and uses the account initial. | Host title, Done, and Open in Safari own the child browsing-context lifecycle; `window.close()` closes its subtree. | The primary fallback preserves account access without covering Web controls; a child needs an explicit close affordance and external-browser escape. |
| Content-process termination | Closes all children and reloads the configured instance URL. | Closes the terminated child subtree. | Reloading a popup after process loss would invent navigation state; the primary has one configured recovery URL. |
| Loading callbacks | Drives the root loading/error state. | Does not change root loading/error state. | Child loading is contained by its own presented window. |

### Web UI decisions

| Web decision | Signal | Primary instance WebView | Child instance WebView |
| --- | --- | --- | --- |
| `useIosKeyboardInset` | `keyboardResizesViewport: 1` | Browser correction disabled; UIKit owns viewport resizing. | Same. An older shell without the capability keeps the correction in both. |
| Workbench outer-scroll lock | `viewport: 1` | Mounted AppShell locks its own WebView. | Same. |
| Installation prompts | `isNativeShell()` | `InstallHint` and Show Page Add to Home Screen are hidden. | Same. |
| Web Push surfaces | `push: 1` | Browser `PushManager` is omitted; notification controls use the native push bridge and the existing Web-owned subscription/reconciliation flow. | Same. |
| Haptics | `haptics: 1` | Native haptics for hold-to-talk and accepted sends. | Same. |
| Voice permission probe | `voice: 1` | Reads native permission state before browser fallback. | Same. |
| Account presentation | `accountPresentation: 1/2` | Capability 1 opens the bundled account surface over the runtime. Capability 2 renders and registers the current AppShell header avatar, suppressing the native fallback; older Web UI gets the delayed fallback bar. | Capability 1 remains available, but capability-2 registration is rejected because child content cannot control primary chrome. |
| `appLaunch` new-context suppression | `isNativeShell()` | iOS standalone suppression is disabled; `window.open` reaches native child creation. | Same for nested windows. |
| `PwaLoopbackLinkGuard` | `isNativeShell()` | Loopback blocking remains; same-origin `_blank` rewriting is disabled. | Same. |
| `downloadMedia` | `isNativeShell()` | PWA Web Share interception is disabled; admitted anchors reach `WKDownload`. | Same. |
| `remoteAuth` | `isNativeShell()` | Automatic login is not deferred for a PWA user gesture. | Same. |
| `ShowPageShareControl` | `isNativeShell()` | System Web Share remains feature-detected; Add to Home Screen is hidden. | Same. |
| PWA route-memory restoration in `App.tsx` | `isNativeShell()` | The iOS standalone-only restoration path is disabled. | Same. |

## Verification

| Environment | Coverage |
| --- | --- |
| CI: Xcode 26.6, iOS 26.4.1 simulator | Authoritative `ios-shell` build and serialized full XCTest, including cold WebKit transport startup, redirect isolation, cookie behavior, bundled-page interaction, and notification-driven keyboard viewport invariants. |
| Local: Xcode 27.2, iOS 26.4.1 simulator | Focused Debug XCTest for notification-driven viewport geometry and bundled onboarding, plus real-WebKit Home quick-task, conversation composer, background/foreground, and bundled OTP explicit-dismissal checks. Each path returned to its safe-area baseline. Interactive pull-down dismissal remains an owner device retest. |
| Local: Xcode 27.2, iOS 27.2 simulator | Focused Home quick-task check with the software keyboard; explicit dismissal returned the tab bar to its safe-area baseline. |

The initial hidden-WebView readiness bound is 30 seconds so a cold WebContent
process on the CI runtime can start without leaving account operations
unbounded. Request and best-effort logout deadlines remain independently
bounded.

Runtime teardown has one ownership rule: once an account, instance, or logout
transition invalidates a runtime controller, no callback may recreate or use
its primary or child WebView. Delegate, bridge, KVO, observer, delayed-work,
download, and popup paths use retained-only references and become no-ops or
cancel their completion after invalidation. The AppModel transition regression
drives this rule through an instance switch made from the account page that a
real `app.showAccount` bridge call opened, before delivering
late WebKit callbacks. Active downloads are cancelled and their temporary
destinations are removed before runtime state is released. Behavioral WebKit
tests drive late navigation, UI-delegate, bridge, observer, delayed-work, and
download activity after the runtime is invalidated.

- Native XCTest covers bridge policy, Account over the runtime (Back keeps the
  same controller and document; a switch or sign-out releases it), the hidden
  transport starting only after that release, account
  action registration and fallback timing, selected-instance SPA history,
  authentication-history rejection, transport lifecycle, stage-accurate
  failure mapping, bounded hanging
  readiness/GET/DELETE, cold selection ownership, bundled
  full-bleed/keyboard constraints, named-store failure/relaunch/orphan cleanup,
  release of runtime primary and child WebViews before removal, the exact
  code-only OTP `redirect_to`, and real WebKit cookie behavior against a
  hermetic local server. Second-factor coverage
  reads the `403` gate after the email code and on a session read, posts codes
  and a passkey assertion with the trusted Origin, maps throttling and
  rejection, stores the re-issued cookie, keeps the store and selection until
  the factor passes, and drives the bundled page through the bridge.
- Web UI: focused Vitest coverage for the bundled OTP/picker/account flow,
  every two-step verification state,
  provider rate-limit cooldown across send/resend/verify, send-only success
  cooldown, bridge adapter, haptics, Workbench outer-scroll lifecycle, keyboard
  ownership, hidden Web Push/installation surfaces, and all five PWA decisions;
  production Workbench and bundled-mobile TypeScript/Vite builds.
- Simulator screenshots and the machine-readable fixture report are stored
  outside the repository and linked from the PR description. XCUITest evidence
  covers scrolling to and selecting the final instance plus OTP button access
  with the keyboard on a compact viewport; light and dark screenshots verify
  full-bleed safe-area backgrounds.
- The owner manually verified production Supabase email OTP, a real account
  session, and online/offline instance discovery on the iOS 27.2 simulator.
  First-instance consent, hosted refresh competition, and physical-device
  keyboard/autofill remain unverified.

## Local build

Generate the bundled React application before opening or building the Xcode
project:

```sh
cd ui
npm run build:mobile
```

The generated `ios/Avibe/Resources/MobileApp` directory is ignored build
output. Xcode verifies that `mobile.html` and the asset directory exist and
fails with the command above when they do not; Xcode never invokes Node or npm.
The CI verifier additionally checks every asset referenced by the entry.

Then open `ios/Avibe.xcodeproj` in Xcode, select an iPhone simulator, and run
the shared `Avibe` scheme. The command-line build does not need an Apple
account or signing identity:

```sh
xcodebuild \
  -project ios/Avibe.xcodeproj \
  -scheme Avibe \
  -configuration Debug \
  -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO \
  build
```

To run XCTest, use any installed iPhone simulator:

```sh
xcodebuild \
  -project ios/Avibe.xcodeproj \
  -scheme Avibe \
  -destination 'platform=iOS Simulator,name=<installed iPhone>,OS=latest' \
  CODE_SIGNING_ALLOWED=NO \
  test
```

Tests and development builds must use disposable instance URLs, `loadHTMLString`
fixtures, or local fixture servers. Do not point automated tests at a real
Avibe instance.

## CI

`.github/workflows/ios-shell.yml` runs for iOS changes and the complete browser
input set declared by `BROWSER_CHECKOUT` (except the Docker packaging file).
A structural test derives both pull-request and push path lists from that
single source, so new shared or out-of-tree UI inputs cannot silently skip the
mobile build. The job installs the repository-pinned Node version and lockfile,
runs `build:mobile`, verifies the generated entry and assets, then pins the same
Xcode 26.6 toolchain used by the desktop native workflow. It builds against the
iOS Simulator SDK without signing and runs the XCTest navigation, new-window,
download, bundled-page, and WebKit capability contracts on an available iPhone
simulator. Debug and Release use the same generated folder resource; the
bundled-page XCTest resolves `MobileApp` from the built application bundle and
executes its React entry through `avibe-app://bundle`.

## TestFlight

PR builds never sign or upload. `.github/workflows/ios-testflight.yml` is the
only signing path, started by hand (`gh workflow run ios-testflight.yml`, or
Actions → ios-testflight → Run workflow) and only from `master`. In one job it:

1. runs `npm ci`, `npm run build:mobile`, and the bundle verification;
2. imports the Apple Distribution identity into a temporary keychain and
   installs the App Store profile (`ios/scripts/testflight.py prepare`), after
   checking that the profile is a current App Store profile for
   `68B2SQ6YK3.bot.avibe.ios`, grants Associated Domains, and was issued for
   that certificate;
3. archives the Release configuration with manual signing, using
   `<run number>.<attempt>` as `CFBundleVersion`;
4. checks the archived app's bundle, build number, encryption declaration, and
   distribution entitlements (`verify-archive`);
5. exports and uploads with the App Store Connect API key;
6. waits for App Store Connect to process the build and adds it to the internal
   group `Avibe Internal` (`distribute`); internal testing needs no App Review;
7. always removes the keychain, profile, and key (`cleanup`).

`Info.plist` declares `ITSAppUsesNonExemptEncryption = false`: the app uses
only the system's HTTPS and passkey cryptography, so builds need no export
compliance answer before testers can install them.

Repository secrets, set once by the Apple Account Holder:

| Secret | Content |
| --- | --- |
| `IOS_APPLE_TEAM_ID` | `68B2SQ6YK3` |
| `IOS_DISTRIBUTION_P12_BASE64`, `IOS_DISTRIBUTION_P12_PASSWORD` | the Apple Distribution certificate and key |
| `IOS_APP_STORE_PROFILE_BASE64` | the "Avibe iOS App Store" profile |
| `IOS_APP_STORE_CONNECT_KEY_ID`, `IOS_APP_STORE_CONNECT_ISSUER_ID`, `IOS_APP_STORE_CONNECT_PRIVATE_KEY` | a dedicated App Store Connect API key |

Changing a capability on the App ID invalidates the profile. Regenerate it in
the Apple Developer account and replace `IOS_APP_STORE_PROFILE_BASE64`; the
workflow refuses an expired profile, or one without Associated Domains, before
it builds. The profile was last regenerated on 2026-11-07, after Associated
Domains was enabled.
