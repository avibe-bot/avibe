# App Lock review boundary completion — November 23

Main approved this one-round scope at 05:44 and clarified retained-take ownership
at 05:46. Remediation base: 8449a8b01daa1f1cffff9761199e3b96695bb827.
That base is not mergeable. Default Off and native unlock/focus are unchanged.

## Review inventory

GitHub returned every thread (`hasNextPage=false`). Findings-bearing Codex heads:
6946e2a778 (two findings) and 8449a8b01d (three); 6bcdb8f281 was clean.
Main independently checked the inventory, code and consuming tests before scope
approval. Independent reviews additionally identified Direct checkmarks and the
unfinished voice lifetime; these are not additional Codex heads.

| Thread | Root cause / disposition |
| --- | --- |
| PRRT_kwDOPbFPYs66n6Gh / 4659907358 | Link-free secondary launches classified as Navigation; fixed and resolved |
| PRRT_kwDOPbFPYs66n6Gl / 4659907363 | Locked key release did not drain physical state; fixed and resolved |
| PRRT_kwDOPbFPYs66oeMA / 4660148764 | Completed Show take lost its retry owner; unresolved, F1 below |
| PRRT_kwDOPbFPYs66oeME / 4660148768 | Startup assembly reversed explicit/fallback priority; unresolved, F2 below |
| PRRT_kwDOPbFPYs66oeMG / 4660148772 | Deferred menu redraw covered only Direct; unresolved, F3 below |

## F1 — retained take state × exit

A completed take keeps one reachable owner. Lock invalidates delivery, not the
retained audio/result. No new capture, automatic retry, draft insertion, send,
transcript presentation or window focus while locked. Successful late retries
are retained on that same take for explicit consumption after unlock, without
another network transcription. Check identity as well as the native lifetime.

| Owner / state | Lock / late completion | Explicit action after unlock / disposal | Consumer evidence |
| --- | --- | --- | --- |
| Show starting/listening/finishing an unfinished take | Existing abort and identity invalidation | New explicit take only; repeated lock inert | Existing phase tests |
| Show settled retryable, with or without keeper | Retain original active owner; no abort/detach/cancel event | Existing retry action remains reachable; actual failure permits network retry | Actual host retry after lock/unlock, both target shapes |
| Show retry in progress on a settled take | Fence old delivery; retain success text on same active owner, actual failure stays retryable | Explicit use consumes cached success once; no duplicate transcription | Controlled delayed retry, no result while locked, explicit later result exactly once |
| Show dispose while locked / later reload | Existing detach releases keeper; no destination follows existing disposal behavior | Pending items remain discoverable; no retry admission while locked even on online/visible; explicit use after unlock | Real keeper/store and pending item consumer, including online/visible |
| Show disposal during an admitted retry | Retain its keeper until that retry completes, then detach once | Pending owner receives that same persisted result; no competing transcription, including unlock before completion | Real dictation/keeper/store and rendered pending item; one explicit insert, no send |
| Composer retained retry in progress | Retain settled result on same session; do not insert or change draft | Existing ready-result action consumes it; failed result keeps retry | Controlled retry, original draft, actual ready action after unlock |
| Hold interrupted take retry in progress | Keep same interrupted owner; retain new success text privately while locked | Present retained text after unlock; explicit send only, no second transcription | Controlled retry then actual interrupted-send, no late cross-owner delivery |
| Mini failed take / previously pending take | Mini already detaches to pending; no new in-surface retry path | Narrow pending retry admission gate below; already submitted requests not retracted | Existing Mini tests plus pending consumer |

Pending items auto-retry on first appearance, online and visible, so detach alone
cannot prove safety. Reuse the native document lock at the actual retry admission
point, including asynchronous claim/audio-read boundaries. Off/no marker remains
unchanged. An admitted pre-lock request may complete into its same persisted take;
this does not authorize insertion/send, nor revoke an already sent request.
After unlock, the existing online/visible automatic retry behavior resumes;
it only transcribes and never inserts or sends. Any automatic attempt also counts
as this document's initial attempt, preventing a stale pending row from repeating
an online-triggered request as it ends. Hold uses an unlock subscription to show
privately retained text; neither polling nor a recording request is involved.

