# macOS Background Input Research

Date: 2026-10-10

This note records a read-only source review and one bounded, hermetic probe. It
does not change the installed Avibe application, TCC state, credentials, or
owner Runtime, and it does not automate QQ, WeChat, or any owner application.

## Finding

The pinned cua-driver 0.31.0 can deliver a background synthetic mouse click
that a test-owned AppKit surface accepts through normal `hitTest` and
`mouseDown` dispatch. The working route is the exact window-scoped pixel path
with background delivery, after the driver enters its raw synthetic-event
path.

This result is narrower than "all self-drawn macOS apps work in the
background." It does not establish that QQ or WeChat will accept the event.
The upstream macOS contract explicitly documents that some OpenGL, Metal, Qt,
wxWidgets, game, and pointer-reading surfaces require a real HID origin or
hardware pointer position and can discard PID-routed events.

There is also a current route-selection trap. For an ordinary background
left-click with `count=1`, the driver first performs a pixel hit-test and
attempts `AXPress` when an AX element is exposed at that point. In the probe,
the test view deliberately made AXPress a no-op for the real button behavior:
the AX counter increased, while the real `mouseDown` hit-test counter did not.
Using `count=2` bypassed that single-click AX hit-test branch and reached the
raw synthetic-event path. That is evidence that the transport works, but it is
not a correct general-purpose single-click API.

## macOS routes in 0.31.0

The pinned source tag is `cua-driver-rs-v0.31.0` at
`5272e492d61b96caf08e3bf434d91126c1f3dccc`. The repository records the
corresponding managed tool snapshot in
`desktop/cua-driver/tools-v0.31.0.json`.

The probe below used the exact earlier snapshot from source-worktree commit
`d40a5fda4c5d2be230e2a898528b25cda2cbe220`, whose SHA-256 is recorded below.
The current repository snapshot has SHA-256
`4257e598f5a6ad7e632b48e96c03257d7c5ed618a93b72820b2d4fef0f196acf` and
includes Avibe's later `click_mode:"raw"` patch. The probe therefore records
historical behavior of the earlier snapshot; it is not a conformance result
for the current snapshot or its raw-click selector.

The relevant routes are:

| Route | Mechanism | Background behavior |
| --- | --- | --- |
| Semantic AX actions | `AXUIElementPerformAction`, including `AXPress` | Calls the target's accessibility action without moving the hardware pointer. A successful AX return is not proof that a custom surface handled a real mouse event. |
| Pixel mouse, per PID | Private SkyLight `SLEventPostToPid`, with public `CGEvent::post_to_pid` fallback | Sends a synthetic mouse event to one process. The driver stamps screen/window routing metadata, including `CGEventSetWindowLocation` and the target PID/window fields. |
| Window-pointer pixel route | Window-local coordinates converted from the exact window snapshot, then routed as PID-targeted CGEvents | Provides precise window targeting. In the earlier snapshot used by the probe, a normal single left-click could be intercepted by the AX hit-test optimization before this route ran. The current Avibe snapshot exposes `click_mode:"raw"` for the exact background left-click case. |
| PID keyboard | `SLEventPostToPid` with an `SLSEventAuthenticationMessage` where needed, then public PID fallback | Background key transitions and text synthesis without a global HID post. The auth envelope is for Chromium-like targets; menu shortcuts have a no-auth path. |
| Foreground HID | Global `CGEvent` posts through the HID tap after explicit activation | Not a background route. It is the fallback for surfaces that require real HID origin or hardware pointer state. |
| Focus without raise | SkyLight event-record SPI used by the background pixel activation guard | Uses private records to make the target AppKit-active for routing while suppressing normal raise/Space-follow behavior. This is separate from the raw click transport. |

The source resolves private symbols at runtime with `dlopen`/`dlsym`. The
focus-without-raise implementation uses `_SLPSGetFrontProcess`,
`SLSGetWindowOwner`, `SLSGetConnectionPSN` or `GetProcessForPID`, and
`SLPSPostEventRecordTo`. `SLPSSetFrontProcessWithOptions` is resolved for the
broader SkyLight support surface, but the documented focus-without-raise
recipe deliberately does not call it. The raw event post uses
`SLEventPostToPid`; the public fallback is `CGEvent::post_to_pid`.

