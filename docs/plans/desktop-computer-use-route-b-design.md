# Route B: Bounded Foreground Delivery

Status: superseded. The implementation of this record (#2607) was reverted by
#2674. The owner decision on #3050 replaced per-call consent, leases, and
verified restoration with ordinary foreground input and explicit window
activation; see
[`2026-10-29-computer-use-foreground-and-readiness.md`](2026-10-29-computer-use-foreground-and-readiness.md).
The record below is kept for history.

[Route A](desktop-computer-use.md#route-a-raw-single-click-selector-2026-10-10)
remains the shipped background contract. Route B is a separate, explicit
foreground request for applications that do not produce an observable result
through the available background routes. It must never become a silent fallback
from Route A.

## Evidence

The background-input research found that the pinned driver can deliver an
exact-window `synthetic_events` click to a test-owned AppKit surface without
changing the frontmost application or hardware pointer. It also found that an
Accessibility success response is not proof that a custom surface handled a
real mouse event. The research records the limitation that some applications
require real HID origin or hardware pointer state:

[`desktop-computer-use-background-input/findings.md`](../research/desktop-computer-use-background-input/findings.md)

The research note is committed in this PR as the durable record of the
test-owned probe. Slice 2 provides the application evidence.

### Slice 2 QQ observation

The owner-approved QQ Route A observation used one background raw click with
`x=41`, `y=146`, `count=1`, `button=left`, `modifier=[]`,
`click_mode:"raw"`, and `delivery_mode:"background"`. The driver accepted the
dispatch and reported the `synthetic_events` route. Fresh before/after
observations showed no observable QQ view change.

The raw QQ responses, observations, and screenshots remain in the local Slice 2
evidence directory and are intentionally not committed because they contain
private third-party application content.

The evidence shows a gap between accepted driver dispatch and observable
application effect. It does not change Route A semantics or justify automatic
replay.

## Decision

### Granularity: per call

Foreground is allowed only for one explicit foreground input call. The call
must identify one exact application window observed in the same Avibe Agent
Session and must use the explicit `delivery_mode:"foreground"` value. The
foreground interval ends immediately after that call, followed by restoration
of the prior desktop state.

Consent does not make later calls foreground calls. There is no per-app
allowlist and no session-wide foreground mode. Per-app permission is too broad
for applications with multiple windows and can outlive the task that justified
it. A session-wide mode makes an accidental or stale request affect every later
input. Per-call admission keeps the target and the side effect bounded.

The first implementation decision should remain narrower than general HID
support: it should separately specify the exact click shape, target types,
platforms, and result contract it can verify. Typing, hotkeys, scrolling,
dragging, desktop targets, ambiguous windows, and unsupported operations remain
refused until they have their own contract.

### Consent: native session toggle

The recommended consent model is a per-Avibe-Agent-Session toggle owned by the
native desktop tray/menu:

`Allow foreground control for this conversation`

The owner turns it on from the native shell UI. It expires when the Session
ends or is archived, when native Computer Use stops, or when the owner switches
it off. It is ephemeral session consent, not a durable allowlist.

Consent can never be granted by the CLI, an IM message, or agent arguments.
Those paths may request a foreground call, but they cannot create or extend
the grant. The added rule is exactly that foreground consent is granted only
through the native shell UI.

Every-call confirmation is more interruptive and does not fit work initiated
from an IM or an unattended Session. A per-app allowlist is longer-lived and
broader than the task requires. The per-Session toggle is the selected
compromise: the owner makes one explicit native decision, while each input
still requires a separate foreground request and exact target admission.

Route B inherits the existing documented same-user local trust boundary. This
record does not introduce a second authority model for same-user processes.
Signed shell capabilities, secure-enclave or keychain keys, mutual-authenticated
minting, and a new daemon-level capability protocol are future hardening, not
part of this decision. Any stronger trust guarantee requires a separate owner
decision before implementation.

### Record, restore, and verify

Route B v1 has a finite restoration contract. Before the foreground call, the
implementation records exactly:

1. the prior frontmost application and its frontmost native window, including
   identities that distinguish the current process and native window;
2. the hardware pointer position; and
3. the active Space of the affected display.

The target must be one freshly observed application window in the same Session
and must satisfy the admission preconditions below. The implementation must
retain the target identity and coordinate context needed to confirm that it is
still the requested window before posting the foreground input. Numeric PID or
window identifiers must not be treated as proof of identity after reuse.

After the explicit input call, the implementation restores those three recorded
values and verifies them against a fresh observation: the prior frontmost
application and exact window, the hardware pointer position, and the affected
display's active Space. The previously frontmost window must still be present
on the current Space in that observation. A transport acknowledgement is not
effect confirmation; the result must preserve the existing click effect
vocabulary, including `confirmed` and `unverifiable`.

If restoration cannot be completed or cannot be verified, the operation stops
and reports a concrete restoration failure. It does not retry, choose another
window, silently fall back to Route A, or claim that the input was undone.
The implementation must preserve enough delivery and restoration detail for the
caller to distinguish an unattempted call, an attempted call with an
unverifiable effect, and a call whose effect occurred but whose restoration
failed.

Native Computer Use stop expires consent and cancels the current foreground
call. Restoration and reporting remain the terminal obligation before normal
daemon teardown; if the call cannot restore and verify, it stops and reports.
Stop cannot claim to undo an input that was already posted.

Any other window-system state is not restored in v1 and may differ after the
call. This includes the target's `z_index` and z-order relative to
non-frontmost windows, focus or selection inside windows, IME state, and
fullscreen or tiling arrangements. Because of the admission preconditions, the
target was already visible beforehand.

Owner-input quarantine, suppression, capture, and replay during the
focus-changing interval are explicitly out of scope for this record. The
implementation must not claim those stronger guarantees without a later owner
decision. The same applies to durable recovery markers across shell or daemon
restarts.

### User-visible indication

The owner must be able to see when foreground mode is active. The native shell
must show a visible indicator before the foreground call begins and keep it
visible until restoration has been verified. The indicator should identify the
active foreground operation and expose the existing native Computer Use stop
control. A successful restoration clears the active state; a failed or
unverifiable restoration remains visible as an attention state until the
owner resolves it.

The Workbench may mirror this state, but it cannot be the only indication
because the request may originate from an IM or another hidden surface.

## Admission and API shape

Today the Computer Use server refuses `delivery_mode:"foreground"`. That
remains the behavior until a separately approved implementation lands. A
future Route B admission branch must require the explicit foreground value,
the active native session toggle, a fresh exact target, the existing
observe-first and lease checks, and a supported operation/platform. Omitted
delivery mode remains background. An accepted background dispatch with an
unverifiable result never escalates by itself; a later foreground call must be
an explicit caller decision with its own admission.

Route B v1 admits a target only when a fresh `list_windows` call in the same
call's admission finds the exact pid and window with all of these conditions:
`is_on_screen=true`, `minimized=false`, and `on_current_space=true`. For v1,
the on-screen condition is also the no-hidden condition: Route B does not
un-hide, reveal, or un-minimize a target. Any false, null, missing, or
otherwise non-admissible value fails closed with a stable reason:
`target_off_active_space` for a missing or off-current-Space target, and
`target_not_admissible` for visibility, hidden, or minimized targets. Route B
never switches Spaces; cross-Space targets are future work.

The CLI should reuse the existing leaf command and argument shape rather than
add a new top-level foreground command or hidden retry flag. The planned
request shape is conceptually:

```text
vibe computer click --arg pid=97470 --arg window_id=6285 \
  --arg x=41 --arg y=146 --arg capture_id=capture_... \
  --arg action=click --arg button=left --arg count=1 \
  --arg modifier=[] --arg delivery_mode=foreground
```

This example documents the future contract; it does not add a product flag in
this design-only lane. The built-in `computer-use` Skill should teach callers
to use background delivery first, observe the exact target in the same Session,
carry the observation context, and verify both the application effect and
restoration. It must tell agents that they cannot grant consent, that
foreground is never an implicit fallback, and that an unverifiable background
result must not be blindly replayed.

## Windows and Q6

Route B has no Windows parity decision yet. Q6 still determines whether the
embedded daemon, proxy reachability, UIA worker, interactive-session boundary,
integrity-level behavior, and foreground-only targets can support the same
contract. Windows therefore remains refused for foreground delivery until the
Q6 machine run establishes exact process/window targeting, pointer restoration,
visible indication, effect verification, and restoration verification through
native Windows APIs.

Q6 changes Route B admission and safety evidence, not just packaging. A future
Windows implementation needs its own owner-reviewed platform contract; it
cannot infer parity from the macOS design.

## Out of scope and next decision

This record does not include:

- Route B implementation, product flags, persistent state files, schema
  changes, GUI/TCC changes, credentials, Runtime changes, or packaging work;
- a silent fallback or automatic replay from Route A;
- per-app allowlists, global permission, session-wide foreground mode, or
  agent-granted consent;
- signed shell capabilities, secure-enclave/keychain identity,
  mutual-authenticated minting, or a new process-authentication trust model;
- owner-input quarantine, suppression, capture, or replay, including restart
  persistence and recovery markers;
- cross-Space targets or Space switching;
- general foreground typing, hotkeys, scrolling, dragging, desktop-wide HID,
  login-screen or lock-screen control;
- QQ/WeChat acceptance, application-specific adapters, OCR, perception, or
  model changes; or
- Windows foreground delivery before the Q6 machine run.

Before implementation, owner and source must review and approve the first
supported click shape, native toggle lifecycle, exact restoration and failure
result, visible indicator behavior, admission/refusal rules, CLI/Skill wording,
and platform gates. That later implementation decision must also state whether
any future hardening listed above is required. This PR remains docs-only and
must not be merged without source review.
