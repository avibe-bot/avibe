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
- Primary and child WebViews are created through one factory and pinned to
  `keyboardLayoutGuide`. Outer scrolling and automatic content insets use
  WebKit defaults until an instance-origin Workbench document requests
  `viewport.setOuterScrollLocked({locked: true})` for its own WebView. Each
  WebView keeps independent lock state. The lock is released on AppShell
  teardown, `pagehide`, or that WebView's next main-frame navigation commit,
  and reacquired on `pageshow` when WebKit restores the Workbench from its page
  cache. The Web UI disables `useIosKeyboardInset` only when the bridge
  declares `keyboardResizesViewport: 1`.
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
`forbidden`. Account-action registration requires the source to be the
selected-instance primary WebView; a child receives `forbidden`.
The document-start bridge also reports the internal
`navigation.historyChanged` signal after `history.pushState` or
`history.replaceState`. It is not an advertised capability and carries no
history data. The trusted primary-main-frame handler uses it only to reread
WebKit's current and immediate back items; the controller rejects child
WebViews, and those native origin checks remain the gesture authority.

The PR 2 method surface is fixed:

| Capability | Method | Result |
| --- | --- | --- |
| `accountPresentation: 1` | `app.showAccount()` | `null`; closes the runtime surface and opens the bundled account page |
| `accountPresentation: 2` | `app.setAccountActionAvailable({available})` | `null`; primary Workbench display hint used to suppress or restore the old-Web native fallback |
| `app: 1` | `app.info()` | `{contract, platform, shellVersion, capabilities}` |
| `app: 1` | `app.openExternal({url})` | `{opened}` for `http`/`https` URLs with a host and `mailto`/`tel` URLs with a target |
| `haptics: 1` | `haptics.impact({style})` | `null`; styles are `light`, `medium`, `heavy`, `soft`, or `rigid` |
| `haptics: 1` | `haptics.notify({type})` | `null`; types are `success`, `warning`, or `error` |
| `haptics: 1` | `haptics.selection()` | `null` |
| `keyboardResizesViewport: 1` | No method | Declares that UIKit shrinks this WebView's viewport with `keyboardLayoutGuide` |
| `voice: 1` | `voice.permission()` | `{state: "granted" | "denied" | "prompt"}` without prompting |
| `viewport: 1` | `viewport.setOuterScrollLocked({locked})` | `null`; applies to the calling managed WebView |

Replies use `{ok: true, result}` or
`{ok: false, error: {code, message}}`. The compiled errors are
`invalid_request`, `invalid_argument`, `forbidden`, and `unsupported`.
Unknown methods never expand the native surface.

The production Supabase Auth origin is an explicit primary-navigation
allowlist entry. If Avibe Cloud moves to another Supabase project, update that
origin and its navigation test in the same change.

## Mobile account onboarding

The app bundles the approved `mobile-app/*` React flow and exposes a separate
`window.avibeMobile` bridge only to its top-level
`avibe-app://bundle` document. A read-only `WKURLSchemeHandler` serves the
Vite build generated from the same source commit so ES modules, styles, and
image assets share one fixed local origin. Its fixed
surface is `session.bootstrap`, `auth.requestOtp`, `auth.verifyOtp`,
`session.refresh`, `session.selectInstance`, `session.logout`, and the closed
install-guide `app.openExternal` allowlist. Instance documents, child windows,
Show Pages, and iframes do not receive these handlers.

The bundled surface fills the container safe area so its gradient reaches the
status bar and Home indicator. Its shared `h-dvh` viewport owns vertical
scrolling inside the mobile document lock, while the content column applies
top and bottom safe-area padding and grows from `min-height: 100%`. Every
screen therefore keeps long content reachable, including OTP with the software
keyboard, long email addresses, offline recovery, account details, and large
instance lists. UIKit still pins the WebView bottom to
`keyboardLayoutGuide.topAnchor`.

