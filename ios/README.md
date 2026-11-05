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
- Configured instance origins stay in the WebView and can request microphone
  capture from the main frame.
- Authentication origins (`avibe.bot`, its production Supabase Auth origin,
  and GitHub) stay in the WebView without microphone access.
- The pinned Protected Vault origin may load only in an embedded frame or an
  app-created popup. Direct main-frame navigation still leaves the shell, and
  the Vault origin receives neither microphone access nor a native bridge.
- New windows requested by an instance browsing context open instance,
  authentication, and `about:blank` targets in closable child WebViews created
  with WebKit's supplied configuration. External HTTP(S) targets use
  `SFSafariViewController`, and `window.close()` closes the corresponding child
  WebView.
- Google authorization is intercepted with an email-code explanation.
- Other HTTP(S) origins open in `SFSafariViewController`; external application
  schemes such as `mailto:` and `tel:` use `UIApplication`.
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

The production Supabase Auth origin is an explicit allowlist entry. If Avibe
Cloud moves to another Supabase project, update that origin and its navigation
test in the same change.

## WebKit capability matrix

| Capability | Status and handling | Verification |
| --- | --- | --- |
| New windows and `window.close()` | Supported. Instance, authentication, and `about:blank` targets from a WebView whose main frame is the configured instance open in a closable child `WKWebView` using the configuration supplied by WebKit. Child windows use the same policy recursively. External HTTP(S) uses Safari. | `NavigationPolicyTests` covers internal, authentication, blank, external, application-scheme, Google, Protected Vault, and untrusted-main-frame decisions. Simulator CI compiles the recursive delegate and `webViewDidClose` path. Interactive close behavior remains a real-device check. |
| JavaScript `alert`, `confirm`, and `prompt` | Supported with `UIAlertController` presented by the initiating WebView's controller. Pending completions are cancelled exactly once on navigation, WebView removal, popup close, process termination, and controller teardown. | Simulator CI compiles all three `WKUIDelegate` contracts. The Phase 0 device pass checks interaction and dismissal behavior. |
| Instance HTTPS downloads | Supported through `WKDownload` for download-attribute actions, attachment responses, and MIME types WebKit cannot display. | Pure policy tests cover allowed instance HTTPS and rejected untrusted main frames; simulator CI compiles both action and response download delegates. |
| Instance-origin `blob:` downloads | Supported when the origin embedded in the `blob:` URL exactly matches the configured instance origin. | Pure policy tests cover matching and mismatching embedded origins. |
| Instance-frame `data:` downloads | Supported only when the action source frame is the configured instance origin. Response downloads cannot recover a source frame and therefore do not admit `data:` URLs. | Pure policy tests cover instance, authentication, missing-source, and untrusted-main-frame cases. |
| File selection, camera, and photo saving | WebKit owns `<input type="file">` and its system picker. Camera capture is declared with `NSCameraUsageDescription`; Save Image from the share sheet is declared with `NSPhotoLibraryAddUsageDescription`. | CI validates and builds the localized Info.plist catalog. Picker sources and permission prompts remain real-device checks. |
| External schemes | Supported for trusted instance browsing contexts through `UIApplication.open`; WebKit never loads `mailto:`, `tel:`, or another application scheme. Internal schemes such as `about:`, `blob:`, `data:`, `javascript:`, and `file:` are not treated as applications. | Navigation and new-window policy tests cover application-scheme routing. |
| Web content process termination | Supported. A terminated primary WebView closes child contexts and reloads the configured instance URL; a terminated child WebView closes that child. | Simulator CI compiles the termination delegate. Forced process termination remains a manual device/simulator check. |
| `navigator.share` | Supported by the iOS WebKit engine. The shell leaves it to WebKit and the system share sheet. | The Xcode 26.6 simulator XCTest loads a secure instance-origin document and records `AVIBE_CAPABILITY navigator.share={"secureContext":true,"navigatorShare":"function"}`. Presenting and dismissing the share sheet remains an interactive check. |
| Clipboard | Depends on WebKit feature detection and permission. The Web UI already feature-detects writes and falls back to `execCommand`; reads surface the existing permission-blocked state. No broader native clipboard bridge is added. | Existing Web UI lint/browser tests exercise the feature-detected paths; native CI verifies no extra permission or bridge surface is introduced. |
| Notifications and Service Worker | Not supported in this shell. `WKWebView` does not expose the required Service Worker path for this product configuration, and the Web UI already feature-detects both APIs. Native push is a later phase. | Existing Web UI support-state tests run in `lint`; PR 2 will make the native-shell result explicit through `isNativeShell()`. |
| Fullscreen | Deferred. The shell does not add a fullscreen delegate or product behavior in Phase 0a. | No automated test; this row records the intentional deferral. |
| iOS Home Screen Web App branches | Deferred to PR 2. `isNativeShell()` will decide whether the shell reuses in-app windows, loopback-link protection, and share-sheet media downloads currently limited to `iosStandalone`. | PR 2 must add Web UI unit tests for each shell-aware branch and run `npm run build`. |

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
