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
- Google authorization is intercepted with an email-code explanation.
- Other HTTP(S) origins open in `SFSafariViewController`; other schemes are
  rejected.
- JavaScript confirmation dialogs use a native alert, and same-origin WebKit
  downloads open the system share sheet so the user can save them to Files.
- Cookies and website data use `WKWebsiteDataStore.default()` and survive app
  launches.
- The WebView is pinned to `keyboardLayoutGuide`, has automatic content insets
  disabled, and does not expose an outer scrolling surface.

The production Supabase Auth origin is an explicit allowlist entry. If Avibe
Cloud moves to another Supabase project, update that origin and its navigation
test in the same change.

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

Tests and development builds must use disposable instance URLs or local fixture
servers. Do not point automated tests at a real Avibe instance.

## CI

`.github/workflows/ios-shell.yml` runs only when `ios/**` or the workflow itself
changes. It pins the same Xcode 26.6 toolchain used by the desktop native
workflow, builds against the iOS Simulator SDK without signing, and runs the
XCTest navigation contract on an available iPhone simulator.

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
