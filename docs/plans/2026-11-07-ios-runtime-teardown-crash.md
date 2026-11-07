# iOS Runtime Teardown Crash

## Goal

Prevent late WebKit, bridge, observer, and scheduled-work callbacks from
recreating or using a runtime WebView after an account, instance, or logout
transition releases it.

## Root Cause

`WebViewController` exposed the primary runtime WebView through a lazy
creation path. The account avatar bridge correctly reached the AppModel
transition, which invalidated the controller and cleared its website store.
WebKit then delivered a late callback from the old page. Identity checks and
other callback paths read the lazy property, attempted to recreate the
released runtime, and trapped with `The runtime WebView has been released.`

The crash reproduces with a real `WKWebView` and the real
`window.avibeNative.call("app.showAccount")` bridge before the fix.

## Teardown Contract

- Runtime WebViews are created only during controller setup or through the
  retained factory while the controller is active.
- After invalidation, callback paths use retained-only references and treat
  released or foreign WebViews as inactive.
- Invalidation cancels observers, pending dialogs, fallback and presentation
  work, downloads, popups, and WebView delegates before releasing the store.
- Active downloads are cancelled before their bookkeeping is released. Any
  temporary destination is removed immediately and again when cancellation
  completes, so a late write cannot leave account data in temporary storage.
- Late bridge calls cannot start another account transition or update runtime
  state.
- The same invariant covers account entry, instance switching, logout, and
  other AppModel paths that call runtime invalidation.

## Audit Scope

The change audits the primary `WKNavigationDelegate`, `WKUIDelegate`, and
`WKDownloadDelegate` entry points, the document bridge callbacks, KVO and
keyboard observers, delayed fallback and file-presentation work, popup
controllers, the onboarding WebView, and the hidden authentication transport.
The latter controllers already hold optional WebViews and invalidate their
delegates without a lazy recreation path.

The WebKit regression uses the production AppModel, a local fixture server, and
a test-owned named WebKit store. It performs the real route transition from a
loaded runtime page, then delivers late bridge, navigation, content-process,
and UI-delegate callbacks. Cleanup observes the test-owned store disappear from
WebKit's registry before the test completes.

Behavioral XCTest drives the real bridge and AppModel transition, then delivers
late navigation and UI-delegate callbacks after invalidation. A real WebKit
download regression also verifies that invalidation cancels an active download,
detaches its delegate, removes its temporary destination, and rejects late
callbacks.

## Validation

- iOS 26.4.1 simulator: focused and full XCTest in Debug and Release.
- iOS 27.2 simulator: focused and full XCTest in Debug and Release.
- Release XCTest uses `ENABLE_TESTABILITY=YES` because the test target uses
  `@testable import Avibe`; the app remains built with Release optimization.
- The mobile bundle is generated locally with `npm run build:mobile`; the
  generated `ios/Avibe/Resources/MobileApp` directory remains ignored output.
- Physical-device verification of the original account-avatar tap remains an
  owner integration check after the PR head is available.
