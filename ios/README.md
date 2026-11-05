# Avibe iOS shell

This directory contains the thin SwiftUI shell for the Avibe Workbench. The
shell stores one configured HTTPS instance origin, loads it in a persistent
`WKWebView`, and owns the platform boundaries that browser content cannot own:
navigation, keyboard layout, and microphone permission.

Phase 0a deliberately keeps the Web composer. The `avibeNative` bridge is added
with its Web UI consumer in the next slice so neither side of that contract can
ship alone.

## Current contract

- Bundle identifier: `bot.avibe.ios`
- Display name: `Avibe`
- Deployment target: iOS 17
- Configured instance and authentication origins stay in top-level WebViews.
  The pinned Protected Vault origin may also load in an app-created popup.
  Direct primary-main-frame Vault navigation leaves the shell.
- Subframes use WebKit's browser behavior for HTTP(S), `about:blank`,
  `about:srcdoc`, `blob:`, and `data:` navigation. A subframe cannot launch
  `mailto:`, `tel:`, or another external application scheme.
- New windows requested by an instance browsing context open instance,
  authentication, and `about:blank` targets in closable child WebViews created
  with WebKit's supplied configuration. External HTTP(S) targets use
  `SFSafariViewController`, and `window.close()` closes the corresponding child
  WebView.
- Microphone capture is granted only to the configured instance main frame.
  Protected Vault, authentication, and arbitrary subframes receive neither
  microphone access nor a native bridge.
- Google authorization is intercepted with an email-code explanation.
- Other top-level HTTP(S) origins open in `SFSafariViewController`; external
  application schemes from a trusted top-level context use `UIApplication`.
- JavaScript alerts, confirmations, and prompts use native alerts. Their
  completion handlers are completed exactly once when the user responds or
  when the owning page, WebView, or content process goes away.
- Trusted instance HTTPS downloads, instance-origin `blob:` downloads, and
  instance-frame `data:` downloads use `WKDownload`, a unique temporary
  destination, and the system activity sheet. Other HTTP(S) download targets
  open in `SFSafariViewController`; all other download sources are rejected.
- Cookies and website data use `WKWebsiteDataStore.default()` and survive app
  launches.
- The WebView is pinned to `keyboardLayoutGuide`, has automatic content insets
  disabled, and does not expose an outer scrolling surface.
- `AppIcon.appiconset` contains one opaque 1024-by-1024 icon. It is a temporary
  upscale of the existing 512-pixel iOS Home Screen Vibey artwork and should be
  replaced when a native 1024-pixel source is delivered.

The production Supabase Auth origin is an explicit allowlist entry. If Avibe
Cloud moves to another Supabase project, update that origin and its navigation
test in the same change.

## WebKit API inventory

This inventory is organized by the finite WebKit API surface instead of by
individual Web UI call sites. It includes the iOS methods present through the
local iOS 27 SDK; iOS 27-only methods are documentation-only while CI remains
pinned to Xcode 26.6.

### `WKNavigationDelegate`

| Method | Handling | Verification |
| --- | --- | --- |
| `decidePolicyFor navigationAction` | Implemented. Download actions use the download admission rule. New contexts continue to `WKUIDelegate`. Subframes follow WebKit except external application schemes, which are cancelled. Main frames use the instance/authentication/Vault/Google/external classification. | Pure tests cover top-level destinations, `about:srcdoc`, blank, blob, data, cross-origin HTTPS, external schemes, and trusted/untrusted download sources. |
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
| `shouldGoTo backForwardListItem` | Uses WebKit default. JavaScript and swipe back/forward navigation are not given a second shell policy. | `allowsBackForwardNavigationGestures` is enabled; interactive traversal is a manual check. |
| `willSubmitForm` (iOS 27) | Uses WebKit default continuation. The pinned Xcode 26.6 toolchain does not expose this method. | Revisit when CI moves to an iOS 27 SDK. |

### `WKUIDelegate`

