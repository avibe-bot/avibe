# Route B: Bounded Foreground Delivery

Status: owner-facing decision record. The Route B v1 implementation follows
this record. Owner and source review are required before merge.

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

### Owner amendment: reveal and restore hidden or minimized targets

On 2026-10-14, the owner approved a Route B v1 amendment based on observed
Codex behavior: a target application that is hidden, or an exact target window
that is minimized, may be admitted when it is on the current Space. The native
shell may reveal it for the bounded call and must restore its original
hidden/minimized state before the call is complete. Missing and off-Space
targets remain refused, and Route B never switches Spaces.

The first delivery PR stages this amendment behind the existing change-size
guard: it ships the bounded foreground contract for already-visible targets,
then follows immediately with native reveal and hidden/minimized restoration.
The staged delivery does not change the approved end-state contract.
For that visible-target first PR, the finite restoration contract contains
three components: the prior exact frontmost window, the hardware pointer, and
the active Space recorded separately for the prior frontmost window's display
and the target window's display. Space restoration happens only by
re-activating the prior exact frontmost window; the shell does not call private
Space APIs or switch Spaces, and one fresh observation verifies both recorded
per-display Space values.

On 2026-10-14, the owner selected Option A for the transaction lifecycle. The
Controller owns one short, non-cancellable foreground transaction from
admission through the terminal result. Native stop paths close admission for
new foreground calls, then wait up to five seconds for the transaction to
reach terminal. They do not send cancel, interrupt, or join messages into the
transaction. If the bound expires, the stop proceeds and reports incomplete
restoration through the native attention state and any result that remains
deliverable. The transaction owner is the Python Computer Use server; the
native shell provides consent, the active indicator, the macOS restoration
primitive, terminal acknowledgement, and bounded stop handling.

Known limitation: if the owner stops during the sub-second transaction, the
click can complete before the stop takes effect; if a platform call hangs past
the bound, restoration may be incomplete and is reported as such.

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
2. the hardware pointer position;
3. the active Space of every involved display, recorded separately for the
   prior frontmost window's display and the target window's display; and
4. the target's original hidden or minimized state.

The target must be one freshly observed application window in the same Session
and must satisfy the admission preconditions below. The implementation must
retain the target identity and coordinate context needed to confirm that it is
still the requested window before posting the foreground input. Numeric PID or
window identifiers must not be treated as proof of identity after reuse.

After the explicit input call, the implementation restores those four recorded
values and verifies them against a fresh observation: the prior frontmost
application and exact window, the hardware pointer position, the affected
display's active Space, and the target's original hidden/minimized state. The
previously frontmost window must still be present on the current Space in that
observation. A transport acknowledgement is not effect confirmation; the
result must preserve the existing click effect vocabulary, including
`confirmed` and `unverifiable`.

If restoration cannot be completed or cannot be verified, the operation stops
and reports a concrete restoration failure. It does not retry, choose another
window, silently fall back to Route A, or claim that the input was undone.
The implementation must preserve enough delivery and restoration detail for the
caller to distinguish an unattempted call, an attempted call with an
unverifiable effect, and a call whose effect occurred but whose restoration
failed.

Native Computer Use stop expires consent only after the current foreground
transaction reaches terminal or the five-second stop bound expires. It does
not cancel an in-flight foreground call. Restoration and reporting remain the
terminal obligation before normal daemon teardown; if the call cannot restore
and verify, it stops and reports. Stop cannot claim to undo an input that was
already posted.

Any other window-system state is not restored in v1 and may differ after the
call. This includes the target's `z_index` and z-order relative to
non-frontmost windows, focus or selection inside windows, IME state, and
fullscreen or tiling arrangements. The target's hidden/minimized state is
restored in the approved reveal follow-up; it is outside the visible-target
first PR's three-component contract.

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
call's admission finds the exact pid and window with `on_current_space=true`
and verifiable hidden/minimized state. A target with `is_on_screen=false` or
`minimized=true` is admitted when it remains on the active Space; the native
shell may reveal it for the call and must restore that state afterward. Any
false, null, missing, or otherwise non-admissible value fails closed with a
stable reason: `target_missing` for an absent exact window,
`target_off_active_space` for a window outside the current Space, and
`target_not_admissible` for an unverifiable or otherwise unsafe target. Route B
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

The built-in `computer-use` Skill teaches callers
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

- durable allowlists, persistent consent state, schema changes, GUI/TCC
  changes, credentials, or Runtime changes;
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

The v1 implementation remains bounded by the finite four-item restoration
contract above. It must not expand that contract in response to a review
finding without a new owner decision.
