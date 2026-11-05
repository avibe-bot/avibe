# Avibe iOS shell

This directory contains the thin SwiftUI shell for the Avibe Workbench. The
shell stores one configured HTTPS instance origin, loads it in a persistent
`WKWebView`, and owns the platform boundaries that browser content cannot own:
navigation handoff, downloads, native dialogs, and microphone permission.

Phase 0a deliberately keeps the Web composer. The shell and Web UI ship the
versioned `avibeNative` bridge together, and the same native-shell marker
coordinates keyboard ownership so native and Web resizing cannot both run.

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
- Cookies and website data use `WKWebsiteDataStore.default()` and survive app
  launches through WebKit's default persistent store.
- The primary WebView is pinned to `keyboardLayoutGuide`, disables automatic
  content insets and outer scrolling, and resets its outer offset as the
  keyboard changes. `isNativeShell()` disables the Web UI's
  `useIosKeyboardInset` correction in the same release. Child WebViews retain
  WebKit scrolling defaults.
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

The PR 2 method surface is fixed:

| Capability | Method | Result |
| --- | --- | --- |
| `app: 1` | `app.info()` | `{contract, platform, shellVersion, capabilities}` |
| `app: 1` | `app.openExternal({url})` | `{opened}` for `http`/`https` URLs with a host and `mailto`/`tel` URLs with a target |
| `haptics: 1` | `haptics.impact({style})` | `null`; styles are `light`, `medium`, `heavy`, `soft`, or `rigid` |
| `haptics: 1` | `haptics.notify({type})` | `null`; types are `success`, `warning`, or `error` |
| `haptics: 1` | `haptics.selection()` | `null` |
| `voice: 1` | `voice.permission()` | `{state: "granted" | "denied" | "prompt"}` without prompting |

Replies use `{ok: true, result}` or
`{ok: false, error: {code, message}}`. The compiled errors are
`invalid_request`, `invalid_argument`, `forbidden`, and `unsupported`.
Unknown methods never expand the native surface.

