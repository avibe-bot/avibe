# Desktop mini v2 delivery contract and evidence

The behavior contract is [the verbatim owner specification](2026-10-12-desktop-mini-main-agent.md).
This implementation builds on the existing binding, authorization, send-ownership,
quick-reply, and pet lifecycle paths. Backend APIs are outside this change.

## Change contract

- Vibey's image box stays at `(172.5, 449, 115, 115)` in a fixed `460 × 600`
  logical-pixel window. The column grows upward; expanded content scrolls inside
  a fixed 292px viewport. Inner detail and card exits never resize the window.
- Main history is read through `api.getMainSession()`, currently adapting the
  existing pet binding. The switcher remains available in the main-card detail.
- Main detail retains Markdown, quick replies, vault actions, queue and uncertain
  send notices. Mark-read additionally waits for the result to intersect the
  fixed scroll viewport. One document-level send owner survives component changes.
  The unread badge remains until the inbox confirms the read. Without a main
  exchange, the picker remains reachable, including when sub-task cards exist.
- `usePetSession` owns one surface epoch: binding plus authorization generation.
  A change clears retained cards, stop/dismiss phases and timers, ordering and
  the switcher before paint. Late surface callbacks cannot write into another
  epoch. Authorization changes preserve the user's draft and composing state;
  binding changes retain their existing reset behavior.
- Background activities supply sub-task cards. Running items lead when collapsed;
  expanded order is stable and new items enter below main. Watch/task pause calls
  use their native IDs, require a second press within 3s, and wait for the response.
  Success lingers for 2.5s before the two-beat exit. Agent runs have no stop control.
  Durable `enabled` watches and `scheduled` tasks can be paused while waiting;
  only `running`/`processing` executions affect the running dot, order and busy pose.
- Dismissal writes the optional `dismissed_ids` field in `pet.json`. Old files load;
  a binding change clears the IDs. Only an authoritative turn read removes stale IDs.
- The page reports clipped logical hit rectangles. The native poller runs at 16ms
  inside the window, 100ms outside, and sleeps while hidden. Both new commands
  enforce the existing pet-only origin/caller check and capability boundary.
  Unmounting the page clears the rectangles, so authorization rechecks leave
  the empty native window fully click-through.
- If the selected work area cannot fit the fixed frame, placement instead contains
  the 115px image and its dock (143px combined height). The transparent frame
  may overflow; the expanded list can be clipped at the top. Selection keeps
  image-overlap, saved-display, primary priority across all connected displays.
  The footprint is decided per target, so a small laptop keeps its pet when a
  large external display is connected. Both saved and first-use anchors take
  the same path; full-frame fitting on the target and no-display behavior stay
  intact. This is the owner-approved §9.1 fallback after breaker diagnosis.
- Hotkey press and release are distinct. A released hotkey during page/runtime
  startup opens text rather than starting a microphone later. Hold threshold is
  320ms; leaving before the threshold cancels it. Dragging retains the existing path.
- Voice ownership remains a separate commit. Composer receives only the readiness
  wait; its dictation implementation is not refactored. Pet uses the existing
  `ShowPageVoiceDictation` wrapper and thus the same capture/transcription pipeline.

## Voice: enabled; real-hardware handoff pending

Pet voice was first shipped behind a `__AVIBE_PET_VOICE_ENABLED__` window marker
that no production shell installed, so the microphone controls stayed hidden
everywhere. The owner has since removed that gate so the captured-speech handoff
can be verified on a real desktop test prerelease. Pet voice now appears
whenever Web Locks, `getUserMedia` and ASR are all available; missing Web Locks
still denies it, and hotkey/hold still falls back to text in that case. ASR
status is re-read whenever the pet window becomes active, so enabling or
disabling transcription in settings takes effect without restarting the pet.
A microphone or transcription failure reopens the text input with the pre-voice
draft and a short note; the mini does not keep the failed recording for retry
(the main Composer does).

