# Desktop App Lock acceptance smoke

## Package boundary

As of 2026-11-21, #4034, #4036, #4037 and Mini #4104 are merged. The App Lock
branch rebases onto master snapshot `ca534deea6`, which also contains the lazy
Ask Vibey receiver #4077. Main and packager session `sestwvy8p84ue` own the first
fixed-signature acceptance package. This implementation lane does not package
or install. The previously separate Mini branch is already included by master.
Record the integrated SHA and package identity before running this checklist.
Run the two hardware groups below with both the ad-hoc local package and the
signed TEST RC. A mock-authentication peer does not satisfy real-device checks.

## A. Agent-runnable

These checks use isolated fixtures or an explicitly assigned acceptance profile.
Do not restart the Agent runtime, change production account settings, or install
a package without main's packaging instruction.

| Check | Expected result / evidence |
| --- | --- |
| Build and substitute boundary | Desktop/runtime-host unit tests, normal/e2e clippy, fmt, focused Vitest, UI build and desktop-client typecheck pass. Normal release check passes; release with `app-lock-e2e` fails at the explicit compile gate. |
| Native peer scenarios | Exercise the real AppKit owner with substituted LA/time/system facts: cold launch, all three enabled thresholds, cancellation, two-second return ordering/expiry, lifecycle sheets, capability recovery and native entry routing. |
| Real Ask consumer | Production enqueue, WKWebView transport and shipped lazy DesktopCompose/ DesktopComposeMount: locked requests do not allocate shares or open files; latest Content/Navigation wins; cancellation followed by Dock/tray Open retains the payload and pending hint; unlock stages without sending. Previously admitted shares wait while locked. |
| Native Show actions | While locked, click each of Annotate, Open Chat, Share and Open in Browser: no page event, popover or browser opening before unlock. After unlock the latest action runs once; Annotate/Share restores and focuses the owning Show window so its UI is visible in front of Workbench. Cancel and replace an action; navigate the window during the wait and confirm the stale action is not replayed. The accessory labels are generic and its bound title remains concealed. |
| Notification answers | While locked, choose a quick reply and separately type Reply. Neither submits before unlock; cancel then click Dock and confirm the answer survives. After unlock exactly one request carries the original operation ID and revision. Replace a pending answer with a Command, Content or Navigation: one generic not-delivered notice opens the original Session, without including the answer/source text. Quit-discard stays silent. Stale/already-handled follow-up uses D2a's existing notices. Off submits as before. |
| Queue priority | Action fills only an empty slot. It cannot replace another pending intent or clear an older Workbench navigation/update. Explicit commands retain their existing deferred execution behavior. |
| Default Off and settings | Settings is under General. Off preserves existing window/entry behavior. Setting changes commit only after successful system verification; cancellation preserves the previous value. Automated evidence uses the peer; coordinate with Max for a real verification. |
| Windows and privacy | On the designated package/profile, inspect main, independent App/Show, new windows and an assigned Vault fixture. Content and document titles are covered; input cannot reach content; Mini/update popovers hide. Inspect light/dark, resize, Mission Control and screen-sharing captures with test content. System tray/menu controls stay usable. |
| Foreground entries | With a test profile already locked, exercise deep links, Dock, tray, shortcuts and Services selections. Observe generic hints only; after Max unlocks, confirm the requested destination/input. Test new Content versus Navigation replacement in both directions. |
| Lifecycle | With Max available if verification is needed, test Quit/busy sheets from main, minimized main and background/Dock. Sheets remain visible and responsive; cancellation releases the lifecycle request and does not create an absence deadline. Quit never requires unlocking. With unsaved Editor state, the generic Discard/Cancel confirmation stays visible without revealing the page title or message; Cancel keeps state and Discard permits the existing close/quit chain. Hide or minimize main, then close an unsaved App window: the sheet belongs to that App window, on its display; main stays hidden/minimized. |
| Integrated regressions | On the combined master-based package, verify W1/P3/sign-in windows inherit covers and Mini sizing still works. Confirm background Runtime/Agent work continues. |

The current native peer is test-only, with a nonpersistent WebView store,
temporary files/preferences and inert Runtime dependencies. Its W1 physical
window case uses real WebviewWindows and the production Show title follower
and accessory; Runtime-receipt authorization and actual account sign-in remain
package checks. Do not infer real
LA focus timing, real session recovery or Vault approval from these substitutes.

## B. Needs Max

The Agent may observe timing/logs and record outcomes; Max supplies credentials,
biometrics and physical interactions. Never ask Max to disclose a password.

### First gate: authentication and foreground

- With Touch ID and separately with login password, measure successful LA reply
  to `NSApplicationDidBecomeActive`. The return must reliably fit the approved
  two-second window and produce one unlock, without another automatic prompt.
  Confirm coreautha/password panels are not mistaken for another regular App.
- Repeat from a background global shortcut and a background deep link. Exercise
  cancellation and a late return: an expired success cannot unlock; explicit
  retry must remain usable. If normal successful verification cannot reliably
  restore the foreground in two seconds, stop acceptance and ask main to decide
  the return policy.

### Touch ID Mac group (plan §5)

- Enable each policy with real verification. Test cold launch, Immediately via
  Cmd-Tab, One Minute at 59/61 seconds, Five Minutes, and Off. Cancel a setting
  change and confirm the saved policy is unchanged.
- Unlock by finger, login password and Apple Watch; cancel and retry. System
  authentication panels must not cause repeated immediate locking.
- Lock/unlock macOS, sleep/wake, and fast-switch users. Confirm protection and
  recovery after returning; record any inconclusive public-facts diagnostic.
- While locked, select text and files in Finder/another App and invoke Ask Vibey.
  Cancel Touch ID, click Dock or tray Open, then unlock: the selected content
  still reaches the composer and is not sent. Test a URL and a share admitted
  before locking as well.
- With test content, confirm main/App/Show/Vault windows and Mission Control
  reveal no protected content. After unlocking, Vault's arrival/input delay
  starts again; use only an explicitly assigned test approval.
- Confirm minimized/background Quit sheets can be answered without unlocking.

### No usable Touch ID group (plan §5)

- Use a Mac without usable Touch ID, or physically close the lid with an
  external keyboard/display. The native button offers login password even when
  the hardware type still reports Touch ID; password verification unlocks.
- Where available, test unenrolled/locked-out biometrics and password fallback.
- Use an existing dedicated passwordless test user to verify that App Lock is
  ineffective and General explains why. Do not remove a daily user's password.
  If no such test user exists, mark this real-device item untested rather than
  changing account security solely for acceptance.
- In an approved test account, restoring a password under a stored enabled
  policy must engage Locked. Transient LA availability failures must keep
  existing protection, rather than being interpreted as no password.

## Record and handoff

For each package/hardware group, record pass/fail/untested, integrated SHA,
package signing identity, macOS/hardware context, and the first-gate timing.
Attach only test-content captures and scrubbed diagnostics. Main owns Max's
acceptance decision. A PR, exact-head Codex review and CI follow acceptance.
