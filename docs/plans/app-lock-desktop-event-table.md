# Desktop App Lock: event and state contract

Status: revision 2 passed independent table review at `3bccb5f035`
(receipt `86028c1feb52`). Implementation follows the approved table.
Decision owner: main. Reviewer: `sesmsfnku7fjm`.

Authoritative decisions: main's `app-lock-desktop-table-decisions-1210.md`
(2026-11-20), the public-signals clarification `b15574d1e709`, and shared
availability contract `50c57df65c51d4a2ecddfbe2d94e393be11dcb83`.

Three reviewed heads (`e899f374d2`, `295e3fe69e`, `68ebe612f9`) contained
findings in the departure/authentication family. This table replaces local
patching with an input contract. No native implementation change is permitted
until the independent reviewer approves it. That approval was received on
2026-11-20; the next gate is exact-head implementation review.

## A concrete case

An unlocked user with One Minute chooses Quit at time 10, then Cancel at time
12, and continues working until time 100. Neither showing nor dismissing the
sheet is a departure. With no prior resign, `away_since` stays absent and the
app remains unlocked.

If that user actually switches to another app at time 20, the departure records
20. A busy response, redraw, deferred link, or verification cancellation at time
30 cannot erase or move 20. At time 80, One Minute locks. Actual foreground
return evaluates the deadline before clearing the absence.

During LA verification, a resign is initially ambiguous. If it is C=20 and a
lifecycle cancellation ends verification at time 30, C survives until a real
foreground observation or expiry at 32. At expiry, the absence starts at 20,
not 30 or 32. A screen lock or definite switch to another app at 31 immediately
confirms departure from 20. Conversely, a timely foreground return resolves
the focus excursion without manufacturing a departure.

## State and invariant vocabulary

- `A`: `away_since`, the first confirmed departure in the current absence.
- `C`: earliest observed resign while verification is in flight. It may be
  caused by the LA panel, so it is not confirmed A until its classification ends.
- `V`: in-flight LA verification, with purpose (Unlock/Change) and epoch.
- `W`: a bounded return transition after V ends by any means. It retains C and
  an end timestamp; a matching LA reply can also carry an outcome. An existing
  earlier deadline is never extended by cancellation, duplicate replies or UI.
- `R`: the outcome and original callback timestamp inside W when one exists.
  Success is consumable only while age < 2 seconds, with real foreground.
- `Q`: resolved outcome awaiting native consumption/persistence. It is not an
  activation token. New evaluations and automatic prompts cannot overtake it.
- `B`: suppression reasons from system-departure hints. B suppresses automatic
  evaluation, but never permanently prevents an explicit Unlock button tap.
- `P`: whether an authentication attempt has already been used on this visit.
  `Used` suppresses automatic re-prompt; `Fresh` permits one eligible automatic
  attempt. Panel return is part of the same visit; a confirmed departure and
  subsequent actual return starts a new visit.
- `L`: latched lock. Ordinary return/cancellation cannot unlock it. Authorized
  successful verification can; Off or confirmed missing passcode makes locking
  ineffective under the shared contract.

`First(t)` sets A only when absent, retaining an earlier A. `Confirm(t)` uses
First(min(C,t)) when C exists, otherwise First(t), and clears C. `Preserve`
creates, clears and moves nothing. Real foreground return evaluates Due(A,t)
before clearing A. Time is continuous monotonic seconds, including sleep.

`Due` locks Immediate whenever A exists, One Minute when `t-A >= 60`, and Five
Minutes when `t-A >= 300`. Without A it does nothing. `Keep` retains L. A locked
app stays locked until successful verification or the explicit Off/missing-
passcode exceptions. Passive checks may apply Due but cannot restart A.

### One end-of-verification rule

All endings of V (reply, cancellation, lifecycle cancellation or epoch
invalidation) preserve C and enter the same two-second W. If no C or pending
reply exists, invalidation has no focus transition to wait for. LA reply time
is captured before dispatch to main; delayed dispatch cannot renew its TTL.
No C is invented from a reply timestamp.

