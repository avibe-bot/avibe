# Managed desktop voice cancellation at App Lock entry

Decision: main, November 23, 2026, 04:56. This is a merge gate for #4191.

Only entering the locked state invalidates unfinished locally managed recording. Cancel
capture and release resources without submitting, preserving a take, opening a
window or resuming after unlock. Preserve the existing draft, completed takes
and explicitly submitted work; cancellation does not retract requests already
sent. Off, ordinary Release and Escape retain their existing behavior.

## Producer / consumer and asynchronous inventory

| Producer or phase | Consumer / delayed work | Lock-entry contract and evidence |
| --- | --- | --- |
| Hold / Toggle native key press | queued main-thread `handle_shortcut`, `deliver_summon`, native pending summon | Invalidate the admitted generation; queued old work cannot create/show a window or dispatch Listen/Release. Reset voice latch and pending voice intent; physical release only drains the old key. Test both voice modes and stale queued delivery. |
| Mini mic button and pointer hold | `voice.start`, hold timer, asynchronous pointer query, pointer up/cancel | Cancel the old press/timer; a late callback or pointer release cannot start/finish a take or open the composer. New explicit input after unlock is allowed. |
| `petComposer.pendingListen` and `petShell.pendingSummon` | mount effect, late `pet_ready` response, summon callback | Clear recording intent without changing draft/references/send state. Remember the lock generation so a ready response begun before lock cannot reintroduce it after unlock. |
| Capture starting | microphone permission / dictation `start()` promise | Mark capture invalid before abort. A late permission success must release tracks; late start cannot call finish or open the composer. Use fake mic and delayed start. |
| Capture listening | preview, microphone/pipeline frames | Abort current take and release microphone. No additional capture/admission after cancellation; stale preview cannot overwrite current UI. |
| Capture finishing | `finish`, `done`, realtime/fallback completion | Lock cancellation uses `abort`, never ordinary `stop(false)` (which can setAside). Invalidate identity first; stale completion cannot call landed/noteDictation/openComposer or submit. Do not claim to retract already-sent requests. |
| Failure / recovery | `fail`, done.catch, detach/setAside, unmount cleanup | Invalidated capture cannot reopen draft UI, detach into saved pending takes, or affect a later capture. Ordinary unrelated failure/Escape recovery stays unchanged. |
| Repeated lock / unlock | native sync, document loading, listener mount | One invalidation per actual entry; a newly loaded Mini document receives current lock state. Unlock permits only new intent; no automatic resume. Off emits no cancellation. |
| Completed / explicitly submitted work | existing draft, kept takes already detached, send request | Leave intact. Abort only the unfinished take owned by each controller at lock entry; no global voice-store cleanup. |

## Smallest implementation boundary

The native lock owner publishes a monotonic generation and current locked state
to every native-managed top-level document, including remote Runtime Workbenches
using this Mac's microphone. One document-local lifetime primitive fences the
four existing controllers: Mini, Composer dictation, Show built-in dictation,
and Workbench/BotHold hold-to-talk. Each controller owns its existing abort
and cleanup; no global recording store or new permission architecture is added.
Native pending voice state and queued delivery use the same generation. New
and reloaded documents wait for the initial native snapshot before admission.
Absent the native marker, browser and iOS behavior is unchanged.

Cancellation clears capture identity before calling `dictation.abort()` and
clears only voice/hold intent. It must not call either ordinary stop path. The
existing dictation abort primitive owns microphone/resource cleanup and removal
of this unfinished take; no all-site voice refactor or real audio is needed.

## Validation

Use the existing Mini consumer test harness with controllable fake dictation /
permission promises for Off, starting, listening, finishing, delayed preview,
done/fail, repeated lock, late release and lock-unlock-new-capture. Assert draft
preservation, no saved-take recovery, no send/open and resource release. Keep
normal Release/Escape tests. Native peer verifies real lock transition wiring,
new-document initial state and stale native delivery. Rerun Mini drag integration
and shell boundaries after rebase onto #4190.

The Show focus assertion stays unchanged. This change must not modify unlock
replay or focus code; the independent reviewer must confirm that boundary before
using the previously approved deferred physical-focus rerun condition.

## Review clarifications and expanded boundary (05:01 main decision)

V1: A voice-only shortcut pressed while locked is Action (focus/authenticate),
never a deferred Listen/Release. A held key, repeat, or release crossing unlock
cannot record. A new physical press is required. The non-voice Mini toggle
remains Command. Update E29 accordingly.

