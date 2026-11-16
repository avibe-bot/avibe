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

### Rules

1. **Which requests.** A request from the main window's webview for exactly the
   sandbox origin (`https`, `sandbox.avibe.bot`, default port, no credentials),
   or for exactly `about:blank`. Everything else goes to wry.
2. **What the window may load.** Only the sandbox origin. Every frame is
   checked when a navigation starts (this also sees each redirect) and when its
   response arrives; the main frame is checked again when it commits. The
   reserved blank page is allowed in the main frame until the first sandbox
   document. A nested request for a
   window, a download, and a response WebKit cannot display are refused.
3. **When it is visible.** The native window is created ordered out. It is put
   on screen when a sandbox document commits. If anything else commits, the
   window is closed. If nothing has committed after 60 seconds, it is closed.
4. **What it holds.** A new, empty `WKUserContentController`, verified on the
   created webview before it is returned. No delegate method grants media
   capture. Its title is the allowed host.
5. **How it ends.** `window.close()`, the user closing it, the opener committing
   a new document, the opener's window being closed or destroyed, or a crashed
   web content process. Each path detaches the delegates, closes the native
   window and releases the webview, which destroys the page.

### Why the window is hidden until the sandbox commits

The Workbench reserves `about:blank` in the approval click and navigates it
after verifying the pinned sandbox release. A blank page inherits its opener's
origin, so the opener can write into it, and the Workbench origin also serves
Agent-authored Show Pages. Showing the window at creation would let any page of
that origin draw in an app window.

A sandbox document is cross-origin to the opener, so the opener cannot script
it, and rule 2 means it can only be replaced by another sandbox document. So
"visible" implies "showing the sandbox" from the first frame to the last.

This does not rely on a user gesture. On macOS WebKit's default lets scripts
open windows without one, and changing that default would also change every
link the Workbench opens. The cost is bounded instead: at most four windows,
each hidden until it holds the sandbox and gone after the deadline. A page of
the Workbench origin can open the real sandbox window unprompted, as any site
can in a browser; it cannot put a request into it, because the window and the
iframe speak only to documents of the sandbox origin.

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
  needed: the hidden window makes the blank step safe, and the Workbench keeps
  one code path for browser and app.
- **Requiring a user gesture.** The public API does not report one for a
  window request, and turning WebKit's switch off changes unrelated links.

## Verification

- Unit tests in the module: the origin rule, which requests open a window, what
  a child may load, when it is shown, and the marker's two conditions.
- `desktop/src-tauri/examples/vault_window_probe.rs`, run by CI on macOS: real
  WebKit windows on two loopback origins with a disposable data store. It covers
  each rule above, both ways a window is requested (the Workbench's reservation
  and the iframe's own window), the opener link in both directions, every close
  path, and which window is key. The probe compiles the module's source file
  into itself and enters one step below the signature check, so the library
  exports nothing for tests and has no test-only feature. A second run uses the
  shell's own install on the unsigned test executable and expects no marker and
  no window.
- The two calls in `build_main_window` that connect the module to the main
  window have no automated check. Removing the install leaves the browser
  hand-off; removing the navigation hook leaves a sandbox window open across a
  Workbench reload. Both show in the manual check on a signed build.

## Not verified

- A passkey prompt in the window, and PRF from the sandbox iframe inside the
  app. Both need a build signed with the entitlement and a real passkey.
- The marker being present on such a build. The entitlement cannot be given to
  a test executable.
- macOS 15 itself. The probe runs on the CI image's macOS.