The production Supabase Auth origin is an explicit primary-navigation
allowlist entry. If Avibe Cloud moves to another Supabase project, update that
origin and its navigation test in the same change.

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
| `didCommit` | Uses WebKit default; the shell has no commit-time behavior. | API ledger plus simulator build. |
| `didFinish` | Implemented. Completes primary loading state. | Simulator build. |
| `didFail` | Implemented. Reports non-cancellation committed failures. | Simulator build; failure UI is a manual check. |
| `didReceive challenge` | Implemented with `performDefaultHandling`. The shell adds no certificate, HTTP-auth, or client-certificate override. | Standard HTTPS loads in simulator CI; custom challenge UI is unsupported. |
| `webViewWebContentProcessDidTerminate` | Implemented. The primary view closes children and reloads the configured instance; a child closes itself. | Simulator build; forced termination is a manual simulator/device check. |
| `authenticationChallenge:shouldAllowDeprecatedTLS` | Intentionally not implemented, so deprecated TLS is not admitted by shell policy. | API ledger; no deprecated-TLS fixture is maintained. |
| `navigationAction:didBecome download` | Implemented and attaches the shared download context/delegate. | Simulator build and download policy tests. |
| `navigationResponse:didBecome download` | Implemented and attaches the shared download context/delegate. | Simulator build and download policy tests. |
| `shouldGoTo backForwardListItem` | Uses WebKit default. JavaScript and back-forward-list navigation are not given a second shell policy. | API ledger; interactive traversal is a manual check. |
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
| `websiteDataStore` | WebKit's default persistent data store. Cookies and sessions survive app launches without a shell override. | Simulator build. |
| `userContentController` | The root installs the document-start `avibeNative` script and reply handler. Children receive WebKit's exact configuration, preserving the same controller and `window.opener`; each message still repeats the main-frame and exact-origin check. | XCTest covers instance main-frame discovery and calls, subframe absence, external-origin absence, and unknown-method rejection. |
| `applicationNameForUserAgent` | `AvibeiOS/<CFBundleShortVersionString>`, appended by WebKit to its standard user agent. | Configuration XCTest and Debug/Release simulator builds. |
| `allowsInlineMediaPlayback`, `mediaTypesRequiringUserActionForPlayback` | WebKit defaults. Phase 0a adds no media playback policy. | API ledger; playback is a device check. |
| `javaScriptCanOpenWindowsAutomatically` | WebKit default. WebKit decides whether a script may request a new context; accepted requests use the child-WebView rule. | New-window policy tests and simulator build. |
| `allowsBackForwardNavigationGestures`, `allowsLinkPreview`, `dataDetectorTypes` | WebKit defaults. The shell adds no gesture, preview, or data-detector policy. | API ledger; interactions are manual checks. |
| `isElementFullscreenEnabled` | WebKit default. Fullscreen product work remains deferred. | API ledger; the deferral is recorded below. |
| `isInspectable` | WebKit default in both Debug and Release. Temporary simulator fixtures do not ship an inspectability override. | Debug and Release simulator builds. |
| `upgradeKnownHostsToHTTPS`, `limitsNavigationsToAppBoundDomains` | WebKit defaults. Configured instance input independently requires HTTPS; the shell does not opt into App-Bound Domains. | Simulator build and origin policy tests. |
| `defaultWebpagePreferences.preferredContentMode` | WebKit `.recommended`, which selects the iPhone-appropriate content mode. | API ledger and simulator build. |
| `defaultWebpagePreferences.allowsContentJavaScript` | WebKit `true`; the Workbench requires JavaScript. | The simulator capability probe executes JavaScript. |
| `defaultWebpagePreferences.lockdownModeEnabled` | System/WebKit default. The shell does not weaken Lockdown Mode. | System-owned behavior. |
| `defaultWebpagePreferences.preferredHTTPSNavigationPolicy` | WebKit keep-as-requested default; configured instance input already requires HTTPS and known-host upgrades remain enabled. | Origin parsing tests. |
| `minimumFontSize`, `fraudulentWebsiteWarningEnabled`, `textInteractionEnabled`, `siteSpecificQuirksModeEnabled`, `inactiveSchedulingPolicy` | WebKit defaults. These preserve browser text interaction, compatibility, fraud warnings, and lifecycle scheduling. | API ledger and simulator build. |
| `allowsAirPlayForMediaPlayback`, `allowsPictureInPictureMediaPlayback` | WebKit `true` defaults. | API ledger; media UI is a device check. |
| `suppressesIncrementalRendering`, `ignoresViewportScaleLimits` | WebKit `false` defaults. Pages render incrementally and retain author viewport limits. | API ledger and simulator build. |
| `allowsInlinePredictions`, `supportsAdaptiveImageGlyph`, `writingToolsBehavior` | WebKit defaults. The shell does not add text-system behavior beyond Safari/WebKit. | API ledger; keyboard behavior is a device check. |
| Primary `scrollView.isScrollEnabled`, `bounces`, `contentInsetAdjustmentBehavior`, and content offset | The primary WebView disables outer scrolling and bounce, uses `.never` inset adjustment, pins its bottom to `keyboardLayoutGuide.topAnchor`, and resets outer offset on keyboard frame/hide notifications. The Web UI disables `useIosKeyboardInset` only when `isNativeShell()` is true. Child WebViews retain WebKit defaults. | Focused Web hook test, source inspection, simulator keyboard fixture, and Debug/Release builds. |
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

## Phase 0a PR 2 verification

- Xcode 27.2 simulator: Debug and Release no-sign builds, XCTest bridge policy
  and runtime discovery, plus the hermetic keyboard/bridge fixture.
- CI remains authoritative on Xcode 26.6 and runs `lint` plus `ios-shell`.
- Web UI: focused Vitest coverage for the bridge adapter, haptics, keyboard
  ownership, hidden Web Push/installation surfaces, and all five PWA decisions;
  production TypeScript/Vite build.
- Simulator screenshots and the machine-readable fixture report are stored
  outside the repository and linked from the PR description.

## Local build

Open `ios/Avibe.xcodeproj` in Xcode, select an iPhone simulator, and run the
shared `Avibe` scheme. The command-line build does not need an Apple account or
signing identity:

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

`.github/workflows/ios-shell.yml` runs only when `ios/**` or the workflow itself
changes. It pins the same Xcode 26.6 toolchain used by the desktop native
workflow, builds against the iOS Simulator SDK without signing, and runs the
XCTest navigation, new-window, download, and WebKit capability contracts on an
available iPhone simulator.

## TestFlight prerequisites

PR builds never sign or upload. A later owner-authorized TestFlight workflow
still needs the one-time setup held by the Apple Account Holder:

- the registered `bot.avibe.ios` identifier and App Store Connect app record;
- an Apple Distribution certificate and password;
- an App Store provisioning profile for the app target;
- the Apple Team ID;
- a dedicated App Store Connect API key ID, issuer ID, and private key.

Those credentials belong in a protected GitHub Environment. They are not
required for simulator development or this PR's CI.