## F2 — cold startup intent assembly

Use master behavior: explicit supported arguments precede the setup-resume
fallback; the generic parser continues to consume its first supported link.
Update opening remains its independent side effect, before applying the link.
Only cold startup adds the resume marker. Secondary launches and Apple/notification
URL delivery retain their existing semantics.

| Launch arguments | Resume marker | Consumed intent (Off and deferred replay) |
| --- | --- | --- |
| Session / Show / settings link | present or absent | Explicit destination |
| Several supported links | present | First explicit supported link |
| Update OPEN_URL plus supported link | present | Open update, then explicit destination |
| Update OPEN_URL only | present | Open update, then CU setup fallback (master ordering) |
| Empty / invalid only | present | CU setup fallback |
| Empty / invalid only | absent | No navigation or update |
| Explicit link only | absent | Explicit destination |

Extract production startup assembly/routing only as needed for real DeepLinks
consumption through Off and locked-unlock cases. No parser-order rewrite.

## F3 — all checkable native menu items

After admission is deferred, redraw authoritative state without executing the
intent. Preserve the existing toggle/choose implementation for successful replay.
Use existing owners and controls; no new menu framework. Reads/redraws must not
write preferences, toggle, query permissions or launch workers.

| ID | Authoritative read | Read-only render |
| --- | --- | --- |
| Direct Automatic | Direct chosen mode for shown target | Existing Direct refresh |
| Direct Tunnel | Same Direct mode | Existing Direct refresh |
| notifications | Notifications.enabled() | Existing notifications CheckMenuItem |
| start-at-login | autolaunch().is_enabled(), same false fallback on read failure | Existing login CheckMenuItem |
| show-pet | Pet.enabled() | Existing Pet menu refresh |
| computer-use | computer_use_menu_state(controller.view()) | Existing CU item checked/enabled/text render |
| desktop-update-test | Updater state.channel == Test | Existing channel CheckMenuItem |

The native test uses real CheckMenuItems and optimistic toggles before calling
the production menu handler. Test all seven IDs, cancellation, replacement,
authoritative value/checkmark agreement, and zero preference writes. Explicit
successful authentication executes once; replace only external effects (OS login
registration, CU permission/worker, update/network) with test-owned terminal sinks.
The login-state read itself is replaced with a fixture, not just enable/disable.
Computer Use reads only the fixture controller view and starts no worker.

## Consumer evidence

The new Show owner tests failed against the old behavior (three failures), then
passed with ownership retained. The real keeper/store tests additionally render
the pending list, reload it while locked, dispatch online/visible, and explicitly
insert the recovered text after unlock. Disposal during an in-flight retry keeps
the original owner live until its result is persisted; no second request occurs.
Composer recovers through its existing ready-transcript action; Hold sends only
after an explicit post-unlock action. The eight affected UI suites pass 418 tests.

The new native startup test failed before the fix by navigating to Computer Use
instead of the explicit Session; the new seven-item menu test failed because the
Computer Use checkmark differed from its effective value. Both pass after the
fix. Startup covers eleven argument combinations in both Off and deferred modes.
Menu cases consume the production handler with actual AppKit checkmarks, cancel
and replace deferred work, compare test-owned disk bytes, and verify successful
notification replay toggles exactly once.

## Gates

Record one old-red/new-green consuming test per family. Original reviewer checks
these tables once, then reviews the single final SHA. Run focused suites first,
then required Rust/UI/build checks. Keep the unchanged Show focus assertion and
public unlock Watch. New same-family omissions or product decisions return to
main before another push. Main owns Codex triggering; fresh exact-head review,
zero unresolved threads, green expected CI and CLEAN are mandatory for merge.

