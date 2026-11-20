# Desktop App Lock: event and state contract

Status: draft for independent table review; implementation is paused.
Baseline: `68ebe612f96294ced6163442e54953c5f60ffb32`.
Decision owner: main, 2026-11-20. Reviewer: `sesmsfnku7fjm`.

Three reviewed heads (`e899f374d2`, `295e3fe69e`, `68ebe612f9`) contained
findings in the departure/authentication family. This table replaces local
patching with an exhaustive input contract. No implementation change is
permitted until the independent reviewer approves the table. It retains the
approved two-second LA return rule, asynchronous lifecycle sheets, single
pending intent, and existing product policies.

## A concrete case

An unlocked user with One Minute chooses Quit at time 10, then Cancel at time
12, and continues working until time 100. Neither showing nor dismissing the
sheet is a departure. `away_since` remains absent and the app remains unlocked.

If that user actually switches to another app at time 20, the resign event
records 20. A busy response, window redraw, deferred link, or authentication
cancellation at time 30 must not move or erase 20. At time 80, One Minute locks.
Only an actual foreground return can end that absence; an expired deadline
latches the lock before the absence is cleared.

## State, time, and ownership

- `A`: `away_since`, the first confirmed departure in the current absence.
- `C`: candidate resign during LA verification. It is an observed resign whose
  cause may be the system verification panel. It is not yet a confirmed `A`.
- `V`: verification purpose and epoch (Unlock or Change(policy)).
- `R`: LA outcome plus the original callback timestamp; lifetime is strictly
  less than two seconds. The callback timestamps before dispatch to main.
- `Q`: resolved completion awaiting native consumption/persistence. It is never
  an indefinite successful return token. `begin` and automatic prompts remain
  blocked until Q is consumed or invalidated.
- `B`: sleep/display-sleep/screen-lock/session-inactive reason bits.
- `L`: the latched locked state. Returning or cancelling does not unlock L.
  Valid successful verification can unlock; unavailable authentication or Off
  makes protection ineffective as required by the shared contract.

The pure Machine owns every time/classification decision. Native code supplies
facts, presents controls, persists an authorized policy, and consumes effects.
A presentation callback must not directly set a departure timestamp.

`First(t)` sets A to t only if absent; it preserves an earlier A. Repeated resign
or system notifications never extend a deadline. `Preserve` neither creates nor
clears A. `Clear on return` first evaluates the existing deadline and then clears
A only when real foreground is observed and all B bits are clear.

`Due(A,t)` means Immediate locks when A exists, One Minute locks at `t-A >= 60`,
and Five Minutes at `t-A >= 300`. All rows marked Due use precisely this rule;
if A is absent, Due does nothing. Keep means L is unchanged (already locked
stays locked). All transitions use continuous monotonic time, including sleep.

Only actual application resign, system lock/sleep/session loss, or confirmation
of an observed candidate resign after the bounded LA return can create A.
Internal sheets, settings requests, LA reply/cancellation, entry requests,
window events, and Runtime/account operations cannot invent a departure.
No candidate is synthesized from a reply timestamp: that timestamp bounds R,
not the start of an absence. A reply with no observed resign still expires,
but cannot manufacture an A.

An actual foreground observation is distinct from the verification outcome.
`DidBecomeActive`, or the real `NSApp.isActive` sample accompanying a reply or
system return, can establish return. A panel being created/closed, a focus
request, `orderFront`, or a successful LA outcome alone cannot establish it.
Expiry is checked before any foreground observation can consume R.

## Event × state table

Each ID below is a required table-driven Machine test row, run for Immediate,
One Minute and Five Minutes. Branches within a row are separate cases. Native
sources that only read/check state map to a passive deadline check; they do not
need new production event variants merely to obtain a test name.