The 0.31.0 `serve` invocation used by this research and by the desktop
Runtime is:

```text
serve --embedded --parent-liveness-stdio --no-permissions-gate
      --no-overlay --socket <path> --host-bundle-id <id>
      --permission-mode standard
```

The 0.31.0 CLI also exposes `--grant existing-profile`, capability-manifest
and approval flags, cursor theme/reduced-motion flags, and experimental
history/PIP flags. None of those selects a different macOS background mouse
transport or disables the AX-first single-click optimization. There is no
`serve` flag that forces a raw single-click or enables foreground input.
`delivery_mode` is a per-tool argument; the Avibe admission layer currently
rejects `delivery_mode:"foreground"`.

## Bounded probe

The probe created two test-owned AppKit applications:

- `HitTestOnlyTarget`: a custom `NSView` whose real behavior only increments
  `hitTestClickCount` from `mouseDown` inside the button rectangle.
  `accessibilityPerformPress` increments a separate `axPressCount`.
- `ForegroundGuard`: an unrelated foreground window used to detect an
  unintended raise or focus change.

The probe evidence was retained in a temporary local evidence root.

The latest run used the installed rc4 helper solely as a process binary:

```text
driver version: cua-driver 0.31.0
driver path: installed Avibe app helper (`cua-driver`)
driver sha256: a72b09106ee1c962eef465a407b6e2f172d38944f6702e5f11e6ad85feb390d1
tool snapshot sha256: b03c3e48d1b00c8fe7c0e8b9813eb5ea104ad38313672fc4b68af203a827f43b
```

The `b03c3e48...` snapshot is the exact
`desktop/cua-driver/tools-v0.31.0.json` artifact at source-worktree commit
`d40a5fda...`; it predates the current repository snapshot and its
`click_mode:"raw"` selector. The observations below are consequently bounded
to the installed/pre-patch probe artifact and must not be read as validation
of the current checked-in snapshot.

The original post-merge acceptance helper under
`~/Applications/Avibe CUA Post-Merge Acceptance 20261009.app` was removed by
the later rc4 replacement, so its previous binary hash
`2a05d3e8...` is preserved only in the earlier acceptance evidence. The
source tag remains pinned to 0.31.0, but the probe and current repository
snapshot are distinct artifacts; this note keeps both hashes explicit rather
than treating them as identical.

Observed results from the latest run:

| Action | Result |
| --- | --- |
| AXPress through `element_token` | `axPressCount` 0 to 1, `hitTestClickCount` unchanged. The driver reported route `accessibility`. |
| PID-only pixel click | Refused as `ambiguous_window_target` because the test process exposed five eligible windows, including four off-screen windows. No event was sent. |
| Exact window pixel click, `count=1` | Routed to the driver's AX hit-test optimization. `axPressCount` increased again; the real hit-test count stayed unchanged. |
| Exact window pixel click, `count=2` | Routed as `synthetic_events`; `hitTestClickCount` increased by 2. |
| Same exact window click through `core.ComputerUseServer` | Passed after observing the window with a screenshot on the same session. `hitTestClickCount` increased by 2 more and the response retained route `synthetic_events`. |

The direct raw click and the core-forwarded raw click both returned the same
hardware cursor position before and after dispatch. The exact target never
became the frontmost application. In the latest run the frontmost app at the
raw dispatch point was an already-running user app and remained unchanged in
three follow-up samples. An earlier run kept the test guard frontmost across
the same samples. No test process remained after cleanup; the latest target,
guard, and driver PIDs were `84605`, `84610`, and `84613`.

The core forwarding result matters because it rules out a
`core/computer_server.py` event-posting defect. The server performs the
observe-first and exact-window admission checks, then forwards the pixel
arguments to the driver. The successful core run used a fresh
`get_window_state` screenshot on the same session; the earlier failed core
attempt was correctly refused with `screenshot_context_missing` because that
context had been omitted.

