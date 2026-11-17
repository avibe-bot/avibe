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

1. **Which requests.** A request from a Workbench's web view for exactly the
   sandbox origin (`https`, `sandbox.avibe.bot`, default port, no credentials),
   or for exactly `about:blank`, while that web view is in place (in its
   window, not hidden, not covered; see "One native window, several web
   views"). One that is not in place gets `null`. Everything else goes to
   wry.
2. **What the window may load.** Only the sandbox origin. Every frame is
   checked when a navigation starts (this also sees each redirect) and when its
   response arrives; the main frame is checked again when it commits. The
   reserved blank page is allowed in the main frame until the first sandbox
   document. A nested request for a window, a download, and a response WebKit
   cannot display are refused. The origin is what is enforced, not the release:
   an older retained release or the origin's root loads too, as in a browser
   tab. Pinning the release stays the Workbench's check before it navigates.
3. **When it is displayed.** The native window is created ordered out. When a
   sandbox document commits it comes on screen with a transparent webview. Its
   content is revealed by the first presentation update WebKit delivers after
   that commit, so the empty window stays until the sandbox's first paint. At
   that moment it is ordered in front of the app's other windows and made key.
   If anything but a sandbox document commits, the window is closed. If it has
   displayed nothing 60 seconds after it was created, it is closed.
4. **When it comes on screen, and when it takes input.** It comes on screen
   only if, when its first sandbox document commits, the web view that asked
   is the one the user is looking at (still in the native window the support
   was installed for, not hidden and with no hidden view around it, no other
   web view of that window in front of it, the window visible, on the active
   Space and not minimized, and the app active) and no other sandbox window is
   on screen over that native window. Otherwise it is
   closed, never kept for later. A window on screen takes clicks and keys only while
   the app is active and the window is visible, and that has held without a
   break for 0.75 seconds the first time the card is seen and 2 seconds every
   later time, and never sooner than 2 seconds after the user last arrived
   (the app became active; the opener came back into view: its window from
   hidden, minimized or another Space, or its web view from hidden; or an
   opener that had no sandbox window asked for one). Until then input is dropped at the window's
   event entry and is not passed to what is underneath. Its content size is
   clamped to 360-900 by 400-1000 points.
5. **What it holds.** A new, empty `WKUserContentController` and default
   `WKPreferences` of its own with element fullscreen, picture in picture and
   video presentation modes off. Before the webview is returned to WebKit, the
   shell reads both back from it: the controller must be the empty one, and the
   preferences must not be the opener's and must report each of those features
   off. Otherwise there is no child. Media capture is refused. Its title is the allowed host.
6. **How it ends.** `window.close()`, the user closing it, the opener committing
   a new document, the opener's web view being hidden, the opener's window
   being closed or destroyed, a crashed web content process, or rules 3 and 4. Each path takes the window out of the
   shell's registry, closes the native window and destroys the page one turn of
   the main run loop later. The web view keeps its navigation delegate until
   the page is destroyed; with the window out of the registry that delegate
   refuses every navigation.

At most four children exist at once in the app, hidden ones included.
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
feature list used for rule 5. Each is checked to exist before anything is
installed. Existence says nothing about behaviour: that is checked only by the
probe, on the macOS versions CI runs it on, and the marker is limited to those
versions. The public snapshot call with `afterScreenUpdates` waits on the same
update and is a possible later replacement for the first; it has not been
tested. The page is destroyed one turn of the main run loop after the window
closes, so that the shell does not depend on what WebKit does after one of its
own callbacks returns.

### Why being displayed is not taking input

On macOS WebKit's default lets scripts open windows without a user gesture. A
browser requires one. Changing that default would also change every link the
Workbench and its Show Pages open from script. So a page of the Workbench
origin can make the sandbox's real window appear, showing the card of a real
pending request, at a moment it chooses. It cannot put content of its own into
the card, because the window and the iframe speak only to documents of the
sandbox origin, and a confirmation still needs the user's click on a truthful
card.

The bad outcome is not a window appearing. It is a click or a key reaching
Confirm on a card that has not just been displayed to a user who is present.
Rules about when a window may appear do not prevent that, for two reasons
found in review. The sandbox arms Confirm half a second after it builds the
card, with no condition on visibility, and a hidden page's timers run; so a
card held back and then shown takes a confirming click in its first visible
frame. And once a card is on screen nothing was judged again: a card left on
screen was live the moment the user returned or the app was pulled forward.

So there is one rule, about input, and it is the fourth above. It is checked
for each event at the one place every mouse, scroll and key event for a window
passes (`sendEvent:`, and `performKeyEquivalent:` for keys with Command).
"Visible" is AppKit's own occlusion state for the window, so that hiding the
app or the main window, minimizing, a Space change and display sleep are one
signal and not a list of causes. The probe produces the first two. The others
rest on AppKit's documentation, and one of them is known not to hold as
documented: on the CI runner AppKit did not report a card fully covered by
another window of the app as hidden. The app can be brought forward without the user
choosing it (a page asking for the main window or opening an app window, an
`avibe:` link from another app, an Agent launching the app), so an arrival
counts from the system's notification, not from any of those commands, and it
counts even if the card did not exist yet.

The intervals: 0.75 seconds at first sight is longer than the default
double-click interval, so a click begun on something else never lands on a
card that appeared in its place. Two seconds after an arrival gives a user who
has just come to the app, or been brought there, time to see where they are.

Clicks on the window's content in the interval are dropped, not passed
through; clicks on the title bar are delivered, so the window can be moved or
closed. The card is on screen;
a click on its Confirm that fell through would land on whatever the Workbench
has at that spot, which the page chooses and which can be a real control. The
card is put in front of the app's other windows and made key at the moment it
is displayed, and the shell never moves or keys it again, so nothing happens
on screen at the moment it turns live that a page could time. Another window
of the app can be put in front of it later, by the user or by a page. Keys are
dropped at the same entry, so being key early costs nothing.

There is no visual cue for "not live yet". One consequence: keys typed in the
two seconds after the user comes back to a card are dropped silently. In the
seed-wallet window that can shorten a typed passphrase without notice; the
double entry catches it when a wallet is created, and the address comparison
when one is imported.

**Each event is judged at the time it was made.** The first version judged an
event at the moment the window was handed it. A click is addressed to the
window that was in front when it was made and then waits in the queue while
the main thread is busy; judged at dispatch, the time it waited counted as
time the user had the card in front of them, so under a stall, or on a loaded
Mac, both intervals shrank by the length of the wait. Now:

`live(E) = in view at dispatch ∧ seen_for − age(E) ≥ (first ? 0.75 s : 2 s) ∧ arrived_for − age(E) ≥ 2 s`

with `age(E) = max(0, uptime now − E.timestamp)`. AppKit states an event's
timestamp on the clock of the system's uptime; both readings are taken when
the event is dispatched, the event's last, so only a difference crosses to
the clock the periods are kept on, and a gap between the readings makes the
event older. The age is never negative: a timestamp can make an event older,
never newer than its dispatch. An event made before the current period began,
or before the user last arrived, is dropped; so is one whose timestamp gives
no length of time. Keys and Command keys are judged the same way. What this
can drop that was delivered before: an event forwarded or posted again later
than it was made, and an event some software dated zero; each costs the user
a second click or key.

The period in view no longer starts in `reveal`, which runs inside WebKit's
presentation callback: what it changes reaches the screen only after that
turn of the run loop. `reveal` queues a block; the block, run from the run
loop outside any WebKit callback, commits the change to the window server
(`CATransaction flush`), then looks at the card and starts the period.
Nothing else can start it before: until the block has run, the card does not
count as in view, whatever event arrives.

Residual: the rule holds through any stall of the app's main thread. It does
not help against a device or system layer that stamps an event later than it
happened, against software that creates events dated now (Computer Use and
other accessibility-driven actions, below), or against a stall between the
commit leaving the app and the user's eyes: the flush is the last thing the
app can know. Whether a flush in that block is ever deferred on macOS 26 is
not verified.

Not covered, by decision or by nature: a second card navigated into the same
visible window (the sandbox's own arming applies, as in a browser); a present
user who confirms without reading; a card displayed while nobody is at the Mac,
which is live when they return; a card fully covered by another window of the
app and brought forward again, which may be live at once; the main window
merely raised from behind another window of the app, which is not an arrival
(the first 0.75 seconds cover it); partial covering, since a window counts as
visible when any part shows, so a card mostly covered by another window of the
app and brought forward again is live at once, and only its first click, made
while it is not the key window, is not delivered; menu commands and system input panels (Paste, the
emoji picker, dictation), which reach a key window and cannot confirm;
Computer Use and other accessibility-driven actions, as in a browser; drag and
drop.

The complete answer to the missing gesture still belongs to the sandbox
(arming Confirm only after its document has been focused and visible for the
arming time) and is not part of this change.

### One native window, several web views

While this change was in review the shell changed what a main window is. A
Runtime replacement (the user switching to another instance, or back) used to
build a new main window; it now creates a child web view inside the one native
main window, and the account screen is another child web view shown over a
hidden Workbench. Three things follow.

- "The main window is on screen" is no longer enough for rule 4: a Workbench's
  web view can be hidden while the native window is visible and the page keeps
  running. So the rule is stated on the opener: the web view that asked must be
  the one the user is looking at. That is three reads, made afresh at the
  commit, for every click and key, and for the arrival bookkeeping: the web
  view is still in the native window the support was installed for; it is not
  hidden and has no hidden view around it; and no other web view of that
  window that is not hidden lies in front of it. The first matters because
  closing a web view takes it out of its window without changing its `hidden`,
  and the shell does not rely on a closed web view being freed, on its page
  having stopped, or on a weak reference to it clearing. Admission at
  `window.open` makes the same three reads (not the window being on screen
  or the app being active, which are judged at the commit). The third
  matters because the shell covers a Workbench by hiding it, but one path
  (bringing the main window forward while the account page reloads) can show
  the Workbench under the account screen.
- Separately, every web view the shell builds announces three things: that
  it was hidden, that it was shown, and that it was moved into or out of a
  window (which is also how it is closed). AppKit calls `viewDidHide`,
  `viewDidUnhide` and `viewDidMoveToWindow` on a view for itself and for a
  hidden view around it; the module adds those three methods to wry's web
  view class, each calling its superclass first. Nothing inherited is
  replaced (that would change every web view in the process, a sandbox
  window's own included), and if wry's class already defines one of them
  nothing is added and the support is not installed. On each announcement,
  whichever web view made it, every opener is read again, and the sandbox
  windows of one that is no longer in place (in its window, not hidden, not
  covered) are closed, reserved and empty ones included, and their pages
  destroyed before the announcement returns. (The handler reads nothing only
  if the module's registry is in use at that instant. No path that reaches
  that is known, because the registry is never held across a call that
  touches a view; and the reads at admission, at the commit, at the reveal
  and for every event do not depend on announcements.) **An announcement only
  ever closes.** It is what makes closing prompt; nothing in the input rule waits
  for one, and whether an opener is in view is still read afresh at the
  commit and for every click and key. Nothing is observed and no web view is
  held, so nothing here depends on when a web view is freed. (The first
  version of this change key-value observed `hidden` on one web view and held
  it while observed; that could not see a close, and a held web view would
  have kept a replacement's disposable data store alive.)
- This covers each way a Workbench leaves: the shell hiding it (under the
  account screen, and first on every retirement path), closing it without
  hiding it, and creating another web view in front of it, as it does with a
  candidate Workbench during a switch. A retirement hides the web view,
  erases its data store and then closes it; the hide closes the cards and
  destroys their pages, so that is done before the erase starts. That is
  true of every sandbox window's page; the Workbench's own page and the
  sandbox frame inside it live through the erase, as #3850 designed it. What is
  asked of WebKit there is `_close` on the card's web view, in the same turn
  as the hide; the erase is asked for in a later turn.
- **A sandbox window exists only for an opener in place: one that is not in
  place cannot get one, and one that leaves its place loses those it has in
  that turn.** So admission at `window.open` reads all three too, and a web
  view that is not in place gets `null`. Without that, a window created after
  the opener left its place would not be looked at again until the next
  announcement or its commit, and a hidden Workbench could reserve a window in
  a store that is being erased. The native window being on screen and the app
  being active are not part of this: nothing is retired or erased by those,
  and a window reserved while the main window is hidden or the app is not
  active is still judged at its commit. The Workbench page takes a `null` as
  "the app could not open the window" and moves its passkey steps to the
  browser for the life of the page, so a page that asked while hidden would
  stay in the browser hand-off after it is shown again, until it reloads. No
  path in the Workbench does that: each of its requests for a window is made
  inside a click on the page (Approve on a request card, Show or Copy on a
  protected secret, creating a seed wallet, turning off Ask), and the sandbox
  frame opens its own window moments after one.
- Not announced: a web view that is moved, resized or reordered over a
  Workbench without being created or shown there. The shell has no such
  path. If it gets one, a card stays on screen without input (every event
  reads the opener) until the user closes it.
- The shell still does not assume how long a Workbench has been in front of
  the user: **the first window an opener asks for when it has none is an
  arrival, stamped at the moment it is asked for.** A hide closes every
  window, so the next request is again a first one. The cost: Confirm takes
  clicks no sooner than 2 seconds after the step's window was asked for
  (after the Approve click), instead of 0.75 seconds after the card appears,
  and approving a second request after the first card closed waits 2 seconds
  from that click too. Taking the "shown" announcement as the arrival would
  give the 0.75 seconds back; that would make an announcement open something
  for the first time, and is not built.
- Coming out from under another web view, or a hidden view around it, is an
  arrival only if the shell looked while it held (a window was asked for, or
  an event arrived for a card); otherwise a card asked for then is live after
  0.75 seconds, not 2. Input is not affected: every event reads the opener
  afresh. Before a card is displayed the opener is read once more; if it is
  no longer in view the card is closed instead.
- "One on screen" is counted for the native window, whichever web view in it
  asked. The cap of four is for the whole app.
- The support is installed on every Workbench web view: the one the app
  starts with (`build_main_window`) and each replacement
  (`build_main_child`), with the reload hook on both. Requests are admitted
  by the identity of the web view that made them, so one Workbench cannot
  reach what was installed for another. The account screen is not an opener.

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
they are in place. That call returns before doing anything unless both hold:

- The system is a macOS major version on which the probe has passed in CI.
  Today that is 26 only; for macOS 15 see "Not verified". It is a list, not a
  floor: on a version the probe has not run on, nothing says the non-public
  calls behave as rule 3 needs.
- The running process's own code signature carries
  `com.apple.developer.associated-domains` with
  `webcredentials:sandbox.avibe.bot` (read with `SecTask`, which reports what
  the kernel holds for the process). A build flag is not consulted. macOS
  refuses to start an ad-hoc build that claims this entitlement, so a
  development build can only be without it.

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
- **Holding a window off screen until the user has been there for a while.**
  Built once and removed: the page runs and arms while it waits, so the card
  is confirmable in its first visible frame.
- **Refusing a window shortly after another closed.** Replaced by the input
  rule, which protects the same click without refusing the window.

## Verification

- Unit tests in the module: the origin rule, which requests open a window, what
  a child may load, the commit rule, the attention rule and its interval, the
  size bounds, and the marker's two conditions.
- `desktop/src-tauri/examples/vault_window_probe.rs`, run by CI on macOS 26:
  real WebKit windows on two loopback origins with a disposable data store. The
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
    rule 4: a second window while one is on screen, and a window opened while
    the main window is hidden, are each closed without ever being on screen.
    For input, the test sandbox page arms itself half a second after it starts
    running and logs every click and key it receives, and the probe delivers
    clicks, a Return key and a Command key to the window every 30 ms. No
    input may be logged before 0.75 seconds after a card is first displayed
    (with a page that was armed long before it could paint); before 2 seconds
    after the card comes back with the main window, or the user returns from
    another app or un-hides the app; or before 2 seconds after an arrival for
    a card asked for at once. The probe also covers the card completely with
    another window of the app and checks the same if AppKit then reports the
    card as hidden; on the CI runner it did not, so that case is not proven.
    Clicks must be logged afterwards, so the check cannot pass by never
    delivering. For the time an event was made: after both intervals have
    passed, the probe hands a card a click, a Return key and a Command key
    dated a tenth of a second after the card appeared, dated before it
    appeared, and dated zero, and earlier, while the card is not yet live, the
    same three dated an hour ahead; none may be logged; the same three dated
    now must be. That the period starts only after the revealed card has been
    committed is not probed (the probe cannot stall the reveal's turn); a unit
    test covers that a displayed card whose block has not run is not in view. A card displayed while another window of the app is in front
    is the app's frontmost window and key, and neither changes when it starts
    to take input. With the Workbench's own web view hidden: its card is
    closed, `window.open` returns `null` for the sandbox and for a reserved
    page and no window is created, and after the web view is shown again a
    card asked for at once takes no input for two seconds. With another web
    view of the main window shown in front of the Workbench: `window.open`
    returns `null`. That web view is then moved aside, the Workbench reserves
    a window, and the web view is moved back over it; neither move is
    announced, so the reserved window is still there, and when the sandbox
    document commits in it the window is closed without ever being on screen.
    After that web view is removed a card takes no input for two seconds.
    With a replacement web view in the same window, which has a data store of
    its own, three times: the replacement gets the marker and a card that
    shares its data store and writes a cookie every 20 ms; then the
    replacement is hidden, as a retirement begins, or closed without being
    hidden. Each time the card's window is gone and WebKit reports its page
    closed (`_isClosed`) when the probe asks on the main thread right behind
    the hide or the close. One of the hidden ones is then taken through the
    rest of the sequence of `instance_store::retire`, written out in the
    probe because the probe does not link the library: the data store is
    erased, and 200 ms later its cookies are read back and must be none.
    Minimizing, a Space change and display sleep are not
    produced by the probe; for those, as for full covering, the claim rests on
    AppKit's documentation of the occlusion state.
  - **App not active.** A sandbox window is closed instead of appearing. This
    run takes no keyboard focus.
  - **The shell's own install** on the unsigned test executable: no marker and
    no window.
- These rules were also removed once by hand to see the probe fail: the
  opener's content controller kept, no URL scheme guard, reveal on the commit,
  two windows on screen, a window over a hidden main window. The input rule
  was not tried this way: its cases need the app activated, which is done only
  in CI.
- The two calls in `build_main_window` and in `build_main_child` that connect
  the module to a Workbench's web view have no automated check; the probe
  builds its replacement web views itself. Removing the install leaves the browser
  hand-off; removing the navigation hook leaves a sandbox window open across a
  Workbench reload. Both show in the manual check on a signed build.

## Not verified

**macOS 15 to 25.** Passkey PRF starts with macOS 15, so 15 would be the
natural floor. The probe was run once on a hosted macOS 15.8 runner. Every step
before the pixel check passed there, including windows being revealed, which
means the presentation update is delivered. The pixel check then failed: the
window stayed on screen and revealed, the probe never read the opener's colour
from it, and it never read the sandbox page's colour either. Either the
read-back does not see WebKit's content on that system, which would make the
check blind there, or the page was not displayed. The two cannot be told apart
without a macOS 15 machine. Until the probe passes on it, the marker is not set
on macOS 15, and those systems keep the browser hand-off. Follow-up: make the
read-back work on macOS 15 (or replace it), then add a CI job and the version.

**A page's last writes.** Destroying a card's page is asked of WebKit before
the erase is; the page's process is told a moment later. The probe's
retirement passed with a card writing cookies, but nothing proves that no
write from a card can land after the erase. The Workbench's own page is alive
through the erase in any case; that is the retirement's existing rule, not
this change's.

**A web view moved or reordered over a Workbench**, as said above: not
announced, not produced by the probe.

**A pause inside WebKit when a web view is created.** On hosted macOS 26
runners the app's main thread has been seen blocked for about five seconds in
`-[WKWebView initWithFrame:configuration:]` (`WebProcessPool::registerUserInstalledFonts`
→ `_sandbox_extension_issue`), in about 3% of the moments a sandbox window was
asked for right after another web view of the app had been torn down. It is
platform behaviour: it does not follow the shell's deadline, and it does not
occur when no web view is created. In the app it would be a delay (the
deadline is 60 s), and a click made during it is judged at the time it was
made. Whether it occurs on a real Mac is not known: in an RC walk, approve two
requests in a row and note any hang. The probe's own deadline is 15 s, and
its reservation cases report a round that outlasted the deadline as a pause
while still failing on a close of any other origin.

Only a signed build with a real passkey can show these:

- The shell's install passing its own admission. In every test it either
  refuses (the unsigned executable) or is entered one step below.
- The real origin, and how long its first paint takes in the window.
- The passkey sheet from the window, and passkey PRF from the sandbox iframe
  inside the app.
- Focus while a real system sheet is on screen, and the two-second wait after
  it closes.
- A release build. Tests run debug builds.
- The association being fetched after the app's first launch.
- For replacement web views: the marker and the support on one in the real
  shell (the two calls in `build_main_child` have no automated check); a real
  instance switch, and back on the local instance afterwards; a remote
  instance (a card from a remote HTTPS Workbench, the passkey sheet, passkey
  PRF from the sandbox frame there); the shell's own `instance_store::retire`
  with a card up; signing out with a card up; the account screen opened while
  a card is up.