| ID | Input and precondition | A operation | Verification / C / epoch operation | Immediate / One Minute / Five Minutes lock result |
| --- | --- | --- | --- | --- |
| E01 | Process initialization, password available, saved enabled policy | Absent | V/R/Q/C empty; B empty | Locked / Locked / Locked, independent of elapsed absence |
| E02 | Actual app resign; no verification | First(event time) | No verification change | Due / Due / Due |
| E03 | Repeated actual resign in same absence | Preserve earliest A | No verification change | Due / Due / Due; deadline never restarts |
| E04 | App resign during V, including LA panel focus loss | Preserve A | First observed resign becomes C; preserve earlier C; retain V/R | Due from existing A only; C alone does not lock yet |
| E05 | Actual foreground return, B empty, no V/R | Clear on return | Preserve settled outcome; do not mint verification | Due before clear; otherwise Keep / Keep / Keep |
| E06 | Actual foreground return while V waits for an LA reply | Clear on return | Clear C; retain V; no successful outcome yet | Due before clear; otherwise Keep / Keep / Keep |
| E07 | Begin Unlock while L=true, available, unblocked, no V/R/Q | Preserve A | New epoch/V; clear stale C/R; mark automatic attempt used | Keep / Keep / Keep; verification is not unlocking |
| E08 | Begin Change from an unlocked settings caller | Preserve A | New epoch/V; clear stale C/R | Keep / Keep / Keep; saved policy unchanged |
| E09 | Begin rejected: blocked, unavailable, another V or Q, or Unlock while unlocked | Preserve A | No mutation of current verification or candidate | Keep / Keep / Keep; reply busy/unavailable as applicable |
| E10 | Current-epoch LA Success before TTL, actual foreground observed | Clear on return, independently of outcome | Resolve V to successful Q; clear C/R; native may persist Change before commit | Successful Unlock/Change commit unlocks; persistence failure follows E17 |
| E11 | Current-epoch LA Cancelled/Failed/Unavailable, actual foreground observed | Clear on return, independently of outcome | Resolve V to matching Q; clear C/R; no policy write | Due before return, then Keep; failure/cancellation does not unlock |
| E12 | Current-epoch LA reply while not foreground, with C present | Preserve A | Store outcome/timestamp R and retain V/C for at most two seconds | Due from existing A only; locked content remains covered |
| E13 | Current-epoch LA reply while not foreground, no C observed | Preserve A | Store R and retain V; leave C absent | Due from existing A only; reply does not manufacture departure |
| E14 | Actual foreground return with R age < 2 seconds | Clear on return | Resolve R to Q; clear C/R/V | Success may unlock after commit; all other outcomes Keep |
| E15 | R reaches age >= 2 seconds, C present; includes delayed callback dispatch and late Arrive | First(C), preserving earlier A; Due before any real-return clear | Discard original outcome; Unlock resolves Cancelled, Change resolves Failed; clear C/R/V; never persist stale Change | Due / Due / Due; later return cannot consume old Success |
| E16 | R reaches age >= 2 seconds, no C present | Preserve A; Due before any real-return clear | Same invalidation/result as E15; no synthetic C or A | Due from existing A only; L never becomes unlocked from old Success |
| E17 | Native consumes Q; policy persistence succeeds or fails | Preserve A; a successful foreground verification has already ended the absence | Only successful Change authorizes write; failed write keeps old policy and returns Failed; consume Q; no automatic retry | Valid Success unlocks; other results Keep; no new departure |
| E18 | Old epoch, duplicate reply, reply after lifecycle/system invalidation | Preserve A | Ignore; cannot replace V/R/Q or revive C | Keep / Keep / Keep |
| E19 | `cancel_for_lifecycle`: supersede verification to show Quit/busy | Preserve A exactly | Increment epoch; clear V/R/Q/C; invalidate LAContext; cancel settings sender; suppress automatic re-prompt | Keep / Keep / Keep; no leave, arrive, or deadline mutation |
| E20 | Quit/busy sheet created; already unlocked or already locked | Preserve A | E19 once for a new sheet; duplicate request keeps current sheet and state | Keep / Keep / Keep; sheet is not a departure |
| E21 | Quit/busy sheet cancelled or dismissed | Preserve A | No authentication transition; resolve lifecycle callback only | Keep / Keep / Keep; later ticks can use only an independently recorded A |
| E22 | Quit Keep/Stop confirmed, no question required, or process exit | Preserve A until process exit | Invalidate outstanding verification if continuing lifecycle; on exit discard in-memory pending entry and owner | No unlock required; process termination is not an unlock |
| E23 | OS screen lock, will sleep, display sleep, or user-session inactive | First(system-event time), preserving earlier confirmed A | Set its B bit; invalidate epoch/V/R/Q/C and LAContext; cancel settings sender | Due / Due / Due; a definite new event uses its own timestamp, not an unconfirmed panel C |
| E24 | Repeated/overlapping system departure notifications | Preserve earliest A | Accumulate B bits; repeated cancellation harmless | Due / Due / Due |
| E25 | Wake/screens wake/screen unlock/session return, but another B bit remains or app is not foreground | Preserve A | Clear only matching B bit; do not revive V/R/Q/C | Due / Due / Due; wake is not necessarily app return |
| E26 | Last system reason clears and real app foreground is observed | Clear on return | No stale verification revived; ordinary automatic-prompt eligibility applies | Due before clear; locked stays locked until a fresh verification |
| E27 | Periodic tick; synchronous `is_locked`/entry check | Preserve A, except E15 promotes an existing C | Resolve expired R via E15/E16; keep Q until consumed on main | Due / Due / Due; checks cannot restart the clock |
| E28 | Dock/tray/menu/deep-link request, or queued launch link; locked | Preserve A | Replace single pending intent; may subsequently request foreground and attempt E07; neither request itself is Arrive | Due check then Keep; execute pending action only after successful unlock |
| E29 | Mini/global shortcut press/release; locked | Preserve A | Press queues one action and may request E07; release alone neither verifies nor delivers; paired replay happens after unlock | Due check then Keep; Mini stays hidden |
| E30 | Ask Vibey content entry; locked (integration after #4035) | Preserve A | Replace pending Content before file read or compose enqueue; may request E07 | Due check then Keep; no content written or delivered before unlock |
| E31 | Window creation/order/content/title/visibility/resize/key-window event; Mini/Show/Vault changes | Preserve A | No verification change; synchronize native cover and desired visibility | Due check then Keep; key-window change within Avibe is not app departure |
| E32 | Deferred action delivery or document-load completion | Preserve A | Execute only if L=false; otherwise retain intent; no synthetic Arrive | Due check then Keep; returning to a page is not app activation |
| E33 | Runtime stop/restart/rebind, instance switch, account sign-out, remote/local route or auth-page replacement | Preserve A and saved device policy | No App Lock reset; menu entry obeys E28; native content remains covered if locked | Due check then Keep; runtime/account lifecycle is not OS session departure |
| E34 | Settings/status read; same-policy request; unknown policy; unauthorized caller | Preserve A | Status may refresh availability under E36/E37; same policy is a no-op without verification; unknown/unauthorized rejects transport | Due check then Keep; no departure or unlock from a command |
| E35 | Settings change cancelled, failed, busy, unavailable, or persistence failure | Preserve A except an independently observed actual return | Existing E09/E11-E19 govern verification; old persisted policy stays | Keep, subject to pre-existing Due; UI failure cannot start or reset an absence |
| E36 | Availability becomes false, or successfully committed policy becomes Off | Clear A because protection is ineffective | Preserve saved policy on unavailable; do not evaluate a new LA request; refresh is outside active verification | Unlocked/ineffective for all stored policies; explicit shared-contract exception |
| E37 | Availability becomes true while a saved enabled policy was ineffective | A remains absent | No reusable verification; new lock can prompt on actual activation | Locked / Locked / Locked; no invented departure timestamp |
| E38 | Application Ready while active/inactive; main recreation after close/minimize | Active Ready follows E05; inactive Ready preserves A | Cold L from E01 persists; recreation is E31, not new owner initialization | Never unlock merely because a window appears; active Ready may begin fresh E07 |

## Clarifications that prevent another timing patch

1. Lifecycle cancellation is verification invalidation, not a synthetic system
   departure. It must not call `leave`, `system_leave`, `block`, or `arrive`.
   Existing A survives even when a stale C is discarded.
2. C can come only from an observed resign during V. A reply timestamp is not C.
   Expiring an outcome with no candidate still rejects stale Success; it does
   not by itself prove the user left the app. This explicitly removes the
   current reply-time fallback from the proposed implementation.
3. A definite system departure invalidates LA and records the system event's
   own time, preserving an earlier confirmed A. It does not retroactively turn
   an earlier ambiguous panel resign into a confirmed departure. Only the
   bounded-return expiry rule promotes C. This explicitly distinguishes E23
   from E15 and removes the current `candidate_leave.take().unwrap_or(now)`
   shortcut in `system_leave`.
4. Foreground return must evaluate a real existing deadline before clearing A.
   Cancellation does not clear it; observed actual foreground can. This holds
   whether foreground precedes or follows the LA reply.
5. No internal UI or account/Runtime event may route through a system-departure
   helper just to cancel verification. Conversely, cancellation cannot reset
   a previously confirmed absence. Passive checks evaluate A without changing
   its timestamp.
6. Off and unavailable authentication are explicit exceptions to protection,
   not fake foreground returns. Settings verification still has a TTL while
   the stored policy is Off.
7. The raw LA outcome grants no persistence authority. Only a current,
   unexpired completion resolved with real foreground may authorize Change.
   Expiry/system invalidation/lifecycle cancellation must defeat a late reply
   even if it says Success.

## Verification required after table approval

Use stable E01-E38 IDs in a table-driven pure Machine suite. Every row runs
across the three enabled policies, including each precondition branch above.
For Preserve/no-op rows, seed both no A and an existing A: advance beyond the
policy threshold to prove respectively no new lock and no postponed lock.
For rows involving verification, seed both Unlock and Change where applicable,
with a candidate and without one. Assert the resulting policy, A, C, V/R/Q,
lock latch, persistence authorization, and stale-epoch rejection, not only a
boolean snapshot. No-op native source aliases consume the same Machine check;
source wiring is verified separately rather than inventing duplicate owners.

Required native consumer tests:

- Unlocked Quit and busy sheet, Cancel/dismiss, then advance past each policy
  threshold while still foreground: no lock. Repeat with actual departure
  before the sheet: its original deadline survives.
- Already locked Quit/busy remains usable without unlocking; a queued main
  dispatch block runs while the sheet is visible. Duplicate busy requests do
  not replace the exact exempt window.
- LA reply before/after foreground; 2-second boundary; one-hour late return;
  real resign versus no-resign expiry; Change never persists stale success;
  screen-lock/sleep and lifecycle cancellation invalidate stored replies.
- Existing pending-entry, native-window coverage, title privacy, Mini,
  status/menu, Vault arrival, shared-contract and release-feature gates remain.

## Real-device smoke checks (not substituted by the peer)

- Global shortcut/deep-link launch while Avibe is in the background: after LA
  success, confirm macOS returns Avibe to foreground within two seconds, or
  that expired verification leaves a clear, usable explicit unlock action.
- Quit from Dock/menu with the main window minimized or app in the background:
  confirm the main-window sheet is visible and usable, and dismissal releases
  the lifecycle request. This is an explicit acceptance item, not an assertion
  that `orderFront` restores minimized windows or activates the app.
- Run the product plan section 5 Touch ID Mac and no-usable-Touch-ID groups,
  including real login password, Watch, lid/sleep, Mission Control, all window
  types and both ad-hoc and signed TEST RC packages.

## Review record

Awaiting independent review of this table. Implementation remains paused.
