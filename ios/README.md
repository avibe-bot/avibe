# Avibe iOS shell

This directory contains the thin SwiftUI shell for the Avibe Workbench. The
shell stores one configured HTTPS instance origin, loads it in a persistent
`WKWebView`, and owns the platform boundaries that browser content cannot own:
navigation handoff, downloads, native dialogs, and microphone permission.

Phase 0a deliberately keeps the Web composer. The `avibeNative` bridge is added
with its Web UI consumer in the next slice so neither side of that contract can
ship alone. Native keyboard coordination moves with that same shell marker;
this slice leaves WebKit and the existing Web UI keyboard hook unchanged.

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
- Keyboard resizing, content insets, scrolling, link previews, media behavior,
  data detection, and browser preferences use WebKit defaults. PR 2 will add
  `isNativeShell()` before changing native keyboard layout or disabling the
  Web UI's `useIosKeyboardInset` hook.
- `AppIcon.appiconset` contains one opaque 1024-by-1024 icon. It is a temporary
  upscale of the existing 512-pixel iOS Home Screen Vibey artwork and should be
  replaced when a native 1024-pixel source is delivered.

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
6. PR 2 adds the origin-checked native bridge only to an instance main frame.

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
| `userContentController` | The root uses WebKit's controller; children receive the exact configuration supplied by WebKit, preserving the same controller and `window.opener`. PR 1 installs no bridge. | New-window implementation build; PR 2 owns bridge tests. |
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
| `scrollView.isScrollEnabled`, `bounces`, `contentInsetAdjustmentBehavior`, and content offset | UIKit/WebKit defaults. PR 1 installs no keyboard observer, keyboard layout guide constraint, outer-scroll lock, inset override, or offset reset. | Source inspection plus Debug and Release simulator builds. |
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
| Notification and Service Worker | Unsupported in this shell configuration. The Web UI already feature-detects both; native push is later work. |
| Fullscreen | Deferred. Phase 0a leaves `isElementFullscreenEnabled` at WebKit's default. |
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
