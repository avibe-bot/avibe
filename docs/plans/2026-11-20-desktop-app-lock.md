# Desktop App Lock (D7-7A)

## Contract

Based on origin/master `3913091383` (including #4031). The approved product
specification is the orchestrator's `app-lock-plan.md` and `app-lock-design/`.
All seven recommendations were accepted on 2026-11-20.

One native owner protects every AppKit window and owns the local preference,
continuous-time departure deadline, device-owner authentication, and a single
memory-only pending entry. Default Off preserves existing behavior. An enabled
policy locks on cold start and after the selected departure threshold; changing
policy requires successful system authentication. No account password makes the
policy ineffective without discarding the saved preference. Availability is
rechecked on activation. Authentication focus changes cannot start a new timer.

The owner covers content before presentation, hides Mini and update popovers,
and restores their prior state only after authentication. New windows inherit
the same protection through AppKit observation. Entry points defer their effect
until unlock; a newer pending entry replaces the older one. Quit remains usable.
Only the main local Workbench can call the two settings commands. There is no
Web unlock API. Background Runtime and Agent operations continue.

## Boundaries

- Native implementation: `desktop/src-tauri`, including AppKit, LocalAuthentication,
  lifecycle, capabilities, main-document markers and entry routing.
- Shared Web panel, types, translations and adapters: iOS lane, cherry-picked
  from the orchestrator-provided commit before the freeze.
- Vault's existing view-retirement checks must distinguish a temporary lock
  cover from actual replacement; locked content must never accept input.
- W1 and Ask Vibey are independent lanes. Their final integration must retain
  the common native entry gate; Ask Vibey payloads remain memory-only until unlock.

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
Lifecycle alerts run on the existing main dispatch queue outside tao callbacks.
Document titles are concealed along with their content and restore the latest
requested title after unlock. Mini and the update popover restore only their
latest desired visibility.

The view hooks operate at window content roots. Descendants of hidden content
cannot reveal pixels; reacting to the lock button's own internal layout would
reenter AppKit layout. Concealment is synchronous, drawing uses AppKit's normal
render cycle. Catalog parsing is cached and controls update only when their
rendered state changes.

A LocalAuthentication reply can arrive before AppKit restores activation. The
owner retains that reply and its authentication epoch, requests activation, and
finishes only when the app is active. Existing content stays protected through
that transition. Sleep/session departures invalidate the retained reply as
well as the LAContext. Restoring protected windows records a fresh Vault arrival
so its existing two-second input guard applies even when LA did not change focus.

Pending entries distinguish navigation, content for Vibey, and other actions;
only the first two show the corresponding generic shared copy. The owner never
puts the content itself on a lock screen. Pending publication is serialized with
completion; older Workbench navigation is replayed after unlock unless a newer
locked intent superseded it. Deadline changes found by another entry are marked
for the timer to synchronize, and Off does not dispatch a periodic UI tick.

The first independent review examined `e899f374d2` and found the status/menu
coverage and activation-return ordering defects, plus pending copy, repeated
catalog parsing, modal callback placement, and Vault arrival issues. The above
changes address that inventory; the final candidate needs a fresh exact-head
review. This is the first findings-bearing review head, not a clean review.

## Acceptance handoff

Automated native browser tests use the shipped settings component and adapter
against a compiled AppKit owner. Only authentication results, foreground state,
and continuous time are substituted. They include real status-item/menu windows
and the native lifecycle alert. This is not proof of real Touch ID, real Vault
approval, or Mission Control snapshots.

Main owns packaging and Max's acceptance. Test both an ad-hoc local package and
a signed TEST RC:

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
  shortcuts wait for unlock; a newer pending intent replaces the previous one.
  Cancellation retains it, process exit discards it. Quit, including the Runtime
  choice and busy response, works without authentication.
- Mac without usable Touch ID, including a closed lid with external keyboard:
  retain the reported Touch ID hardware type but offer Password when biometric
  evaluation fails. A user without a login password cannot enable effective
  locking; the saved preference survives and resumes when authentication becomes
  available again.

Ask Vibey #4035 is not in the current base. After it merges, this lane must rebase
and put the content-kind gate before `compose::enqueue` reads files or writes the
share queue, add its integration test, and describe the semantic integration in
the eventual PR. Main confirmed this ownership on 2026-11-20. The generic pending
content test does not claim actual Ask Vibey integration. W1 #4036 needs the
corresponding window/menu acceptance checks after integration.
