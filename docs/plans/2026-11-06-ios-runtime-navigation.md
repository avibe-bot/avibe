# iOS Runtime Navigation And Account Entry

## Goal

Match Safari's primary-runtime back gesture without allowing account or external
pages into the selected instance's interactive history, and move the
compatibility account entry out of the Web content area.

## Change Contract

- The primary runtime WebView enables WebKit's interactive back gesture only
  when its immediate back item belongs to the selected instance origin.
- A back-forward navigation to an authentication, consent, callback, or other
  origin is cancelled even when code calls `goBack()` directly.
- Child windows keep their existing native toolbar, history, and close
  behavior. They do not gain the primary interactive gesture.
- `accountPresentation: 2` adds
  `app.setAccountActionAvailable({available})`. The Workbench AppShell
  registers while its account action is mounted and releases the registration
  on `pagehide` or unmount.
- Registration is accepted only from the trusted selected-instance primary
  main frame. It is a display hint and grants no account operation or
  authorization.
- The native account fallback starts hidden. After primary `didFinish`, an old
  Web UI that does not register within a short grace period receives a
  WebView-external top bar. A current Web UI never flashes or reserves that
  bar.
- The fallback bar uses the current account's email-local-part initial, or the
  Avibe initial when no account is available. Its 56-point content height,
  safe-area background, border, and light/dark colors match the existing
  mobile brand header and Account avatar.

## History Boundary

The Web UI records phone conversation navigation with same-document history.
The native shell observes WebKit's immediate `backForwardList.backItem`, so SPA
`pushState` entries participate without a parallel history model. A system
edge traversal is deliberately a cut in the Web motion layer; only the Web
header's own Back action runs the product pop animation.

OIDC remains ordinary primary navigation while it is active, but it is never an
admitted interactive back target. If history is
`instance -> control plane -> instance`, the control-plane item disables the
gesture and a forced back traversal is rejected. A later instance-origin
`pushState` entry can become the immediate back item and re-enable the gesture.

## Compatibility

New Web UI plus old app keeps the existing Web account behavior because
capability 1 still admits only `app.showAccount()`. New app plus old Web UI
shows the native fallback after the grace period. New app plus current Web UI
uses the Workbench account action and shows no native bar.

## Evidence

- Focused Vitest covers capability negotiation and the AppShell registration
  lifecycle.
- Hermetic real-WKWebView XCTest covers SPA push/back, an authentication item
  in the back-forward list, forced traversal rejection, recovery after a new
  instance history entry, primary-only registration, child rejection, and the
  delayed old-Web fallback.
- SwiftUI host snapshots cover the old-Web fallback in light and dark themes;
  a WebKit attachment covers the current Web UI with the fallback suppressed.
- The focused WebKit suite runs on local iOS 26.4.1 and iOS 27.2 simulators.
  Full CI remains authoritative on the pinned Xcode 26.6 / iOS 26 runtime.

Physical-device interactive gesture feel and VoiceOver menu behavior remain
manual acceptance. No signing, TestFlight upload, production account, or live
authentication mutation belongs to this change.
