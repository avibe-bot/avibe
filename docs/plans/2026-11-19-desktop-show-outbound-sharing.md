# Desktop Show outbound sharing

## Goal and scope

Let a person share the Show a desktop window displays as its existing link, a PDF, or an image through the macOS share menu (`NSSharingServicePicker`). This is the desktop half of C1 v1 outbound, with the same requests, answers, copy and share control as the iPhone (`docs/plans/2026-11-18-ios-show-outbound-sharing.md`). It does not change access or publish pages, and it adds no entitlement or system permission. It does not cover inbound sharing (D1), packaging, or release.

## Contract

- **Which Show and which pixels.** These are decided by state the shell owns, never by the page's DOM or URL (independent review of `9904474a`, P1).
  - The Workbench (chat preview, in-page Show windows) shares a link only. It has no trusted tie to the pixels of a Show it embeds.
  - A native Show app window (`avibe-app-show-*`) shares only the Show the shell opened it for. It shares that Show's link, or a PDF or image of the window's whole web content.
- **Markers.** They tell the page what to show; they are not an authority. Both are set only on macOS 13 and later (`-[NSSharingServicePicker close]` is macOS 13), and only on a top-level loopback document.
  - `__AVIBE_DESKTOP_SHOW_SHARE__ = 1` goes on the Workbench and on Show windows.
  - `__AVIBE_DESKTOP_SHOW_EXPORT__ = 1` goes on Show windows only.
  - A Show window carries the app-window marker, not the shell marker; the page accepts either.
- **The command.** One Tauri command, `desktop_show_share`, carries the iPhone bridge's `share.capabilities` and `share.present` requests unchanged.
  - Capabilities answer per window: `["link"]` for the Workbench, and `["link", "pdf", "image"]` for a Show window.
  - Capability `show-share` grants only this command, to `main` and `avibe-app-show-*` on loopback origins. No other app window and no remote origin receives it.
- **Who may share** (`app_windows::share_source` and `show_share::decide`):
  - A Show window naming another Show is refused as `forbidden`.
  - The Workbench asking for a PDF or image gets `capability_mismatch`.
  - The current document is the one registered for this window and Runtime. Any navigation, close or Runtime switch ends the operation as `cancelled`, both from the watcher and from the worker path.
- **Reading the Show.** In WebKit's isolated client world, the shell reads the Show's access and page through the instance's existing GET APIs. These calls read no DOM.
- **The link** comes from those reads.
- **An export** captures the Show window's whole web view with `takeSnapshotWithConfiguration`, as a PNG or as the same pixels in a one-page PDF. It does not navigate, scroll, load hidden content or print. What the window shows is what is exported.
- **One currentness test.** Every boundary of an operation asks `Currency::of`: READ, capture, claiming the file, creating the menu, the watcher, and the end of a failed run. The answer is Live, Moved or Ended.
  - The operation is Live while all of these hold:
    - its token is active;
    - its window's registration holds (not retired, owning its native resource, same document generation);
    - its Runtime receipt holds (epoch, presentation owner, role);
    - the top document is on the Runtime's origin;
    - in a Show window, the path is still the registered `/apps/show/<id>`. WebKit's URL follows `pushState` and `replaceState`.
  - The Workbench is bound to its document and Runtime, not to the chat it shows.
  - The facts are read on the main thread, from WebKit's own URL and from the Runtime receipt (atomics and std mutexes only). Nothing there waits on the event loop.
  - Every terminal answer goes through one place: a worker error, the menu gate, the service's report, and the watcher's departure or expiry. The facts are read first, outside any lock. Then one coordinator transaction, `Coordinator::end`, records what they show, applies `conclude`, and commits the answer. Closing the menu, answering and removing files happen after the lock is released.
  - A window once seen Moved stays Moved, so it is `cancelled` even if it comes back, or if a service then reports `completed`. A departure recorded by any boundary before that commit cancels it. One recorded after the commit finds the operation ended.
  - An error while Live is `failed`. A service report or expiry while Live stands. An operation that already Ended needs nothing.
  - Expiry is decided in the same transaction, for the watcher's own operation only.