- Actual foreground within W clears C. It can consume an unexpired matching
  reply; a cancelled epoch can never become successful again.
- W expiry confirms C as A, preserving an earlier A. With no C it preserves A.
  A retained successful reply expires as Cancelled for Unlock, Failed for
  Change. No stale Change grants persistence authority.
- A definite system departure or definite foreground switch away during V/W
  immediately Confirm(event time), invalidates verification and closes W. An outstanding Change sender receives
  `cancelled`, including a resolved Q discarded before persistence.
  It does not wait two more seconds, because departure is already established.
- Lifecycle cancellation may answer its settings sender immediately, but W
  continues classifying its retained C. Repeated cancellation cannot extend W.
- Foreground expiry checking happens before any success consumption. A fresh
  real return after expiry can begin a new verification; it cannot reuse the
  old result. The native test must count fresh LA calls separately.

There is deliberately **no maximum V duration**. Main rejected a 60-second
limit: idle while a Change panel is open is treated like idle in an unlocked
app, which this feature does not protect. Unlock already has L=true. A genuine
app switch/system departure is different and is handled immediately. An
unlocked screen saver without screen lock is likewise not an App Lock event.

### Automatic prompts and actual return

P is set Used by every begun evaluation, rejection does not consume an attempt.
Cancellation/failure/expiry keeps Used for that visit. A confirmed departure
arms a new visit; on actual foreground return P becomes Fresh, unless an
already-running evaluation is serving that return. A panel-only return never
resets P. Sleep/screen lock followed by actual return is a new visit even when
L was already true. A return can begin at most one automatic evaluation; begin
sets P Used before calling LA. Explicit unlock can retry regardless of P.

### Native observations and B reconciliation

The app-level coordinator is entered by `NSApplicationDidBecomeActive`, never
by an arbitrary window focus. It samples public `CGSessionCopyCurrentDictionary`
with a present, correctly typed `kCGSessionOnConsoleKey == true`, plus
`CGDisplayIsAsleep(CGMainDisplayID()) == false`. Combined with actual NSApp
activation, these facts clear B together. A dictionary that is empty/malformed,
a sleeping display, or an off-console session retains suppression and records
a diagnostic. No undocumented screen-lock dictionary key is used. This is the
public-signals policy chosen by main, not a claim that these APIs independently
expose the graphical screen-lock state.

A fresh, non-expired successful explicit LA verification also clears B. The
manual Unlock path can start while B is set; stale/rejected LA results cannot
clear B. Availability still follows the shared rule, so a transient error must
not turn a blocked/locked app into an unprotected one.

The LA reply-time `NSApp.isActive` sample settles W/C/A only; it does not
recompute B. The fresh accepted LA success rule is the explicit exception.

System departure notifications immediately record a conservative departure
and suppress automatic prompts. Their matching return notifications request
reconciliation; no individual pair can decrement B into an unprotected state.
A trusted app activation (or fresh LA success) recovers all stale hints at one
coordinator. This avoids permanent denial after a missed return notification.

Native source pairs: NSWorkspace willSleep/didWake, screensDidSleep/screensDidWake,
sessionDidResignActive/sessionDidBecomeActive, and distributed screenIsLocked/
screenIsUnlocked. Distributed names remain hints, not a public screen-state API.

A definite app switch while V/W exists must also be observable if Avibe already
resigned to LA. Use NSWorkspace didActivateApplication for a different regular
foreground application; the system LA UI is an authentication excursion, never
this definite-switch input. Native classification must distinguish its own LA
UI from a user app, and test that distinction. An unknown/helper-only activation
is not proof of definite departure; it keeps the existing candidate/return rule.
The public activation-policy filter and real LA behavior are smoke-test items.

## Core Machine event × state table

Each core row and its branches runs against Immediate, One Minute and Five
Minutes. P's default in every row is Preserve; its explicit changes are listed.
The final column applies to all three policies through the defined Due rule.