Acceptance on the test prerelease: start dictation in the main Composer, then
claim the microphone from the mini (and the reverse). Each side must keep the
transcript of the speech it captured before the handoff. If either direction
loses speech, fix the handoff before the next stable release. Until that
transcript evidence is recorded here, the handoff remains unverified.

In the isolated macOS desktop build, `main` and `pet` both exposed Web Locks and
reported the same `avibe.voice-capture` owner/client ID. A pet claim changed that
owner and finished the main Composer's pending dictation. **The main capture had
not produced a transcript**, so this does not prove captured speech survives a
real cross-webview handoff. Background scripted audio startup was suspended, and
the native driver refused the main mic's background AX action. No foreground
activation was requested to bypass that refusal, and no shell arbiter was added.

Independent-module unit tests share one fake lock manager and prove local
ownership/release semantics, newest-claim ordering, remote-steal finalization,
and preservation of buffered PCM through `finish()`. They do not substitute for
the missing desktop captured-speech evidence.

## Visual evidence

The task shell was built as a separate **Avibe Mini.app**, identifier
`bot.avibe.desktop.mini`, and pointed through `AVIBE_DESKTOP_ORIGIN` at a test-only
loopback server. Its real native `main` and `pet` webviews loaded a production
bundle of the branch's real Composer/PetPage components. Session, authorization,
ASR responses and microphone samples were test fixtures. No production runtime,
user session, physical microphone, or external transcription service was used.
The installed `/Applications/Avibe.app` was not replaced, and the local `vibe`
service was not restarted.

The reference was rendered from the untouched prototype source copied into an
isolated temporary directory. Both views were compared at 460 × 600 logical px,
with Vibey aligned. The prototype retains its surrounding page copy, mock tool
phrases and run stop controls; production uses the specified status fallback,
existing theme mint/background roles, and hides run stop. Its fixture also has
one fewer sub-task, so the collapsed layer height differs by one 9px step.

![Prototype and native desktop comparison](desktop-mini-v2-evidence/reference-comparison.png)

![Native desktop states](desktop-mini-v2-evidence/states.png)

Individual captures:

- [Collapsed](desktop-mini-v2-evidence/desktop-collapsed.png)
- [Expanded](desktop-mini-v2-evidence/desktop-expanded.png)
- [Scrolled](desktop-mini-v2-evidence/desktop-scrolled.png)
- [Main detail open](desktop-mini-v2-evidence/desktop-main-detail.png)
- [Input bubble open](desktop-mini-v2-evidence/desktop-bubble-open.png)
- [Listening UI](desktop-mini-v2-evidence/desktop-dictating.png)
- [Pause confirmation](desktop-mini-v2-evidence/desktop-stop-confirm.png)
- [Mid-dismiss](desktop-mini-v2-evidence/desktop-mid-dismiss.png)

The listening screenshot uses synthetic audio; it is UI evidence, not evidence
of the cross-webview handoff. For a reproducible
static mid-dismiss image, the actual CSS animations were paused at 100ms using
Web Animations, and the test harness delayed row removal. Ordinary-timing tests
separately assert the 500ms removal and 2.5s stop linger. The remaining captures
wait for the requested DOM state and its transition before native window capture.
[Measured geometry](desktop-mini-v2-evidence/geometry.json) records unchanged
Vibey bounds and window dimensions across the seven steady states.

The owner corrected spec §3.3 during review: collapsed layers now match the
front card's measured height instead of a fixed 52px. The refreshed native
collapsed capture measures a 90px front card, five 90px layers, a 135px stack,
and 45px between the front and final layer's bottom edges. The reference was
recopied from the corrected prototype source. The owner's subsequent §3.2 decision
removes the desktop backing fill, retaining all padding and geometry. The gaps
show the desktop and are absent from the native hit-region reports. The prototype
keeps its page-coloured plate solely to cover the surrounding web-page text.

## Validation

