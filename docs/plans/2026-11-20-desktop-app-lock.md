# Desktop App Lock (D7-7A)

## Contract

Based on origin/master `1da3fb05d7` (including #4031, Ask Vibey #4035 at
`c553f1004e`, and the shared/iOS App Lock #4040 at `1df64a3d7a`). The approved product
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
  inspection; its raw intent remains memory-only until unlock. W1 remains an
  independent lane and must preserve the common gate when integrated.

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
requests; the revised candidate requires another exact-head review.

Shared updates are copied without independent edits from `50c57df6` (availability
contract docs/type comment) and `3431474f` (theme-token lock-frame sizes). The
AppKit sizes follow that kit; shared copy remains the embedded en/zh catalogs.

## Current local evidence

The integrated implementation passes 238 Desktop library tests (15 App Lock
scenario groups, including the core event matrix across all three policies),
252 runtime-host tests (three pre-existing ignored), 27 desktop-client e2e cases,
and 41 focused Vitest cases. Normal and e2e-feature all-target clippy pass with
warnings denied. Rust formatting, desktop-client TypeScript, and UI build pass.
Design typecheck passed for the native baseline; the integration does not change
design files. The release guard compiles the normal release and proves that a
release containing authentication substitutes is rejected. Updated native
light/dark captures were inspected. The Action-priority revision reruns Desktop
tests, native e2e, clippy, formatting, desktop-client typecheck and the release
guard; runtime-host, Vitest and UI build evidence is retained from the previous
CLEAN integration because their implementation and dependencies did not change.

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
WKWebView transport, and shipped composer receiver. It proves latest-intent
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
an already queued selection waits through a subsequent lock. W1 #4036 needs the
corresponding window/menu acceptance checks after integration.

## Revised freeze and package schedule

On 2026-11-20 main approved Action-only-if-empty semantics before packaging.
The pure pending-entry queue tests every kind pairing by consuming the surviving
payload; native e2e covers Dock/tray preserving Navigation and the real Ask
composer flow after cancelled authentication. Specific menu/shortcut commands
remain distinct from foreground-only Action, with no change to their behavior.
No timing/authentication Machine rules changed.

Packaging is on hold until #4034/#4036/#4037 merge. Main will then request one
package from master + App Lock + `fix/mini-input-card-width`, preserving the
features in the installed w1.x line. The [smoke checklist](app-lock-desktop-smoke.md)
separates Agent-runnable evidence from Max's real authentication and hardware
checks. Do not build or install the standalone App Lock branch meanwhile.
