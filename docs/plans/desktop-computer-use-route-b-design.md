# Route B: Bounded Foreground Delivery

Status: design proposal only. Owner and source review are required before
implementation.

Route A remains the shipped contract in
[Desktop computer use via embedded Cua Driver](desktop-computer-use.md#route-a-raw-single-click-selector-2026-10-10).
Route B is a separate, explicit foreground mode for applications that reject
both accessibility actions and background synthetic input. It must never become
a silent fallback from Route A.

## Evidence and conclusion

The load-bearing observations are recorded here so a fresh checkout can review
the decision without the unmerged research worktree or temporary acceptance
directory. Their provenance is the macOS background-input research note
(`docs/research/desktop-computer-use-background-input/findings.md`) and the
Slice 2 final report and QQ artifacts under
`/tmp/avibe-cua-slice2-20261011-1540/evidence/final/`; those paths are
provenance, not dependencies of this contract.

The research probe established that:

- A raw, exact-window `synthetic_events` click can reach a test-owned AppKit
  surface through `mouseDown` without changing the frontmost application or
  hardware pointer.
- An accessibility action can return success while a custom surface receives no
  real mouse event, and the pinned driver has no general-purpose foreground
  selector that makes this safe by default.

Slice 2 showed the gap in a real application: QQ window `6285` under PID
`97470` received `x=41`, `y=146`, `count=1`, `button=left`,
`click_mode:"raw"`, and `delivery_mode:"background"`. The driver reported
`route:"synthetic_events"` and accepted dispatch, but the before/after
observations and screenshots showed no QQ view change. The event reached the
driver boundary without an observable QQ effect; this does not change Route A.

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

Per-app mode is too broad because an app can contain multiple windows and an
allowlist can outlive the task that justified it. Per-session mode is simpler
but lets an accidental or stale request affect every later input. Per-call
delivery is the smallest boundary that matches the risk.

### Owner consent

The recommended consent model is a local, owner-controlled session toggle:
"Allow foreground control for this Agent Session." It is available only from
the native desktop surface or tray, never from the agent CLI, an IM message, or
an agent argument. Session end/archive, native Computer Use stop, or disabling
the toggle revokes it. Consent belongs in the existing ephemeral session/lease
path, not a durable allowlist file.

Every-call consent is the strongest prompt model, but it is incompatible with
unattended IM and scheduled work: the required human may not be at the
machine, and a prompt cannot safely become an agent retry loop. A per-app
allowlist avoids repeated prompts but creates persistent authority that is
harder to audit and revoke. The session toggle keeps the owner decision
explicit while preserving a short lifetime and exact per-call target checks.

The native-to-Controller handoff must be explicit. The Controller issues an
opaque, short-lived challenge for the selected active session. After the owner
toggles foreground control, the native shell sends the session identity, shell
instance/generation, challenge, and owner action over authenticated local IPC.
The Controller stores the grant in its ephemeral session/lease registry and
acknowledges it. The agent cannot mint, extend, or reuse the challenge. Missing
acknowledgement, stale shell generation, IPC loss, native stop, session
end/archive, or shell-lock loss revokes the grant and fails closed. The exact
IPC message and shell-origin proof require owner and source review; this must
not become agent-writable state.

### Recording and restoration

Before admission, the driver must capture a complete restoration snapshot in
the same serialized desktop lease:

- **Frontmost application:** bundle/executable identity, PID, and process-start
  identity so PID reuse cannot match.
- **Frontmost window:** owning process identity, native window ID, title or
  role where available, frame, display, and workspace/Space identity where the
  platform exposes it.
- **Pointer:** global coordinates, display/scale, and button state sufficient
  to reject an existing ambiguous drag.
- **Target:** freshly observed PID/window ID, frame, coordinate transform, and
  the observation `capture_id` used for the coordinates.

If any required snapshot is unavailable, stale, or internally inconsistent, the
call is refused before activation. A foreground pixel click must include the
same `capture_id`; stale, mismatched, or out-of-bounds captures are rejected
before activation. The driver activates only the exact target launch instance,
reads back the exact native target window, recomputes its transform, and
refuses the call unless the admitted `pid`/`window_id` is frontmost immediately
before the HID event.

The first implementation should allow only the evidenced click shape: the
`click` tool, an exact pixel target, `action:"click"`, left button, count one,
no modifiers, no element token, no portable or desktop target, and no
`click_mode:"raw"`. Right or middle buttons, multiple clicks, press-only
actions, modifiers, element actions, and every other tool operation require a
separate contract and validation.

Restoration is ordered and verified: restore the recorded app, its recorded
window, and the recorded pointer position, then read state again. App/window
restoration must match process identity and native window ID; another window in
the same app is not success. Pointer restoration must match display and
coordinates within readback precision. If Route B emitted button-down, cleanup
must emit a bounded matching release when needed and verify that no button
remains pressed before returning `restored`.

The sequence carries a user-activity fence. The snapshot records an OS-level
activation/pointer input generation; the driver checks it before activation,
before posting HID, and before each restoration step. If the owner changes the
frontmost app or pointer, the driver cancels before dispatch when possible.
After dispatch it stops restoration rather than overwriting newer human state,
reports interruption by owner activity, and leaves the attention indicator
visible.

If the prior app/window exited, moved to an unaddressable workspace, or cannot
be verified, the driver must not guess. It should make an independent bounded
pointer-restoration attempt when possible, return
`foreground_restoration_failed`, and quarantine foreground delivery for the
whole desktop until the owner resolves the attention state. This covers every
session and foreground call; Route A remains governed by its existing rules.
The driver must not retry, choose another target, or claim the input was undone.
The same desktop quarantine applies to a post-forward transport failure with an
unknown outcome or restoration state.

## User-visible indication

Foreground mode must be visible before activation and remain visible until
restoration is verified. Keep the existing agent-cursor overlay enabled. The
native tray/app-menu status must say "Foreground control active" and name the
target app/window. A shell-owned banner or equivalent must expose the same
state with an immediate Stop action. The Workbench may mirror it, but cannot be
the only indication because IM-triggered work can run while it is hidden.

Stopping from the native surface revokes the session consent and marks the
in-flight operation cancelled. The operation checks that cancellation before
activation and again immediately before posting the HID event; if it has not
posted, it aborts and restores any state it changed. After dispatch, Stop
cannot claim to undo the event, but the operation still completes its bounded
restoration or enters the attention/quarantine state. A failed restoration
keeps the indicator visible as an error until the owner resolves the state.

## Admission and API shape

Today the Computer Use server refuses `delivery_mode:"foreground"`. Route B
would add one allowlisted admission branch while preserving the current
default:

- omitted `delivery_mode` remains background;
- Route A `click_mode:"raw"` remains background-only and keeps its existing
  exact-window contract;
- `delivery_mode:"foreground"` is accepted only for the first-release click
  allowlist, explicit session consent, a fresh exact target, its matching
  `capture_id`, a supported platform, and existing lease/caller checks;
- unknown modes, desktop-wide targets, stale captures, locked sessions,
  missing consent, and unsupported operations remain structured refusals; and
- an unchanged or unverifiable background result never escalates by itself. A
  separate foreground request requires action-specific negative evidence that
  the background action did not occur; an ambiguous postcondition is reported
  as unknown rather than repeated.

The CLI should reuse the existing leaf command and argument surface rather than
add a top-level foreground command or a hidden retry flag:

```text
vibe computer click --arg pid=97470 --arg window_id=6285 \
  --arg x=41 --arg y=146 --arg capture_id=capture_... \
  --arg action=click --arg button=left --arg count=1 \
  --arg modifier=[] --arg delivery_mode=foreground
```

The built-in `computer-use` Skill should teach: background first; observe the
exact target in the same session; carry `capture_id`; do not retry blindly;
request foreground only when the owner toggle is active; and verify effect and
restoration afterward. It must say that agents cannot grant consent and
foreground is never an implicit fallback. Results should distinguish dispatch
mode, target effect (`verified` or `unverifiable`), and restoration (`restored`
or a concrete failure); transport acknowledgement alone is not success.

## Windows and Q6

Route B cannot claim Windows parity yet. Q6 still leaves embedded daemon spawn,
proxy reachability, UIA worker behavior, and the interactive-session boundary
unverified. It also records elevated-integrity targets, foreground-only
Chromium/terminal/GTK/custom surfaces, and no macOS-style window-change wait.
Q6 therefore changes admission, not just packaging. Windows needs its own exact
process/window identity, cursor restoration, and effect/restoration verification
using native APIs, with refusals for unsupported integrity or UIA routes.
Until the Q6 machine run is complete, foreground remains refused on Windows.

## Explicitly out of scope

- Silent escalation from Route A background delivery.
- Persistent per-app allowlists, global foreground permission, or agent-granted
  consent.
- Desktop-wide HID injection, an activation-only tool, login-screen or
  lock-screen control, and replacement of the native Computer Use stop.
- General foreground support for typing, hotkeys, scrolling, or dragging before
  each operation has its own restoration and effect contract; the first
  implementation target should be the evidenced click path.
- Foreground click variants outside the first-release allowlist, including
  right or middle buttons, multiple clicks, modifiers, element actions, and
  press-only actions.
- QQ or WeChat product acceptance, app-specific adapters, OCR, perception, or
  model changes.
- Windows implementation before the Q6 machine run.
- TCC changes, credentials, Runtime restart, packaging changes, product flags,
  or persistent state files in this design-only lane.

This document defines the bounded contract for review. It does not authorize
implementation. Owner and source review must approve the policy, restoration
failure behavior, and platform-specific gates before any product change begins.