## Does `core/computer_server.py` need a change?

The current Avibe snapshot already exposes `click_mode:"raw"` for the exact
background left-click case, and the core server validates and forwards that
selector. The historical probe predates this selector, so its `count=2`
synthetic-event result does not validate the current raw-click contract.
Reusing `count=2` is not an acceptable product workaround because it changes
the click gesture. The next evidence question is whether the current raw
selector is accepted by target applications that reject background synthetic
events; that is separate from Route B foreground design.

## Options if a target still rejects the raw route

The existing private SkyLight focus-without-raise machinery is already the
closest background mechanism in the driver. A new private route would still
need to use undocumented SkyLight ABI and event-record layouts, preserve the
prior front process/window, make the target AppKit-active without a visible
raise, and restore the prior state. It would carry material risks:

- private symbol names, signatures, and event-record layouts can change across
  macOS releases;
- behavior can differ between AppKit, Catalyst, Electron, Metal/OpenGL, and
  other event loops;
- the target may inspect the real HID source or hardware pointer, which a
  PID-routed event cannot reproduce;
- Accessibility and Screen Recording remain separate TCC boundaries;
- private SPI use increases hardened-runtime, notarization, support, and
  rollback risk even when the binary is locally signed and the symbols are
  resolved dynamically.

The bounded foreground mode is the practical fallback for a surface that
requires true HID input:

1. Require an explicit user-authorized `delivery_mode:"foreground"` request.
2. Admit one exact PID/window observed in the same session; reject desktop-wide
   or ambiguous targets.
3. Record the current frontmost PID/window and the target geometry.
4. Briefly activate only the exact target, post the required HID event at the
   exact screen point, and restore the recorded frontmost app/window.
5. Verify restoration and the target effect from a fresh observation.
6. Refuse implicit escalation, global HID injection, and activation when the
   exact target cannot be restored safely.

That mode should remain a separate, visible product contract. It should not be
silently added as a fallback from a failed background click.

## Upstream check after 0.31.0

I compared the pinned macOS input and click sources at
`cua-driver-rs-v0.32.0`, `v0.33.0` through `v0.33.4`, `v0.34.0`, and
`v0.34.1`. The tags resolve to:

```text
v0.32.0  58aba84b5b83d77e7e2b0f006547699eb594e50d
v0.33.0  1553a3f360ea12155be3bc77e27c427ca62f967a
v0.33.1  a9baa8d107fba8b0aef5a4ed6233e498e88d14d0
v0.33.2  c82d32e3e1adbc6578a148962002ddf6e3e8a15a
v0.33.3  a46cd19cff980c9269425bb8aefa90588d5924d3
v0.33.4  ba0033a661101669c60ce05f1fe5753bf8bad748
v0.34.0  b0968e1b12834e485dda68789541a3cc57664a9f
v0.34.1  0c69d9a2c8abbb5051a2df4a23f0fa166d5385dc
```

The later releases change the surrounding SDK/tool contract, window
enumeration, coordinate and input timing behavior, modifier aliases, menu and
consent handling, cursor behavior, and refusal/verification paths. The
macOS-specific diffs do not add a new background click transport that replaces
the 0.31.0 `SLEventPostToPid`/public PID fallback or the existing
focus-without-raise recipe. The later source still documents the same
limitation for canvases and other surfaces that require real HID origin.

## Scope and follow-up

This is a research result, not an implementation recommendation to enable
foreground input. The current raw single-click selector is already part of the
checked-in Avibe contract. The next implementation question is validation of
that existing selector against target applications that reject synthetic
events; that requires a driver contract and consuming test before any further
change to `core/computer_server.py`.

The probe sources are not included in this PR; this note is the evidence
record. No commit, push, PR, TCC mutation, credential access, installed-Avibe
change, QQ/WeChat action, or owner Runtime restart was performed.