- Focused pet, voice ownership, dictation-wrapper and bridge suites: 156 tests.
  The parity sweep also ran all 15 existing session-action hook tests,
  including “Show in pet”, and 13 session-action component tests:
  184 passing UI tests in total.
  The five retained-card regressions were also run against isolated copies of
  the pre-fix `be8701709` surface: all five fail because the revoked title
  remains visible. They pass with the epoch repair. The temporary copies were
  removed without changing the worktree's production files.
- `npm run test:pet-mini`: 14 real-browser layout/lifecycle checks across Chromium and
  WebKit. They cover both front-card heights and 1/3/5 layers, exact stack/peek
  geometry, long-title containment, unchanged avatar bounds on expansion, and
  transparent gaps excluded from the actual hit-region reporter's output.
  Unmounting during motion releases all hit regions without later reinstalling
  them; remounting reports the new surface's regions.
- UI production build.
- Desktop workspace Rust tests: 370 passed, including shell capability boundaries,
  fixed-window anchoring, optional persistence and cold-start shortcut release.
  Small-display cases consume the shell's actual initial-anchor function and
  window-origin/DPI conversion, checking the image and dock in physical pixels;
  they do not claim an OS display reconfiguration on this Mac.
- Desktop workspace formatting and clippy for all targets/features, warnings denied.
- Separate macOS app bundle build and native screenshots above.

The PR's exact-head Codex review, unresolved-thread inventory and CI are separate
delivery gates. Windows/Linux native visual and voice handoff evidence is not
claimed by these macOS captures.

## Test preservation

All 66 pre-v2 `PetPage` cases from base `5018b6ffc` remain. Twenty keep their
original titles and bodies; 46 adapt entry points, selectors or names to the
main-card/detail and input-slot model. Five of those have renamed titles:
four replace native-resize terminology with panel activation, and one follows
v2 §8 by including the main session's own running sub-tasks in Vibey's busy pose.
No binding, authorization, send, unread, setup or event-order invariant was
removed. The PR body lists every adapted original test and its reason.

The default bound-session fixtures now contain one user message so a main card
exists to open; separate empty-session cases cover switching with and without
background work. A failed-first-session-read case verifies that the status event
performs a second read, restores the validated chat capability and permits a
send; it no longer uses the removed v1 session-title header as a proxy.

The suite has 92 cases after additions. In particular, the quick-reply tests
exercise both orders of `message.updated` and the POST response, preserve the
single-message lock while pending, and verify an older update cannot reclaim a
newer result's options. A separate uncertain quick-reply case blocks both typed
input and newer quick-reply groups until admission uncertainty is resolved.

## v1 capability parity sweep

Checked against base `5018b6ffc` before the post-breaker push. Every requested
capability remains; the deliberate v1 removals are the resizable side-panel
geometry and automatic panel expansion, replaced by the spec's fixed stack.
The following separates automated consuming evidence from native event-wiring
inspection; it does not claim new manual OS-input tests.