The hidden account transport is a native-owned `WKWebView` with no message
handler. It loads only the configured control-plane auth configuration and runs
fixed scripts in `WKContentWorld.defaultClient`: public Supabase OTP requests,
the existing `/auth/callback`, and the mobile session GET/DELETE endpoints. The
isolated world is defense in depth, not a separate security process. The actual
boundary comes from the fixed control-plane origin, narrow compiled scripts,
no page bridge, and lifecycle separation from runtime WebViews. Access and
refresh tokens are transient script arguments and are never returned to the
bundled page or instance content.

Readiness navigation has a native 10-second deadline. Fixed account scripts
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
Returning to Account first stops the primary and child runtime WebViews; the
hidden transport is created only when the bundled account controller appears.
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
Signed-out bootstrap, a stored-selection account mismatch, and failed OTP
verification also rotate to a fresh store before another account attempt.
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
cross-origin redirect checks. When `ios-shell` XCTest fails, CI uploads
`AvibeTests.xcresult` as `ios-shell-xcresult`.

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
| Managed WebView `scrollView.isScrollEnabled`, `bounces`, `contentInsetAdjustmentBehavior`, and content offset | WebKit defaults apply independently in every primary or child WebView until its Workbench AppShell requests `viewport.setOuterScrollLocked({locked: true})`. While locked, the shell disables that WebView's outer scrolling and bounce, uses `.never` inset adjustment, and resets its outer offset on keyboard frame/hide notifications. Unmount, `pagehide`, or that WebView's commit restores the captured defaults; `pageshow` reacquires the lock after a WebKit page-cache restoration. Every managed WebView is pinned to `keyboardLayoutGuide.topAnchor`, and the Web UI disables `useIosKeyboardInset` only when `keyboardResizesViewport: 1` is present. | Factory/constraint and per-WebView state XCTest, focused Web lifecycle and capability tests, simulator primary/child keyboard fixtures, and Debug/Release builds. |
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
| Notification and Service Worker | Unsupported in this shell configuration. Browser Web Push controls and readiness prompts are hidden when `isNativeShell()` is true; native push is Phase 1. |
| Fullscreen | Deferred. Phase 0a leaves `isElementFullscreenEnabled` at WebKit's default. |
| Installation guidance | `InstallHint` and Show Page "Add to Home Screen" guidance are hidden in the native shell. |
| PWA app launch suppression | Not reused. New-context launches use the shell's recursive child WebViews. |
| PWA loopback guard | Retained for `localhost`, `127.0.0.0/8`, and `::1`; same-origin `_blank` rewriting is disabled so WebKit creates a child context. |
| PWA media download sharing | Not reused. Download anchors reach `WKDownload` and the native activity sheet. |
| PWA remote-auth deferral | Not reused. Automatic login follows the existing WebView authentication-origin flow. |
| Haptics | Hold-to-talk start/release/cancel and accepted typed/attachment sends use bridge haptics. Browser contexts retain `navigator.vibrate` as the hold fallback. |
| Voice permission probe | Hold-to-talk reads `voice.permission()` first in the native shell, then retains the browser Permissions API fallback elsewhere. |
| Account presentation | `accountPresentation: 1` opens the bundled account surface. Capability 2 lets the trusted primary AppShell declare its own account action. Current Web UI renders the Account-style initial avatar in `MobileBrandHeader` and uses no native chrome; an older Web UI receives a 56-point WebView-external fallback bar after load, with the account initial and Account/Change Instance actions. |
| Runtime back gesture | The primary WebView traverses only selected-instance history. SPA `pushState` entries participate, including same-URL entries synchronized by the internal trusted-main-frame history signal. Authentication history disables the gesture, and forced cross-origin back-forward navigation is cancelled. |
| Workbench outer-scroll lock | An AppShell in either primary or child requests the `viewport: 1` lock for its own WebView while mounted and releases it on unmount or `pagehide`. Authentication pages, Show Pages, shared pages, previews, errors, and other ordinary documents keep WebKit scrolling by default. |

## Primary and child behavior inventory

The tables below cover primary and child WebViews while they load the configured
instance origin. Authentication and external child documents do not receive the
bridge and do not run these Web UI branches.

### Native behavior