| Method | Handling | Verification |
| --- | --- | --- |
| `createWebViewWith` | Implemented. A trusted instance WebView creates closable child WebViews for instance, authentication, and `about:blank` targets with WebKit's exact configuration. External HTTP(S) uses Safari. Child-frame external application schemes are cancelled. | Pure new-window tests cover every destination class and untrusted containing main frames; simulator build covers recursive delegates. |
| `webViewDidClose` | Implemented and closes the matching child subtree. | Simulator build; `window.close()` is an interactive check. |
| `runJavaScriptAlertPanelWithMessage` | Implemented with an initiating-WebView `UIAlertController` and exactly-once completion. | Simulator build; interaction and removal cleanup are manual checks. |
| `runJavaScriptConfirmPanelWithMessage` | Implemented with OK/Cancel and exactly-once completion. | Simulator build; existing Web UI `confirm()` callers are manual checks. |
| `runJavaScriptTextInputPanelWithPrompt` | Implemented with a text field and exactly-once completion. | Simulator build; interaction is a manual check. |
| `requestMediaCapturePermissionFor` | Implemented. Only microphone access from the configured instance main frame is granted; camera and every other origin/frame are denied. | Origin policy is compiled in simulator CI; microphone persistence is a device check. |
| `requestDeviceOrientationAndMotionPermissionFor` | Uses WebKit/system prompting. The shell adds no broader permission grant. | Manual device check if a Web UI caller is added. |
| `shouldPreviewElement`, `previewingViewControllerForElement`, `commitPreviewingViewController` | Deprecated APIs are intentionally not implemented; current WebKit link-preview behavior is retained. | `allowsLinkPreview` remains enabled; long-press preview is a manual check. |
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
| `websiteDataStore` | Explicit `.default()` so cookies and sessions persist like Safari. | Simulator build. |
| `userContentController` | The root uses WebKit's controller; children receive the exact configuration supplied by WebKit, preserving the same controller and `window.opener`. PR 1 installs no bridge. | New-window implementation build; PR 2 owns bridge tests. |
| `allowsInlineMediaPlayback` | `true`, overriding the iPhone `WKWebView` default to match inline Safari media. | Simulator build; playback is a device check. |
| `mediaTypesRequiringUserActionForPlayback` | `.all`, keeping audible/video playback user-initiated. | Simulator build; playback is a device check. |
| `javaScriptCanOpenWindowsAutomatically` | `false`. User-initiated `window.open` works through `WKUIDelegate`; script-only popups remain blocked. | New-window policy tests and simulator build. |
| `allowsBackForwardNavigationGestures` | `true`, restoring Safari-style swipe traversal where history exists. | Simulator build; gesture behavior is a manual check. |
| `allowsLinkPreview` | `true`, retaining Safari/WebKit link previews. | Simulator build; long-press is a manual check. |
| `dataDetectorTypes` | `.all`, restoring iPhone-style detection of links, phone numbers, addresses, and dates. Generated external schemes still pass navigation policy. | Simulator build; detected-link interaction is a manual check. |
| `isElementFullscreenEnabled` | `false`. Fullscreen product behavior is deferred from Phase 0a. | Simulator build; the deferral is recorded below. |
| `isInspectable` | `true` only in Debug builds; Release keeps WebKit's `false` default. | Debug simulator build setting and compilation. |
| `upgradeKnownHostsToHTTPS` | `true`, retaining WebKit's HTTPS upgrade default. | Simulator build. |
| `limitsNavigationsToAppBoundDomains` | `false`; App-Bound Domains would break configured local/cloud instances and authentication origins. | Simulator build and origin policy tests. |
| `defaultWebpagePreferences.preferredContentMode` | WebKit `.recommended`, which selects the iPhone-appropriate content mode. | API ledger and simulator build. |
| `defaultWebpagePreferences.allowsContentJavaScript` | WebKit `true`; the Workbench requires JavaScript. | The simulator capability probe executes JavaScript. |
| `defaultWebpagePreferences.lockdownModeEnabled` | System/WebKit default. The shell does not weaken Lockdown Mode. | System-owned behavior. |
| `defaultWebpagePreferences.preferredHTTPSNavigationPolicy` | WebKit keep-as-requested default; configured instance input already requires HTTPS and known-host upgrades remain enabled. | Origin parsing tests. |
| `minimumFontSize`, `fraudulentWebsiteWarningEnabled`, `textInteractionEnabled`, `siteSpecificQuirksModeEnabled`, `inactiveSchedulingPolicy` | WebKit defaults. These preserve browser text interaction, compatibility, fraud warnings, and lifecycle scheduling. | API ledger and simulator build. |
| `allowsAirPlayForMediaPlayback`, `allowsPictureInPictureMediaPlayback` | WebKit `true` defaults. | API ledger; media UI is a device check. |
| `suppressesIncrementalRendering`, `ignoresViewportScaleLimits` | WebKit `false` defaults. Pages render incrementally and retain author viewport limits. | API ledger and simulator build. |
| `allowsInlinePredictions`, `supportsAdaptiveImageGlyph`, `writingToolsBehavior` | WebKit defaults. The shell does not add text-system behavior beyond Safari/WebKit. | API ledger; keyboard behavior is a device check. |
| iOS 27 webpage preferences (`securityRestrictionMode`, `alternateRequest`, `overrideReferrer`, `allowsJSHandleCreationInPageWorld`, `globalPrivacyControlEnabled`) | WebKit defaults. No Phase 0a requirement justifies changing requests, referrers, page-world handles, or privacy signals. | Documentation-only until the pinned toolchain exposes the full iOS 27 set. |

## Web platform outcomes

| Capability | Outcome |
| --- | --- |
| New windows and `window.close()` | Supported by `createWebViewWith` and `webViewDidClose`; child WebViews recursively use the same delegates. |
| JavaScript `alert`, `confirm`, and `prompt` | Supported with native dialogs and exactly-once completion on every exit path. |
| Instance HTTPS, matching `blob:`, and instance-frame `data:` downloads | Supported through the action/response policies and `WKDownloadDelegate`; non-displayable MIME and attachment responses use the same rule. |
| File selection, camera, and photo saving | Uses WebKit's Safari-style picker and the system activity sheet. `NSCameraUsageDescription` and `NSPhotoLibraryAddUsageDescription` are localized. |
| External application schemes | Trusted top-level contexts use `UIApplication.open`; subframes cancel them without leaving the app. |
| Web content process termination | The primary WebView reloads the configured instance; a terminated child closes. |
| `navigator.share` | Available in the Xcode 26.6 simulator secure-context probe: `{"secureContext":true,"navigatorShare":"function"}`. Presentation remains system-owned. |
| Clipboard | Uses WebKit edit/clipboard behavior. The Web UI feature-detects writes, falls back to `execCommand`, and reports blocked reads. No native clipboard bridge is added. |
| Notification and Service Worker | Unsupported in this shell configuration. The Web UI already feature-detects both; native push is later work. |
| Fullscreen | Deferred. `isElementFullscreenEnabled` remains false in Phase 0a. |
| iOS Home Screen Web App branches | Deferred to PR 2. `isNativeShell()` must decide whether to reuse in-app windows, loopback protection, and share-sheet media downloads currently gated by `iosStandalone`. |

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