| Capability | v2 status and evidence |
| --- | --- |
| Summon before ready; setup pending | Preserved. Runtime-host tests cover queued summons and release during startup; PetPage tests cover pre-ready binding/revision races and setup recovery on summon/focus. |
| “Show in pet” under the main-session mock | Preserved. `useSessionActions` still calls `petBridge.showInPet`; its three existing tests cover availability, binding, off-state and failure messages. `api.getMainSession()` reads the same shell binding. |
| Tray toggle; shortcut-refused message | Preserved. Native `toggle` persists the explicit preference, registers/unregisters shortcuts and reconciles the window. Registration failure still populates the localized tray message. Store and native catalog tests pass; OS shortcut contention was verified by source parity, not a fresh registration probe. |
| Drag and anchor save | Preserved. The avatar's drag threshold calls `startDragging`; native `Moved` debounces for 400ms and saves the image anchor in the stable coordinate space. Persistence and mixed-DPI placement tests cover the storage/restore path; the new build-boundary tests cover too-small work areas. |
| Closing only hides the pet | Preserved. `CloseRequested` still prevents closure and calls `pet::hide`, retaining the page and binding. This native event wiring is unchanged from v1. |
| Dock reopen while the pet is visible | Preserved. The macOS `Reopen` branch checks main-window visibility, independently of the pet, then restores main. Existing shell-boundary coverage and unchanged event wiring were checked. |
| Destroy on Runtime stop, loss or origin change | Preserved. The sole active-origin setter calls `pet::reconcile`; `window_change` destroys/rebuilds as required. Its consuming tests cover loss, replacement and disabling. Destruction also invalidates the poller and clears hit regions. |
| Navigation guard | Preserved. The pet admits only `/pet` at the exact Runtime origin; new windows are denied. Native tests exercise the origin/path predicate. |
| Caller checks on every pet command | Preserved. `pet_ready`, `pet_set_expanded`, `pet_hit_regions`, `pet_set_dismissed`, `pet_unbind` and `pet_open` check the pet caller. `pet_bind` admits either the validated main caller or the pet. Caller tests and the complete command/capability inventory in `shell_boundaries` pass. |
| Mark-read rules | Preserved and narrowed to the visible main detail. Active-window, complete-unread-tail and rendered-result checks remain; viewport intersection adds the v2 scroll gate. PetPage tests cover hidden/switcher/unfocused states, retry, duplicate avoidance, old binding, trimmed tails, and “More in Avibe” when the unread tail is incomplete. |

## Review inventory

The first findings-bearing head, `6b969e9d2`, received three Codex findings:

| Root-cause class | Resolution and consuming evidence |
| --- | --- |
| Durable activity status mapping | Separate pause eligibility from execution; watch/task interaction fixtures use the producer's `enabled`/`scheduled` statuses. |
| Unread affordance preservation | Restore the existing ready-state badge and test that the inbox owns its removal. |
| Binding control reachability | Keep the picker accessible before the first exchange, with and without sub-task cards; test selecting another session in both states. |

The second findings-bearing head, `fe0736241`, received one finding:

| Root-cause class | Resolution and consuming evidence |
| --- | --- |
| Native hit-region lifecycle cleanup | Clear shell rectangles when PetPage unmounts and when the shell loads a document or loses a window. Chromium/WebKit tests remove the page during motion, assert the empty surface stays click-through beyond the motion window, and verify new regions on remount. A shell lifecycle test consumes the same loading/destruction methods and checks input release and pending-summon ownership. |

The whole lifecycle class was checked in this repair:

- Authorization loading removes only PetPage: its effect cleanup clears regions
  and cancels its animation/observer work.
- Hide/show retains the document and its regions. A hidden native window cannot
  intercept desktop input; the poller sleeps while hidden and wakes on summon.
  Summon changes the rendered panel through the existing DOM reporter.
- Runtime loss or replacement destroys the window and invalidates its poller
  generation. The shell now clears the old rectangles at this boundary as well.
- Window creation now clears regions before building, without waiting for the
  first page-load callback. Navigation/reload uses the same loading method.
  Queued summons survive loading but are discarded when the window is gone.

The third findings-bearing head, `b4b835cad`, received one finding:

| Root-cause class | Resolution and consuming evidence |
| --- | --- |
| Fixed-frame work-area fallback / anchor recoverability | Select the display by image overlap, saved display, then primary; contain the full frame if that target fits it, or the image and dock otherwise. The shell's actual `initial_anchor`/window-origin conversion is tested for a detached display, a 125% primary, mixed DPI, negative origins, and first use on a narrow display. Selection priority and per-target footprint choice are also tested, including a small laptop coexisting with a full-size external display. |

Three distinct findings-bearing heads carried five findings in five classes.
Counts use the review envelope's commit, not the inline comment's reanchored
commit. The post-rewrite breaker tripped and the lane stopped before edits or
pushes. Parent session `sesexk2xpr89k` independently verified the entire GitHub
inventory, inspected the placement implementation and consuming tests, and
recorded its diagnosis and resumption authorization on 2026-10-12 at 17:52
UTC+08:00. The model stays; no architecture rewrite is needed. The classes are
v1 affordances omitted by the rewrite, plus lifecycle/placement boundaries
introduced by the fixed frame. The amended authoritative spec §9.1 records the
small-display fallback. The breaker remains armed: a repeated root class on the
next head stops the lane again before further edits or pushes.