| Area | Primary instance WebView | Child instance WebView | Reason for any difference |
| --- | --- | --- | --- |
| Construction and configuration | Shared `WebViewFactory`; native bridge, message handler, delegates, safe-area constraints, keyboard layout, and scroll-lock state are installed together. | Same factory using WebKit's supplied configuration, which preserves `window.opener` and the shared user content controller. | None. |
| Keyboard viewport | Bottom is pinned to `keyboardLayoutGuide.topAnchor`. | Same. | None. |
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
| Web Push surfaces | `isNativeShell()` | Setup gap, Settings toggle, and notification menu omit browser push. | Same. |
| Haptics | `haptics: 1` | Native haptics for hold-to-talk and accepted sends. | Same. |
| Voice permission probe | `voice: 1` | Reads native permission state before browser fallback. | Same. |
| Account presentation | `accountPresentation: 1/2` | Capability 1 opens the bundled account surface. Capability 2 renders and registers the current AppShell header avatar, suppressing the native fallback; older Web UI gets the delayed fallback bar. | Capability 1 remains available, but capability-2 registration is rejected because child content cannot control primary chrome. |
| `appLaunch` new-context suppression | `isNativeShell()` | iOS standalone suppression is disabled; `window.open` reaches native child creation. | Same for nested windows. |
| `PwaLoopbackLinkGuard` | `isNativeShell()` | Loopback blocking remains; same-origin `_blank` rewriting is disabled. | Same. |
| `downloadMedia` | `isNativeShell()` | PWA Web Share interception is disabled; admitted anchors reach `WKDownload`. | Same. |
| `remoteAuth` | `isNativeShell()` | Automatic login is not deferred for a PWA user gesture. | Same. |
| `ShowPageShareControl` | `isNativeShell()` | System Web Share remains feature-detected; Add to Home Screen is hidden. | Same. |
| PWA route-memory restoration in `App.tsx` | `isNativeShell()` | The iOS standalone-only restoration path is disabled. | Same. |

## Verification

| Environment | Coverage |
| --- | --- |
| CI: Xcode 26.6, iOS 26.4.1 simulator | Authoritative `ios-shell` build and serialized full XCTest, including cold WebKit transport startup, redirect isolation, cookie behavior, and bundled-page interaction. |
| Local: Xcode 27.2, iOS 26.4.1 simulator | Debug and Release full XCTest, including the runtime teardown crash regression. |
| Local: Xcode 27.2, iOS 27.2 simulator | Debug and Release full XCTest without signing, including the runtime teardown crash regression, repeated serialized transport integration, compact keyboard viewport, and light/dark screenshots. |

The initial hidden-WebView readiness bound is 30 seconds so a cold WebContent
process on the CI runtime can start without leaving account operations
unbounded. Request and best-effort logout deadlines remain independently
bounded.

Runtime teardown has one ownership rule: once an account, instance, or logout
transition invalidates a runtime controller, no callback may recreate or use
its primary or child WebView. Delegate, bridge, KVO, observer, delayed-work,
download, and popup paths use retained-only references and become no-ops or
cancel their completion after invalidation. The AppModel transition regression
drives this rule through a real `app.showAccount` bridge call before delivering
late WebKit callbacks.

- Native XCTest covers bridge policy, runtime `app.showAccount`, account
  action registration and fallback timing, selected-instance SPA history,
  authentication-history rejection, transport lifecycle, stage-accurate
  failure mapping, bounded hanging
  readiness/GET/DELETE, cold selection ownership, bundled
  full-bleed/keyboard constraints, named-store failure/relaunch/orphan cleanup,
  release of runtime primary and child WebViews before removal, and real WebKit
  cookie behavior against a hermetic local server.
- Web UI: focused Vitest coverage for the bundled OTP/picker/account flow,
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

## TestFlight prerequisites

PR builds never sign or upload. A later owner-authorized TestFlight workflow
must run `npm ci`, `npm run build:mobile`, and the bundle verification in the
same job before archive or upload. It also needs the one-time setup held by the
Apple Account Holder:

- the registered `bot.avibe.ios` identifier and App Store Connect app record;
- an Apple Distribution certificate and password;
- an App Store provisioning profile for the app target;
- the Apple Team ID;
- a dedicated App Store Connect API key ID, issuer ID, and private key.

Those credentials belong in a protected GitHub Environment. They are not
required for simulator development or this PR's CI.
