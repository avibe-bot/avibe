# Route B: Bounded Foreground Delivery

Status: design proposal only. Owner and source review are required before
implementation.

Route A remains the shipped contract in
[Desktop computer use via embedded Cua Driver](desktop-computer-use.md#route-a-raw-single-click-selector-2026-10-10).
Route B is a separate, explicit foreground delivery mode for applications that
reject both accessibility actions and background synthetic input. It must never
become a silent fallback from Route A.

## Evidence and conclusion

The macOS background-input research note
(`docs/research/desktop-computer-use-background-input/findings.md`) establishes
two separate facts:

- A raw, exact-window `synthetic_events` click can reach a test-owned AppKit
  surface through `mouseDown` without changing the frontmost application or
  hardware pointer.
- An accessibility action can return success while a custom surface receives no
  real mouse event, and the pinned driver has no general-purpose foreground
  selector that makes this safe by default.

The Slice 2 final report
(`/tmp/avibe-cua-slice2-20261011-1540/evidence/final/slice2-final-report.md`)
shows the remaining gap in a real application. The fresh same-connection
observation identified QQ window `6285` under PID
`97470`; the single click used `x=41`, `y=146`, `count=1`, `button=left`,
`click_mode:"raw"`, and `delivery_mode:"background"`. The driver response in
`qq-direct-corrected-click-response-20261012.json` reported
`route:"synthetic_events"` and accepted the dispatch, while the before/after
observations and
`qq-direct-corrected-before-20261012.png` /
`qq-direct-corrected-after-20261012.png` show no QQ view change. The correct
conclusion is that the background event reached the driver boundary but had no
observable effect in QQ. It does not justify changing Route A's meaning.

The design recommendation is therefore a narrow foreground exception:
explicit per-call opt-in, guarded by a session-scoped owner toggle, exact
window admission, bounded activation, exact restoration, and visible local
indication.

## Policy decisions

### When foreground delivery is allowed

Foreground delivery is allowed only for one explicitly requested input call.
The call must:

1. use `delivery_mode:"foreground"`;
2. identify one exact `pid` and `window_id` observed in the same Avibe Session;
3. use coordinates or an action whose target can be resolved to that window;
4. hold the ordinary desktop lease and pass every existing observe-first,
   same-user, active-Turn, and daemon-generation check; and
5. run on a platform and tool operation that has passed its foreground
   verification gate.

The mode expires when the call finishes. A session may have consent to make
another explicit foreground call, but consent must not turn all subsequent
input into foreground input. Desktop targets, ambiguous windows, stale
observations, and activation-only operations remain refused.

Per-app foreground mode is an explicit alternative, but is too broad: an app
can contain multiple windows with different user impact, and an allowlist can
outlive the task that justified it. Per-session foreground mode is simpler but
would make an accidental or stale agent request affect every later input in the
session. Per-call delivery is the smallest boundary that matches the actual
risk.

### Owner consent

The recommended consent model is a local, owner-controlled session toggle:
"Allow foreground control for this Agent Session." The toggle is available
only from the native desktop surface or tray, never from the agent CLI, an IM
message, or an agent-provided argument. It is ephemeral: session end, session
archive, native Computer Use stop, or the owner disabling the toggle revokes
it. The implementation should keep this consent in runtime state owned by the
existing session/lease path rather than create a durable allowlist file.

Every-call consent is the strongest prompt model, but it is incompatible with
unattended IM and scheduled work: the required human may not be at the
machine, and a prompt cannot safely become an agent retry loop. A per-app
allowlist avoids repeated prompts but creates persistent authority that is
harder to audit and revoke. The session toggle keeps the owner decision
explicit while preserving a short lifetime and exact per-call target checks.

### Recording and restoration

Before foreground admission, the driver must capture a complete restoration
snapshot in the same serialized desktop lease:

- **Frontmost application:** bundle or executable identity, PID, and a
  process-start identity that prevents PID reuse from being mistaken for the
  same process.
- **Frontmost window:** owning process identity, native window ID, title or
  role where available, frame, display, and workspace/Space identity where the
  platform exposes it.
- **Pointer:** global screen coordinates, display identity and scale, plus
  enough button-state information to prove that the pointer is not already in
  an ambiguous drag.
- **Target:** the freshly observed target PID/window ID, frame, and the
  coordinate transform used to calculate the requested screen point.

If any required snapshot is unavailable, stale, or internally inconsistent, the
call is refused before activation. The driver then activates only the exact
target launch instance, moves the hardware pointer to the requested point,
posts the requested HID action, and waits for the bounded action completion
window.

Restoration is an ordered, verified operation: restore the recorded frontmost
application, restore its recorded window, restore the pointer to its recorded
global position, then read the state again. App and window restoration must
match the recorded process identity and native window ID; bringing a different
window from the same app to the front is not success. Pointer restoration must
match the recorded display and coordinates within the platform's readback
precision.

If the prior application or window has exited, moved to an unaddressable
workspace, or cannot be verified, the driver must not guess a replacement.
It should still make the bounded pointer-restoration attempt when that can be
done independently, return a structured `foreground_restoration_failed`
result, and block further foreground input for the session until the owner
re-arms the toggle. It must not retry the action, silently continue with
another target, or claim that the input was undone. The local indication must
remain in an attention state so the owner knows that the desktop was not
restored exactly.

## User-visible indication

Foreground mode must be visible before activation and remain visible until
restoration has been verified. The existing agent-cursor overlay remains
enabled. In addition, the native tray/app-menu status should identify
"Foreground control active" and name the target application/window. A
non-interactive native banner or equivalent shell-owned indicator should expose
the same state with an immediate Stop action. The Workbench status line may
mirror the state, but it is not sufficient on its own because the Workbench may
be hidden while an IM-triggered turn is running.

Stopping from the native surface revokes the session consent and prevents the
next input call. It cannot claim to undo an event already posted. A failed
restoration keeps the indicator visible as an error until the owner resolves
the state.

## Admission and API shape

Today the Computer Use server refuses `delivery_mode:"foreground"`. Route B
would add one allowlisted admission branch while preserving the current
default:

- omitted `delivery_mode` remains background;
- Route A `click_mode:"raw"` remains background-only and keeps its existing
  exact-window contract;
- `delivery_mode:"foreground"` is accepted only with explicit session consent,
  a fresh exact target, a supported operation/platform, and the existing
  lease and caller checks;
- unknown modes, desktop-wide targets, stale captures, locked sessions,
  missing consent, and unsupported operations remain structured refusals; and
- a background result that is unchanged or unverifiable never escalates by
  itself. The agent must observe again and issue a separate explicit
  foreground request.

The CLI should reuse the existing leaf command and argument surface rather than
add a top-level foreground command or a hidden retry flag:

```text
vibe computer click --arg pid=97470 --arg window_id=6285 \
  --arg x=41 --arg y=146 --arg delivery_mode=foreground
```

The built-in `computer-use` Skill should teach the same sequence: background
first, observe the exact target in the same session, do not retry blindly,
request foreground only when the owner session toggle is active, and verify
both the target effect and restoration afterward. It must state that agents
cannot grant foreground consent and that foreground is never an implicit
fallback. Structured results should distinguish dispatch mode, target effect
(`verified` or `unverifiable`), and restoration (`restored` or a concrete
failure); a transport acknowledgment alone is not success.

## Windows and Q6

Route B cannot claim Windows parity yet. The existing Q6 record says that the
Windows assets are signed and available, but embedded daemon spawn, proxy
reachability, and the need for the UIA worker remain unverified. It also records
that Windows runs in the interactive logon session, refuses Session 0, may
require a high-integrity daemon for elevated targets, needs foreground delivery
for Chromium page content, terminals, and GTK or custom-drawn surfaces, and
does not provide the macOS-style window-change wait.

Q6 therefore changes the admission matrix, not just packaging. A Windows
implementation needs its own exact process/window identity and restoration
contract using the native foreground window and cursor APIs. It must define
how effect and restoration are verified without the window-change wait, and
must refuse targets whose integrity level or UIA route is unsupported. Until
Q6 is completed on a Windows machine, foreground delivery stays refused on
Windows even if the CLI accepts the documented field on other platforms.

## Explicitly out of scope

- Silent escalation from Route A background delivery.
- A persistent per-app allowlist, global foreground permission, or agent-granted
  consent.
- Desktop-wide HID injection, an activation-only tool, login-screen or
  lock-screen control, and replacement of the native Computer Use stop.
- General foreground support for typing, hotkeys, scrolling, or dragging before
  each operation has its own restoration and effect contract; the first
  implementation target should be the evidenced click path.
- QQ or WeChat product acceptance, app-specific adapters, OCR, perception, or
  model changes.
- Windows implementation before the Q6 machine run.
- TCC changes, credentials, Runtime restart, packaging changes, new product
  flags, or new persistent state files in this design-only lane.

This document defines the bounded contract for review. It does not authorize
implementation. Owner and source review must approve the policy, restoration
failure behavior, and platform-specific gates before any product change begins.