| ID | Input / precondition | A | V / W / C / epoch / B | P | Lock/persistence effect |
| --- | --- | --- | --- | --- | --- |
| E01 | Initialization, available and saved enabled policy | Absent | Empty verification and B | Fresh | All three start Locked |
| E02 | Actual resign, effective, no V/W | First(event) | No verification mutation | Arm next real visit | Due |
| E03 | Repeated real departure in same absence | Preserve earliest A | Preserve | Preserve armed visit | Due; deadline never restarts |
| E04 | Ambiguous resign while V/W exists | Preserve | First observed resign becomes C; no deadline restart | Preserve | Due from A only |
| E05 | Trusted actual foreground, no V/W | Due then clear | Reconcile B from public facts | Fresh if confirmed departure armed visit, else Preserve | Keep after Due; eligible automatic begin once |
| E06 | Foreground before LA reply | Due then clear | Clear C; retain V; reconcile B | Existing V counts as Used; no duplicate | Keep after Due |
| E07 | Begin Unlock, locked, available, no V/W/Q | Preserve | Create epoch/V; explicit tap may proceed despite B, automatic may not | Used | Keep |
| E08 | Begin Change from unlocked settings caller | Preserve | Create epoch/V; do not write policy | Used | Keep |
| E09 | Begin rejected: unavailable, another V/W/Q, automatic blocked by B, or Unlock while unlocked | Preserve | Preserve current verification/C/B | Preserve | Keep; busy/unavailable reply |
| E10 | Current Success, real foreground, age < 2 seconds | Due then clear as actual return | End V via W and immediately resolve to successful Q; clear C; fresh success clears B | Used | Only successful commit unlocks/persists |
| E11 | Current Cancelled/Failed/Unavailable, real foreground | Due then clear as actual return | Same end rule; matching unsuccessful Q, no write | Used for this visit | Keep after Due |
| E12 | Current reply, not foreground, with C | Preserve | End V; retain C/outcome in W, deadline original reply+2 | Used | Due from A only; no success commit |
| E13 | Current reply, not foreground, no C | Preserve | End V; retain outcome in W; C stays absent | Used | Due from A only; no invented absence |
| E14 | Actual foreground before W deadline | Due then clear | Clear C/W; consume only unexpired current result; invalidated epoch stays invalid | Used for panel return; fresh real visit follows E05 | Success may commit; other outcomes Keep |
| E15 | W expiry with C, including delayed dispatch / late arrival | First(C), Due before any actual-return clear | Clear C/W; stale success => Unlock Cancelled / Change Failed; no stale write | Used until a later real return; arm next visit | Due; later arrival can only start fresh LA |
| E16 | W expiry without C | Preserve; Due before any actual-return clear | Same outcome invalidation; no synthetic departure | Used unless already armed real return | Due from existing A only |
| E17 | Consume Q / persist authorized Change | Preserve | Consume once; failed write keeps old policy and yields Failed | Used on success/failure/cancel | Success unlocks; other outcomes Keep |
| E18 | Old/duplicate reply, invalidated epoch | Preserve | Ignore; no V/W/C/B resurrection or extended deadline | Preserve | Keep |
| E19 | cancel_for_lifecycle / epoch invalidation | Preserve | End V through same W; retain C, invalidate outcome/context, answer cancelled sender; existing W deadline cannot extend | Used if cancelling an attempt; otherwise Preserve | Keep; no synthetic departure |
| E23 | Definite lock/sleep/display sleep/session loss | Confirm(event time) | Invalidate V/R/Q/context; close W/C; add B hint | Arm next real visit, never prompt while absent | Due |
| E24 | Repeated/overlapping system departure | Preserve earliest A (Confirm earlier C if present) | Accumulate hints, invalidate once; no reusable result | Preserve armed visit | Due |
| E25 | Return notification, foreground/public recovery not established | Preserve | Request reconciliation; retain B and invalidation | Preserve | Due; wake alone is not foreground |
| E26 | Trusted actual return with public facts agreeing after system departure | Due then clear | Recompute/clear all B together; no old result revived | Fresh for new visit, then Used on automatic begin | Keep after Due; fresh LA required |
| E27 | Timer / is_locked / entry deadline check | Preserve except C confirmation on W expiry | Resolve W expiry; drain Q on main; no UI-created departure | Preserve except E15's visit arming | Due |
| E34 | Same-policy request / unknown policy / unauthorized caller | Preserve | No mutation; same policy succeeds without LA, others reject as specified | Preserve | Keep; no verification/persistence |
| E35 | Settings change cancelled/failed/busy/unavailable/write failure | Preserve except separately observed foreground | Existing E09/E11-E19 govern end of verification; retain old policy | Preserve for rejected begin; Used for an attempted evaluation | Keep, subject to existing Due |
| E36 | Confirmed passcodeNotSet, or successful committed Off | Clear (protection ineffective) | Do not start LA while unavailable; keep saved policy on missing passcode | No automatic prompt while ineffective | Unlocked/ineffective |
| E37 | Passcode becomes available under stored enabled policy | A absent | Availability true; no reusable verification | Fresh | Locked, as cold initialization |
| E39 | didBecomeActive reconciliation: valid awake/on-console vs malformed/off-console/asleep | No new departure; actual return follows E05/E06/E14/E26 | Agreeing public facts clear B, other results retain it; explicit Unlock remains available | Governed by actual-return classification, never window focus | Cannot unlock on facts alone |
| E40 | Definite other-app foreground during V/W | Confirm(event time) | Invalidate V/outcome/context; close W/C; no successful write | Arm next real visit | Due; no wait for a reply |
| E41 | V still pending, no definite departure/return/reply, even after long idle | Preserve | No arbitrary V timeout; preserve C | Used | Due from existing A only; intentional no-idle-lock scope |
| E42 | Availability evaluation: ok / passcodeNotSet / other error | E36/E37 only on effective-state change; otherwise Preserve | ok => true; -5 => false; other => previous ?? true; transient errors preserve L | Preserve unless E37 newly engages | Transient failure never unlocks |
| E43 | Fresh successful explicit LA after retained B / missed return hint | Actual foreground and TTL rules still required; Due then clear | Clear B only after fresh accepted success; unsuccessful/stale result keeps B | Used | Success unlocks; failure stays Locked |
| E44 | Off policy with resign/check, or screen saver without lock | No new A while ineffective; screen saver creates no event | Preserve verification rules for a pending Change; no screen-saver timer | Preserve | No idle lock; successful Change follows normal commit |