- **The share menu.** It is created only if the same test still says Live on the main thread at that moment.
  - Once shown, the menu is registered by operation, and only then is its operation checked in the coordinator; closing takes the menu by operation. Whichever of the two comes second closes it, so a menu shown for an operation that ended or moved on meanwhile is closed. This holds whether or not AppKit runs this operation's queued close inside the call that shows the menu. That reentrancy is assumed possible, not reproduced.
  - A new operation may start while the previous one's menu is still closing. Only one operation is active at a time, but there is no guarantee of a single on-screen menu during that close.
  - Choosing no service is `cancelled`.
  - A service's `didShareItems` is `completed`, which means only that the system took the item.
  - A user-cancelled service is `cancelled`; any other service error is `failed`.
  - A service that never reports leaves the operation to end as `expired` after fifteen minutes.
- **Operations.**
  - One share runs at a time.
  - A repeated request from the same window attaches to the running operation, or replays its answer for a day.
  - A different request is refused, as `busy` or `operation_conflict`.
- **Files.**
  - Exports are written to `$TMPDIR/AvibeShowShares/<operation>/Show.pdf|png`, with the directory mode 0700 and the file mode 0600.
  - A completed share keeps its file for five minutes; a cancelled, failed or expired share has it removed at once. An operation still open after fifteen minutes expires.
  - A file has an owner before it has a byte.
    - The worker claims a lease for an operation that is live and has not been seen Moved, or writes nothing.
    - `Files::write` removes the operation directory it created if any write fails.
    - The claim is released on every exit, and the cleanup then removes the file once due.
    - Each due file goes to one cleaner. It stays leased until that cleaner acknowledges the removal; a failed removal is retried a minute later.
  - The lease reserves the operation id, after its replayed answer is forgotten too. A request reusing the id is refused as `operation_conflict` until the file is gone. A late acknowledgement or retry therefore never touches a later operation's file.
  - Launch removes operation directories older than a day.
  - No path, name or content is logged.

## Native Show window entry

A native Show window has no web title bar. Its share entry is a title-bar accessory the shell adds on macOS 13 and later (`macos_show_titlebar.rs`): the system `square.and.arrow.up` symbol with no label, named and tooltipped with the product's existing "Share" copy in the system language.

- A press dispatches `avibe:show-share-open` into the window's current document through `window.eval`, from Rust to the page. No IPC is granted for it.
- `NativeShowShareEntry` opens the shipped share popover from the content's top right corner; a second press closes it, and so do Escape and a click outside.
- A document that does not listen ignores the event. A Show's script dispatching it can at most open this window's share popover for this window's Show.
- Main-window title bars, other app windows and deep links are untouched.

## Known limits

- The system share menu has no veto. While it is open, macOS tracks the mouse in a nested run loop, so a navigation or Runtime change during that time takes effect when the menu closes: the share then ends `cancelled`, never `completed`. What the menu holds was prepared for this Show while the operation was valid, and a service already chosen cannot be recalled.

- A Show runs `allow-same-origin`, as on the iPhone. It controls its own pixels and can draw any data it can read from same-origin APIs. A Show-window export captures what the window shows, which is what the user sees; the shell cannot tell what the Show drew from genuine Workbench UI.
- A Show's script can call the share command for its own window directly. That skips the web export notice, though the system menu and its service choice are still required. A native confirmation would close this gap, but it is new UI and new copy, and is out of scope.
- In the Workbench, a page may open the link-share menu for any Show the signed-in user can already use. That changes no permission, and nothing leaves without choosing a service.
- Some third-party share services may never report back. Such a share ends as `expired` after fifteen minutes. Until then, the share control stays busy.

## Validation

- Vitest:
  - The share control and native adapter, in the desktop app with and without the marker.
  - The phone bridge stays first when both are present.
- Rust unit tests:
  - Request parsing, answers, operation idempotency and deadlines, file leases and orphan removal.
  - Link validation and region geometry.
- Design frames: `show-pages/desktop-sharing/*`.
- A hermetic WebKit probe with a synthetic Show checks the real capture, encoding, and opening and cancelling the share menu, without sending anything.
- Device checks with real services are residual manual work.

## Work

- [x] Design frames, reviewed by main (2026-11-19).
- [x] Web adapter and share control.
- [x] Native command, window binding, capture, share menu and file lifecycle.
- [x] Review fixes (9904474a): shell-state binding, macOS 13 gate, cleanup and presentation races.
- [x] Native Show window title-bar entry.
- [ ] Independent re-review, then composition into w1.6.
