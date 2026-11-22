# Desktop App Lock (D7-7A)

## Contract

Based on origin/master `ca534deea6004823c9985347ecdd7cedfcfd91e3`
(2026-11-21 snapshot, including W1 #4036, Show share #4037, sign-in #4034,
Mini #4104, lazy Ask Vibey receiver #4077, and shared/iOS App Lock #4040). The approved product
specification is the orchestrator's `app-lock-plan.md` and `app-lock-design/`.
All seven recommendations were accepted on 2026-11-20.

One native owner protects every AppKit window and owns the local preference,
continuous-time departure deadline, device-owner authentication, and a single
memory-only pending entry. Default Off preserves existing behavior. An enabled
policy locks on cold start and after the selected departure threshold; changing
policy requires successful system authentication. No account password makes the
policy ineffective without discarding the saved preference. Availability is
rechecked on activation. Only LAErrorPasscodeNotSet disables protection; other
errors preserve the process's last value, initially true. Passcode restoration
starts Locked. Authentication focus changes follow the approved event table.

The owner covers content before presentation, hides Mini and update popovers,
and restores their prior state only after authentication. New windows inherit
the same protection through AppKit observation. Entry points defer their effect
until unlock. Content and Navigation use latest-wins; a foreground-only Action
fills an empty slot and never displaces pending work. Explicit menu/shortcut
commands retain their existing latest-wins behavior. Quit remains usable.
Only the main local Workbench can call the two settings commands. There is no
Web unlock API. Background Runtime and Agent operations continue.

## Boundaries

- Native implementation: `desktop/src-tauri`, including AppKit, LocalAuthentication,
  lifecycle, capabilities, main-document markers and entry routing.
- Shared Web panel, types, translations and adapters: iOS lane, now inherited
  from master without lane-specific edits.
- Vault's existing view-retirement checks must distinguish a temporary lock
  cover from actual replacement; locked content must never accept input.
- Ask Vibey uses the common native Content entry gate before enqueue or file
  inspection; its raw intent remains memory-only until unlock. W1, Show share,
  sign-in and Mini now come from master and retain the common native cover owner.

## Validation and delivery

Focused state-machine tests, AppKit coverage checks, desktop-client browser
scenarios against a compiled test peer, cargo test/clippy/fmt and Vitest. The
validation peer is compiled only for test/e2e, with a release-build prohibition.
Real Touch ID/password/Watch, lid/sleep, Mission Control and signed-package
behavior require the two Mac acceptance groups in the product specification.

Freeze a local commit for main to package. No packaging, installation or PR in
this lane before Max's local acceptance. GitHub review/CI gates follow acceptance.

## Native boundaries and review corrections

The coverage domain is document content in `NSApplication.windows`, including
unregistered native windows. AppKit status-bar, menu, and tooltip windows keep
their system controls; they are not documents. A native-only lifecycle alert
is individually exempted so Quit and its busy response never require unlocking.
Lifecycle alerts attach an asynchronous NSAlert sheet on the existing main
dispatch queue, then return immediately. Neither tao nor the dispatch queue runs
a nested modal loop; logout replies and authentication callbacks remain live.
Document titles are concealed along with their content and restore the latest
requested title after unlock. Mini and the update popover restore only their
latest desired visibility.

The view hooks operate at window content roots. Descendants of hidden content
cannot reveal pixels; reacting to the lock button's own internal layout would
reenter AppKit layout. Concealment is synchronous, drawing uses AppKit's normal
render cycle. Catalog parsing is cached and controls update only when their
rendered state changes.

The approved [event table](app-lock-desktop-event-table.md) is the timing and
input contract. Machine distinguishes in-flight verification (V), a possible
panel-induced resign (C), confirmed absence (A), a bounded return window (W),
and a resolved result (Q). Every end of verification retains C for at most two
seconds of return classification, without inventing absence from a reply or
extending an earlier deadline. Lifecycle cancellation invalidates the result
without manufacturing or discarding departure. A definite other regular app,
sleep or lock confirms the earliest departure immediately. No arbitrary LA
or idle timeout is added. Late Success cannot unlock or authorize persistence.

The automatic-prompt latch belongs to a real visit: panel return does not reset
it; a confirmed departure followed by actual return permits one new evaluation.
Only NSApp activation with agreeing public on-console/awake facts clears system
suppression hints. Inconclusive facts retain them, but explicit Unlock remains
available and fresh successful LA clears them. Return notifications alone never
impersonate foreground. Native callbacks feed observations and consume Machine
results; no native completion store or separate timer owns these decisions.
The additional NSRunningApplication/libc AppKit features expose the public
regular-application classification and process identity getter.

Restoring protected windows records a fresh Vault arrival so its existing
input guard applies even when LA did not change focus.

Pending entries distinguish navigation, content for Vibey, explicit commands,
and foreground-only actions; only the first two show the corresponding generic shared copy. Copy changes
relayout the native controls immediately, including at unchanged window size.
The owner never puts the content itself on a lock screen. Pending publication is serialized with
completion; older Workbench navigation is replayed after unlock unless a newer
Content, Navigation or explicit command superseded it. Dock/tray Open preserves
both the owner slot and older Workbench navigation/update queues. Deadline
changes found by another entry are marked for the timer to synchronize, and Off does not dispatch a periodic UI tick.

The first independent review examined `e899f374d2` and found the status/menu
coverage and activation-return ordering defects, plus pending copy, repeated
catalog parsing, modal callback placement, and Vault arrival issues. The second
review examined `295e3fe69e`: ten old issues were resolved, but
indefinite activation waiting repeated the LA timing root cause, and moving
runModal to the dispatch queue repeated the serial-executor blocking root cause.
The third native review at `68ebe612f9` found lifecycle cancellation was still
misclassified as a departure. Main stopped local patching and required the full
event/state contract. Table revision 1 (`53cd2d432a`) failed review; main resolved
T1-T4. Revision 2 (`3bccb5f035`) passed independent review, receipt `86028c1feb52`.
This implementation follows that approved table, including S1/S2 clarifications.
The native implementation at `dfde9a5ce5` passed independent review (CLEAN,
receipt `5b87cb526e7c`). Rebasing onto the merged shared/Ask changes produces
`2aa4475691`; the Machine remains unchanged. The only rebase conflict retained
Ask Vibey's crate-visible main-focus entry together with the existing lock gate.
The integrated Ask head `123caf2c53` passed independent review (CLEAN, receipt
`25721b1b9508`). Main then approved preserving pending work against pure Action
requests. The revised head `f26d0f28cd` passed independent review (CLEAN,
receipt `afd5d35ecfdf`). The 2026-11-21 master rebase requires a new exact-head
review. Its Machine timing/authentication rules and compose entry gate are
unchanged from that frozen head. The native lifecycle sheet owner additionally
serves W1 unsaved-close confirmations, as approved by main on 2026-11-21.

Shared updates are copied without independent edits from `50c57df6` (availability
contract docs/type comment) and `3431474f` (theme-token lock-frame sizes). The
AppKit sizes follow that kit; shared copy remains the embedded en/zh catalogs.

## Current local evidence

The combined RC preparation passes 314 Desktop library tests (one existing ignored; 15 App Lock
scenario groups, including the core event matrix across all three policies),
273 runtime-host tests (three pre-existing ignored), 32 desktop-client e2e cases,
54 shell-boundary tests and 56 focused Vitest cases. Normal and e2e-feature all-target clippy pass with
warnings denied. Rust formatting, desktop-client TypeScript, and UI build pass.
Design typecheck passed for the native baseline; the integration does not change
design files. The release guard compiles the normal release and proves that a
release containing authentication substitutes is rejected. Updated native
light/dark captures were inspected for the original native owner. The master
rebase reruns all listed automated gates against the merged dependencies.

The native peer installs the production observer map on test-owned notification
centers, so it exercises the actual event routing without broadcasting system
lock/sleep signals. Its tests also call the production Dock/menu/deep-link/Mini
entry gates, real native Quit/busy sheets and status-item/menu controls. Public
foreground facts, process-activation observations, capability/evaluation results
and continuous time remain deterministic test inputs. This is not evidence of
real LA focus latency or real session-state behavior.

## Acceptance handoff

Automated native browser tests use the shipped settings component and adapter
against a compiled AppKit owner. Authentication/capability results, foreground/system facts,
and continuous time are substituted. They include real status-item/menu windows
and the native lifecycle alert. This is not proof of real Touch ID, real Vault
approval, or Mission Control snapshots.

Main owns packaging and Max's acceptance. Test both an ad-hoc local package and
a signed TEST RC:

- **First gate:** measure real Touch ID success-to-NSApp activation latency;
  the return must consistently fit the two-second window. If it does not, main
  must revise that policy before acceptance. Include global shortcut/deep link
  from background and explicit retry after expiry.
- Quit with the main window minimized or app in background: sheet must be
  visible and usable, and dismissal must release the pending lifecycle request.
- Touch ID Mac: default Off; authenticated policy changes and cancellation;
  cold launch; Cmd-Tab with Immediately, 59/61 seconds for One Minute, and Five
  Minutes; screen lock, sleep, and session switching.
- Touch ID, login-password fallback, cancellation, and Apple Watch approval;
  no second lock caused by the authentication panel's focus transitions.
- Main, standalone, Show, and real Vault windows conceal content and titles;
  clicks and typing cannot reach covered content; Mission Control and screen
  sharing show the cover. New windows inherit protection. Vault's return guard
  applies after unlock. Mini/update popovers hide and restore correctly.
- Deep links (startup, second launch, Apple event), Dock, tray, menus, and global
  shortcuts wait for unlock; newer Content/Navigation replaces earlier work,
  while foreground-only Dock/tray Open preserves it.
  Cancellation retains it, process exit discards it. Quit, including the Runtime
  choice and busy response, works without authentication.
- Mac without usable Touch ID, including a closed lid with external keyboard:
  retain the reported Touch ID hardware type but offer Password when biometric
  evaluation fails. A user without a login password cannot enable effective
  locking; the saved preference survives and resumes when authentication becomes
  available again.

Ask Vibey #4035 is integrated at the start of `compose::enqueue`, before file
validation or share allocation. Its consumer test E30 uses the production queue,
WKWebView transport, and the shipped lazy `DesktopCompose` component, which
imports `DesktopComposeMount` and installs the actual receiver. It proves latest-intent
replacement, cancellation followed by Dock/tray Open without losing content,
retained navigation, deferred file opening (a selected file is
created only after the request), Off behavior, and a previously admitted share
waiting through a lock. Only the final composer target records input instead of
sending a message. The peer uses a nonpersistent WKWebView store, test-owned file
and notification preferences, and Runtime dependencies that panic if used.
The eventual PR must describe this semantic integration.

For acceptance, select text, a URL, and files through the actual Ask Vibey Service
while locked: no selected content appears on the lock screen; cancel retains it;
success places the newest selection in the input without sending. Also verify
an already queued selection waits through a subsequent lock. The native E31
fixture builds physical App/Show WebviewWindows with W1's native marker, the
production Show document-title follower and title-bar accessory. It checks an
existing window and one created while locked, covering content and titles and
restoring document titles after unlock. This is evidence for the physical cover
boundary, not W1 Runtime-receipt authorization or real sign-in. The integrated
package still needs the corresponding launch/menu/sign-in acceptance checks.

## Revised freeze and package schedule

On 2026-11-20 main approved Action-only-if-empty semantics before packaging.
The pure pending-entry queue tests every kind pairing by consuming the surviving
payload; native e2e covers Dock/tray preserving Navigation and the real Ask
composer flow after cancelled authentication. Specific menu/shortcut commands
remain distinct from foreground-only Action, with no change to their behavior.
No timing/authentication Machine rules changed.

On 2026-11-21 main requested rebasing after #4034/#4036/#4037 and Mini #4104
merged. The integration snapshot is `ca534deea6`; Mini no longer requires a
separate branch. The first fixed-signature acceptance package belongs to main
and packager session `sestwvy8p84ue`. This lane does not build/install packages.
The [smoke checklist](app-lock-desktop-smoke.md) separates Agent-runnable evidence
from Max's real authentication and hardware checks.

Rebase conflicts combined both lanes' AppKit features and main Workbench
initialization markers. Duplicate AppKit features were removed. Main's explicit
launch link retains precedence over a Computer Use setup-resume link; the
locked launch retains the same ordering. The W1 native marker is crate-visible
solely so the physical-window peer can consume the production constant. No
new test bypass is exposed to a release build.

W1 now asks before quitting with unsaved page state. Its ordinary dialog would
be concealed by App Lock, preventing an answer. When App Lock is effective,
`confirm_discard` therefore uses the existing asynchronous native lifecycle
sheet and its exact window exemption. The body/buttons use shared generic
`apps.editor.confirmDiscardClose`, `apps.editor.discardAndClose`, and
`common.cancel` copy; neither the page-supplied message nor its title is exposed.
Cancel retains state; only Discard permits the existing close/quit chain to
continue. Off retains W1's ordinary dialog. The consuming native test queries
a real WebviewWindow's private close guard, answers Cancel and Discard while
locked, checks that underlying content stays covered and no LA is requested,
and verifies that unlocked Cancel does not manufacture absence. No modal loop
or new lifecycle exemption registry was introduced.

The W1 discard sheet retains the actual closing window as its parent and uses
W1's existing restore/focus path for that window. Quit/busy keep their main
parent. A same-head review found that using main for every sheet could reopen
a hidden Workbench or put the question behind a minimized main window. The
consuming test now asserts the actual NSAlert parent and preserved main-window
visibility/minimization, with both Cancel and Discard, and repeats unlocked
Cancel with main hidden. This does not change the effective-policy boundary,
generic copy, exact sheet exemption, or Machine timing rules.


## RC preparation on 2026-11-22

The master-only lane rebases onto `ba2a136e975d15b8a6f5397e775519540cd0b28f`.
It includes integration fix `bde21e8e` for the capability inventory and fixes
both new fixture lint violations without changing the lint baseline. The
four-button signature fix `3722f16f` applies only after #4108; the master-only
branch still uses the older title-bar API.

A separate local combined worktree rehearses #4108 at `e15705dab2` and #4105
at `dd65a36a06` before this lane's semantic integration. Main owns the merge
order: both dependencies first, then rebase App Lock and apply the prepared
integration commit. No dependency branches are edited or pushed by this lane.

All four native Show actions enter the existing Command defer gate before any
page eval or native callback. Replay rechecks the window's registered origin
and route. Deferred Annotate/Share restores and focuses that Show window before
opening its page UI; Chat/Browser retain their own destinations. The source-compiled rendering probe supplies an explicit lock-free
adapter; the App Lock peer consumes the production gate and actual buttons.
Native notification answers also enter Command defer before `answer::submit`.
The operation ID is allocated at the original action, retained with revision
and payload, and supplied unchanged after unlock. The transport deadline starts
only then. Pure Action cannot replace any occupied slot, including Command;
a later explicit Command/Content/Navigation replaces it. Cancellation retains
memory-only intent, process exit discards it, and no answer text is rendered.
Replacing a pending notification answer emits one existing-style follow-up
with the original Session link: title `answer.notDelivered` and the approved
new body `answer.answerAgain` ("Open Avibe to answer again." /
"打开 Avibe 重新回复。"). It names no private content and does not falsely
claim a connection failure. Quit-discard remains silent. Replacement callbacks
run outside the owner locks and never run for replay or rejected Action entries.

These additions implement main's 08:17, 08:22, 08:39 and 08:41 decisions. They do not change
Machine's timing/verification model, D2a's Runtime idempotency/outcome rules,
or notification preview policy. Native tests consume actual
NSButtons and the native notification admission path; the latter reaches the
shipped WebView answer receiver with only its final Runtime POST substituted.
Replacement tests consume the actual queue transition and native notification
content builder, substituting only the final OS delivery sink; test-owned
notification preferences are enabled without any production writes.
Real notification-center UI and authentication remain local-package checks.
