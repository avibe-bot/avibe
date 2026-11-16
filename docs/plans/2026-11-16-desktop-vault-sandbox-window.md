# The Vault sandbox window in the macOS Desktop App

Status: implemented in `desktop/src-tauri/src/macos_vault_window.rs`. A real
passkey ceremony in the window is not verified yet; see the last section.
Date: 2026-11-16.
Related: `2026-11-10-ios-protected-vault-passkey.md` (the same decision for the
iOS App), `vault-sandbox-protocol-v2.md`, `desktop/README.md` ("The Vault
sandbox window").

## Background

Protected Vault passkey steps run in sandbox code at `https://sandbox.avibe.bot`.
Two surfaces take part: the sandbox iframe inside the Workbench, and a top-level
sandbox window, which is the only place a passkey is created and the only place
an approval, signature or reveal is confirmed. That window finds the requesting
iframe through `window.opener`.

The Desktop shell sent every request for a new window to the system browser, so
the window never existed in the app and never had an opener. The Workbench
therefore hands every protected step to the browser
(`ui/src/lib/vaultBrowserHandoff.ts`).

Three changes together move those steps into the app on macOS: the app's
signature is associated with the sandbox's passkeys; the shell shows the
sandbox's window (this document); and the Workbench stops handing off when the
shell says it can.

## Goal

On macOS, a request from the Workbench or from the sandbox iframe for the
sandbox's window opens a native child window of the app, linked to its opener,
so the Web flow runs unchanged. Nothing else gains an in-app window, and the
window gains nothing of the shell.

## Solution

WebKit asks the opener's UI delegate for a new webview and requires the answer
to be built from the configuration it hands over; that configuration carries the
opener link and the website data store. Tauri's `on_new_window` can only answer
with a Tauri window, and wry's own child shares the opener's
`WKUserContentController`. The shell therefore replaces the implementation of
wry's delegate method, as `macos_new_context.rs` already does for the navigation
delegate, answers for the sandbox's window, and passes every other request to
wry unchanged.

The passkey prompt, the review and the cryptography stay in sandbox code. None
of the shell's own code handles the passkey result. WebKit's passkey code does
run in the app's process, so the result, including the PRF output, crosses that
process as it crosses a browser's. The app, like a browser, is trusted to run
the sandbox unmodified.

### Rules

1. **Which requests.** A request from the main window's webview for exactly the
   sandbox origin (`https`, `sandbox.avibe.bot`, default port, no credentials),
   or for exactly `about:blank`. Everything else goes to wry.
2. **What the window may load.** Only the sandbox origin. Every frame is
   checked when a navigation starts (this also sees each redirect) and when its
   response arrives; the main frame is checked again when it commits. The
   reserved blank page is allowed in the main frame until the first sandbox
   document. A nested request for a window, a download, and a response WebKit
   cannot display are refused. The origin is what is enforced, not the release:
   an older retained release or the origin's root loads too, as in a browser
   tab. Pinning the release stays the Workbench's check before it navigates.
3. **When it is displayed.** The native window is created ordered out. When a
   sandbox document commits it comes on screen with a transparent webview,
   taking neither clicks nor keys. Its content is revealed by the first
   presentation update WebKit delivers after that commit. If anything but a
   sandbox document commits, the window is closed. If it has displayed nothing
   60 seconds after it was created, it is closed.
4. **When it may appear.** Only while the app is active and the opener's window
   is on screen; only one per main window at a time; not within 0.75 seconds of
   another leaving the screen. A window that may not appear when its sandbox
   document commits is closed, never kept for later. Its content size is
   clamped to 360-900 by 400-1000 points.
5. **What it holds.** A new, empty `WKUserContentController` and default
   `WKPreferences` of its own with element fullscreen, picture in picture and
   video presentation modes off. Before the webview is returned to WebKit, the
   shell reads both back from it: the controller must be the empty one, and the
   preferences must not be the opener's and must report each of those features
   off. Otherwise there is no child. Media capture is refused. Its title is the allowed host.
6. **How it ends.** `window.close()`, the user closing it, the opener committing
   a new document, the opener's window being closed or destroyed, a crashed web
   content process, or rules 3 and 4. Each path detaches the delegates,
   destroys the page and closes the native window.

At most four children exist at once for the main webview, hidden ones included.
This bounds resources; it is not a security boundary between pages.

### Why the content is revealed by a presentation update

The Workbench reserves `about:blank` in the approval click and navigates it
after verifying the pinned sandbox release. A blank page inherits its opener's
origin, so the opener can draw in it, and the Workbench origin also serves
Agent-authored Show Pages.

Keeping the window off screen until a sandbox document commits is not enough.
WebKit keeps displaying the previous document's layers until the new document
has something to show, so that navigations do not flash. A window ordered in at
the commit showed the opener's page, under the sandbox's title, until the
sandbox first painted. The probe measured this: an opener that filled the
reserved page with one colour had that colour displayed for as long as the
sandbox page's stylesheet was held back.

So the window appears with its webview transparent, and the webview is revealed
by the first presentation update after the commit. That update is requested
after the sandbox document committed, so the layers it brings are that
document's; while the display is frozen it does not arrive. It is not a timer.
Until then the window cannot become key and ignores clicks, so no input can
reach a page the user cannot see.

A sandbox document is cross-origin to the opener, so the opener cannot draw in
it, and rule 2 means it can only be replaced by another sandbox document. From
the reveal on, what is displayed is a sandbox document.

The presentation update (`_doAfterNextPresentationUpdate:`) and destroying a
page at once (`_close`) are WebKit methods outside its public API, as is the
feature list used for rule 5. WebKit has no public equivalent for the first.
Each is checked to exist before anything is installed.

### Why a window needs the user's attention

On macOS WebKit's default lets scripts open windows without a user gesture. A
browser requires one. Changing that default would also change every link the
Workbench and its Show Pages open from script. So a page of the Workbench
origin can ask for the sandbox's real window at any moment. It cannot put a
request into it, because the window and the iframe speak only to documents of
the sandbox origin, and a confirmation still needs the user's click on a
truthful card. What it could do without rule 4 is choose when and where real
cards appear: over a hidden main window, several at once, or in the place of a
card the user has just confirmed, so that a second click lands on the next
request.

Rule 4 is the smallest set of bounds that takes those choices away. The
interval is 0.75 seconds: longer than the default double-click interval of half
a second, so a click aimed at the window that just closed meets nothing; shorter
than a person needs to return to the Workbench, approve another request and
have its release verified and loaded. With the sandbox's own half-second delay
before Confirm is armed, a second card cannot be confirmed sooner than 1.25
seconds after the first one's window closed.

The remaining difference from a browser is that a card can still appear without
a gesture while the user is in the app. The complete answer belongs to the
sandbox (arming Confirm only after its document has been focused and visible
for the arming time) and is not part of this change.

### Why the URL schemes are guarded

WebKit's copy of the opener's configuration includes its custom URL scheme
handlers (`ipc`, `tauri`), and the public API can neither list nor remove them.
wry's handler reads fields that only its own webview class has. In wry 0.57 a
request to `ipc://` from a webview of another class aborts the process in a
debug build ("tried to access uninitialized instance variable"); a release
build makes the same read without the check. The blank page a
child starts with is scriptable by its opener, so this is reachable.

The shell therefore wraps the two methods of every URL scheme handler class
registered at run time (wry creates one such class per scheme). A request from
a child's webview fails with "unsupported URL" before the handler runs; every
other request goes to the handler unchanged. If the classes cannot be listed,
or the handler Tauri always registers (`ipc`) is not among those wrapped, the
feature is not installed at all.

### The capability marker

`window.__AVIBE_DESKTOP_VAULT_PASSKEY__` is defined on the main webview's
top-level document by the same call that installs the hooks, and only after
they are in place. That call returns before doing anything unless the system is
macOS 15 or later and the running process's own code signature carries
`com.apple.developer.associated-domains` with `webcredentials:sandbox.avibe.bot`
(read with `SecTask`, which reports what the kernel holds for the process). A
build flag is not consulted. macOS refuses to start an ad-hoc build that claims
this entitlement, so a development build can only be without it.

A shell without the marker is the shell as it was: nothing is installed, and
the sandbox's window goes to the browser with every other link.

### Alternatives not taken

- **wry's `NewWindowResponse::Allow`.** Shared content controller, no
  navigation rule, no close on `window.close()`, and the URL scheme hazard.
- **A Tauri window through `NewWindowResponse::Create`.** wry registers the new
  webview's scripts and IPC handler on the shared controller and removes the
  opener's IPC handler when it is dropped. The window would also be a labelled
  Tauri webview.
- **No blank step** (the Workbench opens the sandbox address directly). Not
  needed, and it would not help: a window opened for the sandbox address also
  starts with a blank document its opener can draw in.
- **Requiring a user gesture through WebKit's switch.** It governs when a
  window may be created, not when a reserved one is navigated, so it does not
  stop a window held in reserve; and it would change links opened from script
  in Show Pages. The Workbench's own code opens windows only inside click and
  key handlers.
- **Revealing on the commit, on load finished, or after a delay.** The first is
  what the probe disproved; the others are timing.

## Verification

- Unit tests in the module: the origin rule, which requests open a window, what
  a child may load, the commit rule, the attention rule and its interval, the
  size bounds, and the marker's two conditions.
- `desktop/src-tauri/examples/vault_window_probe.rs`, run by CI on macOS: real
  WebKit windows on two loopback origins with a disposable data store. The
  probe compiles the module's source file into itself and enters one step below
  the signature check, so the library exports nothing for tests and has no
  test-only feature. Three runs:
  - **App active.** Both ways a window is requested (the Workbench's
    reservation and the iframe's own window); the opener link in both
    directions; nothing of the shell in the page; every navigation refusal;
    every close path; which window is key. For rule 3, the opener fills the
    reserved page with one colour and the sandbox page cannot paint for two
    seconds: the probe reads back what the window's views draw, from the moment
    it is on screen, and no sample may have that colour. A page that never
    paints stays empty and without input and is closed at the deadline. For
    rule 4: a second window while one is on screen, a window pointed at the
    sandbox the moment another closes, and a window opened while the main
    window is hidden are each closed without ever being on screen.
  - **App not active.** A sandbox window is closed instead of appearing. This
    run takes no keyboard focus.
  - **The shell's own install** on the unsigned test executable: no marker and
    no window.
- Each rule was also removed once by hand to see the probe fail: the opener's
  content controller kept, no URL scheme guard, reveal on the commit, no
  interval, two windows on screen, a window over a hidden main window.
- The two calls in `build_main_window` that connect the module to the main
  window have no automated check. Removing the install leaves the browser
  hand-off; removing the navigation hook leaves a sandbox window open across a
  Workbench reload. Both show in the manual check on a signed build.

## Not verified

Only a signed build with a real passkey can show these:

- The shell's install passing its own admission. In every test it either
  refuses (the unsigned executable) or is entered one step below.
- The real origin, and how long its first paint takes in the window.
- The passkey sheet from the window, and passkey PRF from the sandbox iframe
  inside the app.
- Focus while a real system sheet is on screen.
- A release build, and macOS 15. Tests run a debug build on macOS 26.
- The association being fetched after the app's first launch.