V2: Native state publication occurs at the first main-thread presentation sync
observing actual lock entry, including refresh after passcode recovery. Repeated
sync is idempotent. A background tick marks dirty and schedules the existing
250ms sync; that is the scheduling bound, not a claim of synchronous cross-process
microphone cutoff. Cold-start documents receive the locked state before voice
admission. Default Off documents receive unlocked state and never cancel a take.

V3: Admission generations cover voice consent/availability/permission awaits,
finish-to-result/send callbacks, and fallback/recovery. Invalidated callbacks may
release resources only; no new send, take preservation, draft edit or window open.

V4: Main made Workbench Composer and built-in Show voice part of this same gate.
Mini and Show share ShowPageVoiceDictation; Composer owns its separate pipeline.
Use one document-local lock generation/lifetime primitive and abort each existing
controller through its owned cleanup path. Include all native-managed documents
(main, W1/Show, Mini), initial load and reload; no new permission framework and no
third-party page or other-device recording control. The shared dictation primitive
cancels active and finishing resources even after a surface detaches; its existing
abort preserves takes previously set aside by an explicit user action. Composer
cancellation removes only its unfinished session and restores its preview to the
pre-recording draft, preserving edits and explicitly submitted work.

| Additional producer | Consumer / asynchronous exits | Required test |
| --- | --- | --- |
| Workbench mic / shortcut | consent dialog, capture claim, native capability, permission, pipeline startup | Off unchanged; lock during each await closes a late mic, never starts later after unlock |
| Workbench capture / finalization | live preview, segment queue, realtime/http result, retained session restoration | Lock aborts unfinished owned session; late result cannot insert/recover; typed draft and completed takes remain |
| Show built-in start/stop/retry messages | availability/native permission, active session, pending result/error | Locked start rejected; old completion cannot post transcript or retryable recovery; new post-unlock request works |
| Shared dictation | start/done/detach, getUserMedia after cancellation, retry and kept-take writes | Fake stream track stops, no post-lock result or new saved take, old completed/detached explicit takes preserved |

The native signal is read-only. No new settings capability or unlock command is
exposed. Final review must cover all four controllers and native initial/reload
publication before merge. No production unlock replay/window-focus changes are
intended; only voice-specific locked admission changes by V1.

C1: Workbench/BotHold `useHoldToTalk` is a fourth controller: pressing, permission,
listening and settling must be aborted without the normal interruption/keep path.
Late pointerup, done and send callbacks check the lock lifetime. Explicitly kept
or already submitted takes remain intact. C2: Native voice keys pressed while
locked are suppressed until a physical Released, including repeats after unlock.
C3: Native main documents include remote Runtime Workbenches using this Mac's
microphone; signals are not limited to loopback. C4: Absent native integration
(browser/iOS) means unlocked with unchanged behavior. A new macOS document has an
initial pending read-only marker until native page-load synchronization supplies
its actual state; Off is published unlocked and produces no cancellation.

Review: sesmsfnku7fjm approved the expanded inventory at 05:04, conditional on
C1–C4 being captured and implemented; they are included above.

## Implementation evidence

The native peer consumes the production publication and shortcut admission
paths: Off admits voice, lock invalidates once, repeated sync is inert, reload
receives current state, and unlock requires a new physical voice press. A stale
queued press is suppressed through physical release even when its original
callback had not yet reached the native latch. The ordinary Mini toggle still
uses Command replay; native Show unlock replay/focus is unchanged.

The focused UI suite passes 397 tests across eight files, including no-signal
controls and fake microphone/pipeline tests for permission/consent, starting,
listening, finishing/settling, late callbacks, normal Release/Escape, existing
drafts and explicit new recording after unlock. Shared dictation tests consume
real controller cleanup with fake tracks, pipeline and realtime transport;
no real microphone is opened. Explicitly kept/completed takes remain intact.

The native suite has 39 cases. Thirty-eight pass, including both added lifecycle
cases rerun after the final queued-key/reload assertions. The unchanged Show
focus case remains pending the public unlock Watch; the machine is at loginwindow.
The final independent review must confirm the unlock replay/focus boundary.

Retained-take retries and disposal are completed by the state/exit table in
`app-lock-review-boundaries.md`; that table preserves completed results while
fencing locked delivery and new retry admission.