The parent spot-check of `be8701709` identified a sibling before the next bot
verdict: globally filtering to full-frame-fitting displays moved a pet from a
small laptop to its large external screen. At 18:03 UTC+08:00 the parent amended
spec §9.1 to choose from all displays first and decide the footprint per target,
and explicitly authorized the correction. The tests now keep the laptop pet on
the laptop while still clamping the full frame on its external display. This
correction was prepared locally while review was pending; it must not be pushed
until that exact-head review is terminal.

The fourth findings-bearing head, `be8701709`, received one finding:

| Root-cause class | Resolution and consuming evidence |
| --- | --- |
| Authorized surface state outliving its binding/authorization context | One epoch from `usePetSession` invalidates retained cards and every late presentation writer. PetPage tests cover watch/task pause success and failure, dismiss with an already-queued removal callback, binding A→B→A, typed and quick-reply sends, remount uncertainty, a queued native pick, voice preview/finalization, animation hit reports, and result intersection/read completion. |

The inventory is now four findings-bearing heads and six findings in six narrow
classes. Native hit-region lifetime and authorized UI retention share the wider
family of surface state outliving its context. The lane stopped again. Parent
`sesexk2xpr89k` independently fetched the review envelopes, read the affected
code and authorized this family-wide repair at 18:12 UTC+08:00 on 2026-10-12,
together with the per-display correction. **Any finding on the next head stops
the lane for another full inventory and parent diagnosis before edits or push.**
Before this push, the parent independently inspected the epoch owner, callback
fences and per-display diff, ran nine selected consuming cases successfully,
and confirmed the repair could proceed (parent Run `936fd3cc76bd`).

### Epoch ownership and callback audit

The existing authorization event owner in `usePetSession` synchronously advances
the generation before React commits its reset. No second surface authorization
listener was added. The epoch also checks the current binding and mount lifetime.
The only production `currentBinding()` read in pet code is this central check;
the previous call-site comparisons have been replaced.

| Writer | Boundary |
| --- | --- |
| Typed send / quick reply | Capture the epoch before admission; ignore late rows, optimistic titles and draft clearing after invalidation. |
| Dismiss / stop | Clear retained cards, phase refs, leaving IDs, ordering and timers before paint. Guard confirmation expiry, pause success/failure, stopped linger and queued removal by their captured epoch. |
| Voice | Abort on epoch cleanup; fence status reads, capture startup, preview and final text. User-typed draft survives. |
| Switcher | Close on epoch reset. Gate the UI pick with the full epoch and any queued, unsent native bind with its authorization generation. |
| Hit regions | Dispose the old reporter, clear regions and fence queued animation callbacks before the new reporter takes ownership. Setup-only reporting is fenced by its mount lifetime because it has no session. |
| Mark-read | Tag result intersections with the epoch. Ignore old observer/read completions; a new epoch requires a new visible-result observation. |
| Focus / hold / sleep / post-send grace | Timers check the captured epoch before changing surface state. |

Intentional document-owned exceptions preserve existing invariants:

- The send ledger always releases its own in-flight slot, and records uncertain
  admission for the original session even after a recheck or remount. Discarding
  those writes would leave a stuck lock or permit duplicate messages. These
  records contain no retained task/watch title.
- A bind IPC already sent changes the native shell's authoritative binding.
  Its reply still enters the monotonic revision ledger; the UI reads authorized
  session data afresh. A queued pick that has not reached native is dropped on
  authorization invalidation. Using the full binding predicate on that queue
  would reject the pick's own intentional optimistic binding change.
- Panel acknowledgments stay serialized by the existing document-level panel
  owner. Persisted dismissal IDs likewise record already-completed user actions;
  they do not carry authorized presentation data. Surface callbacks that create
  those actions are epoch-fenced.