## Native source wiring table

These are consumer tests, not duplicate no-op Machine matrices. Every source
must invoke its specified input(s), and must never substitute a system leave
merely to cancel verification.

| ID | Native source | Required input and side effect | A / P / lock rule |
| --- | --- | --- | --- |
| E20 | New Quit/busy/W1 unsaved-discard sheet, locked or unlocked; duplicate busy | E19 only if an evaluation/return needs cancellation, then nonblocking native sheet with exact exemption; duplicate preserves sheet | Preserve A; no new visit; no implicit lock/unlock |
| E21 | Quit/busy/W1 unsaved-discard Cancel or dismissal | Resolve lifecycle callback; any existing W keeps classifying C independently | No new A/P transition; advance past thresholds to prove no new absence |
| E22 | Quit Keep/Stop, unasked quit, termination | Cancel verification through E19 if needed; never demand unlock; discard memory-only state on exit | No unlock; an independently existing absence survives until exit |
| E28 | Dock/tray/menu/deep link/cold launch pending link | Passive E27; if locked queue intent (foreground-only Action fills empty slot; Content/Navigation/explicit commands replace), request foreground then E07 when eligible; a request is not Arrive | Preserve A; actual OS activation determines new visit |
| E29 | Mini/global shortcut press/release | Passive E27; locked press queues one explicit command, release alone does not deliver; paired replay after unlock | Preserve A/P until real begin/return; Mini hidden |
| E30 | Ask Vibey (#4035 integrated) | Passive E27 and Content defer before file read/compose enqueue; memory only until unlock; Dock/tray Open cannot displace it | Preserve A/P; content delivery cannot unlock |
| E31 | Window/content/title/order/visibility/resize/key-window; Mini/Show/Vault | Passive E27 plus cover synchronization; arbitrary focus cannot run E39 | Preserve A/P; never new owner initialization |
| E32 | Deferred action/document-load completion | Passive E27; deliver only when unlocked, otherwise retain | Preserve A/P; page load is not foreground |
| E33 | Runtime stop/restart/rebind, instance switch, account sign-out/auth-page replacement | Passive E27; preserve owner/device policy; menu entry obeys E28 | Preserve A/P; account sign-out is not OS session loss |
| E38 | Ready / main recreation / close/minimize | Initial L from E01; actual active Ready can follow trusted activation; recreation is E31; hide/minimize alone is not synthetic resign | Never unlock merely because a window appears |
| N01 | App resign / NSWorkspace other-app activation | E02 or ambiguous E04; definite other regular foreground app during V/W => E40; LA UI excursion stays E04 | Earliest departure preserved; native classification tested |
| N02 | NSApp didBecomeActive / all system notification pairs | Native public snapshot into E39, then actual-return classification; departures E23, return hints E25 | Never clear B from arbitrary window focus; no paired-bit dead end |
| N03 | LA callback / manual unlock button / settings command | Timestamp before dispatch; E10-E19 and E43; manual tap not blocked permanently by B | No raw-success unlock or stale policy write |
| N04 | Availability query / status read | Shared E42 rule with process-local previous value; status read has no departure effect | Off+explanation only on -5; unknown first read fails closed |

## Validation and real-device scope

Core matrix: every core row's precondition branches across three enabled
policies; Preserve rows seeded with and without A, and verification rows with
and without C. Assert A/C/V/W/Q/B/P, policy, lock, and persistence authorization.
Separate Off and previous-availability combinations exercise E36/E42/E44.
Native aliases consume real adapters/controls and assert their specified core
inputs/effects, not a second copy of the transition logic.

Required native regression cases:

- Unlocked Quit/busy, Cancel/dismiss, advance past each threshold while still
  foreground: no new lock. With prior true departure: original deadline holds.
- Cancel an in-flight Change for lifecycle while C exists; a timely actual
  return clears the excursion; absent return confirms C at its original time.
- Locked Quit stays usable; main dispatch work runs while the sheet is visible;
  duplicate busy does not replace its exemption.
- Reply/foreground in both orders, two-second boundary, late dispatch, one-hour
  return, no-C expiry, sleep/screen lock and explicit other-app departure.
  Separate an expired Success from a new automatically prompted verification.
- Fresh explicit unlock can recover stale B; transient capability errors leave
  a locked window covered; passcode reappearance re-engages Locked.
- Existing entry, cover, title, Mini, tray/menu, Vault arrival and release gates.

Real-device local-package smoke list:

- **First acceptance gate:** measure real Touch ID success-to-didBecomeActive
  latency and verify stable return within two seconds. If not, main must
  reconsider the return policy from device evidence before acceptance.
- Global shortcut/deep link from background: verify return within two seconds
  after real LA, or a usable explicit retry after expiry.
- Quit with main minimized or app in background: sheet visible/usable and
  dismissal releases the pending lifecycle request.
- Real LA focus excursion versus Cmd-Tab to a different app during LA;
  workspace activation-policy classification must not mistake LA for departure.
- Lock/unlock, fast user switching, lid close/external display and wake: public
  snapshot coordination recovers missed hints; explicit retry remains usable.
- Plan section 5 Touch ID Mac and no-usable-Touch-ID groups: password, Watch,
  Mission Control, all window types, ad-hoc and signed TEST RC packages.

## Review record

Revision 1 (`53cd2d432a`): NOT PASS, receipt `42c0d51088ef`, T1-T4.
Main resolved T1-T4 in the 12:10 decision and T3 public-signals clarification;
T4 is mirrored from shared contract commit `50c57df6`.
Revision 2 (`3bccb5f035`): PASS, receipt `86028c1feb52`. S1 (cancelled
settings replies on definite departures) and S2 (reply sampling does not
reconcile B) are clarified above. S3 (background-entry focus return) is the
first local-package acceptance gate, not evidence supplied by test substitutes.