## First-load budget follow-up (November 23)

The clean `fccd8c8f96` production build measures 614,509 gzip bytes for home
(109 above the unchanged 614,400-byte budget) and 915,200 for chat
(within 921,600). The eager graph adds only `desktopVoiceLock.ts`; no new
third-party eager dependency accounts for the increase.

The local lazy-card experiment is withdrawn: a retained take can first appear
offline, before any idle prefetch, and must remain visible and usable. The new
production-browser regression fails against that experiment and passes with
the eager card. It discovers the same persisted take offline, keeps the editor
usable, then transcribes once on reconnect and consumes it by explicit Insert.
No microphone or real service is used.

The authorized bounded reduction round did not produce a passing candidate.
Presentation deduplication reached 614,483 home bytes (83 over); shared bridge
request parsing reached 614,501 (101 over); additionally sharing availability
publication returned home to 614,509 (chat 915,176). All experiments are reverted.
The final clean rebuild reproduces 614,509 / 915,200. Strict locked delivery
semantics, eager offline retrieval and both budgets are unchanged. Further
optimization scope requires the orchestrator's decision; no failing candidate
is pushed or merged.

## CI failure inventory — run 45076783540

The exact-head Codex summary for `fccd8c8f96` is completed with no findings;
all five review threads are resolved. CI still blocks merging. Job logs and
workflow `needs` establish these causes (not inferred from aggregate names):

| Failed job | Evidence and root cause | Disposition |
| --- | --- | --- |
| build-linux-artifacts | First-load budget step: home 600.1 KiB > 600 KiB | Independent budget blocker; separate optimization lane |
| desktop-shell / macos-26 and windows-latest | Same shell boundary assertion: login checkbox call count expected 1, actual 2 | Independent outdated source contract; allow authoritative read-only redraw, preserve unique write adapter |
| install-upgrade-regression | Aggregate rejects skipped install-upgrade-shards; shards need build-linux-artifacts | Consequence of budget failure; installation cases did not run |
| windows-install-smoke | Aggregate rejects skipped smoke runners, web-fetch-runtime/macos, windows-model-hub and windows-control-ipc; each needs build-linux-artifacts | Consequence of budget failure; these cases did not run |
| scenario-evidence | Rejects skipped install-upgrade-shards, windows-model-hub and windows-control-ipc | Same dependency failure, no separate scenario-test failure |
| unit-tests | Rejects failed scenario-evidence, desktop-shell and build-linux-artifacts; actual Python shards succeeded | Aggregate of the two independent causes above |

Windows shell artifact uploads also fail because the security producer was
skipped after Test Rust failed; the job step results confirm this ordering.
No CI gate or workflow is relaxed. The adjusted shell contract keeps the unique
write adapter and verifies authoritative redraw without enable/disable, preference
writes or calls back into write adapters. In-memory injected enable, disable,
adapter and preference-save mutations are all rejected. The CI-equivalent
`--locked --all-features` shell-boundary suite passes all 54 tests locally on
macOS; its platform-neutral source checks normalize Windows line endings.
Windows execution remains for the final remote CI run. Rust formatting passes.

## Approved independent dependency correction

The orchestrator subsequently approved the isolated non-voice candidate
`ade0da5590`, integrated after the shell-boundary contract correction. It moves
the unchanged routeable catalog ID projection into a lightweight module; the
first-screen picker no longer imports the catalog editor. Existing deferred
editing boundaries and all voice/App Lock behavior remain unchanged. The
experiment measured home 613,105 and chat 913,738 gzip bytes, both within the
original budgets. See `4191-home-static-dependencies.md` for the import graph and
consumer evidence. The abandoned lazy-card and semantic-reduction experiments
are absent from the final source diff. The combined head must repeat production
build/budget, offline consumer and shell-boundary checks, receive independent
review, and pass fresh remote CI and exact-head Codex review before merge.
