# Home draft and first-send contract

Lane B of the approved desktop alignment: external committed contract
`9ff92b7f02ad950723d0ccf73218052ad2f7c7d3`,
`docs/plans/desktop-alignment-20260919/execution-contract.md`, plus the
orchestrator's September 19 07:16 amendment granting the optional
`NewProjectDialog.initialPath` boundary.

The home retains local Files, text, Agent/project selection and voice state.
Only explicit Send creates the eventual session, uploads into that scope and
submits the first message. Navigation carries no replayable first-message state.
A definite rejection keeps the draft and successful upload progress for explicit
retry. Switching the selection preserves the original Files and gives a later
submission its own upload scope. Closing a picker or opening Settings does not
replace the draft. The shared new-session sheet uses the same first-message
submission owner; existing chat media upload and queued attachment presentation
remain unchanged.

There is no generic caller-provided idempotency key on ordinary message POST.
A lost response or unresolved dispatch therefore retains the draft, blocks
resubmission and offers the created conversation in another tab for inspection.
A fresh global new-session sheet open clears the prior submission lifetime and
starts with an empty draft; an older request cannot alter the new sheet. Terminal
session errors discard the invalid upload scope before explicit retry, while
ordinary dispatch/upload failures keep partial progress.

This is an intentional limit: the frontend never guesses whether an uncertain
turn ran. Abandoned drafts are component-local, not reload-persistent. A failed
attempt may leave an empty session, using the existing service lifecycle.

## Verification

Run from `ui/`:

```sh
npx tsc -p e2e/home-media/tsconfig.json --noEmit
npx playwright test --config playwright.home-media.config.ts
```

The fixture mounts the real Workbench, Composer, AgentRoutePicker,
FolderBrowser, NewProjectDialog, API/project providers and retained Settings
route boundary under StrictMode. It intercepts all fetches in memory; external
network traffic is refused. Audio capture uses Chromium's fake microphone with
the real MediaRecorder/recording pipeline and HTTP transcription boundary.
No Avibe service, credentials, user browser profile or real project is accessed.
The conversation destination records router state: the lane intentionally does
not replace ChatPage or modify its queued attachment behavior.

Assertions cover final project/Agent scope, Unicode file names/content, same
scope retry, attachment-only submission, ambiguous acknowledgment, Settings
during upload, voice cancel/finalize before session creation, and localized
responsive geometry. Screenshots are generated at 320, 375, 390, 1366 and 1600px
in dark/light Chinese/English under `e2e/.artifacts/home-media/`.

Production authentication/transcription and the integrated standalone Settings
shell remain for the orchestrator's authorized acceptance pass. The fixture is
interaction evidence, not live backend or cloud acceptance.

## Settings suspension follow-up

The follow-up branch starts from merged master (including PRs #2047/#2045),
under execution-contract amendments 10:06, 10:13 and 10:54, reference
`824544d91`. Its production change consumes the existing route activity context;
C's AppShell must keep the NewSessionSheet owner mounted, preserve logical
`open={newSessionOpen}` and provide `active={!settingsOpen}`. The lane's shell
producer is a test fixture, not C's implementation; full B+C acceptance remains
separate.

The sheet's existing pendingDraft now survives modal-content unmounts. A true
close invalidates old Composer callbacks; Settings only removes foreground
presentation. The existing useNewSession checks still stop a create/upload
completion before the next POST while inactive. A POST already admitted can
finish: the sheet holds its successful result and uses the current navigator
once on return, or preserves the retry/error/uncertainty state. No automatic
resubmission and no replay state are introduced.

FolderBrowser retains unconfirmed paths, history, manual-path input and
new-folder text while withdrawing its modal and global keyboard/focus effects.
NewProjectDialog retains confirmation state and alone owns deferred project
completion for all callers. A successful or authorization-fenced null result
is delivered through the current callback only after foreground returns. A
cancelled or unmounted dialog cannot reopen an obsolete flow.

`sheet-suspension.spec.ts` drives the actual sheet, project dialog and directory
picker through the frozen shell producer shape. Alt+S is a fixture-only route
entry (the real sheet has no Settings button); all other input, selection,
submit, close, keyboard and return actions use real components. It exercises
Unicode state, 390/1366px, accepted/rejected/uncertain network completion,
pre-POST suspension, true close/fresh reopen, project creation success/failure/
cancel and retained directory inputs. Unexpected requests fail the fixture;
all external traffic is refused.

The picker fixture models `/api/browse` for canonicalization and the Files
list/search/mkdir endpoints for actual browsing. Held browse requests block
the Files listing, not the compatibility resolver. Successful navigations
commit history; refreshes and stale/failed completions do not. A newer
unsubmitted path draft survives an admitted navigation, including its caret.
Escape explicitly cancels a pending manual submission, so reopening the editor
cannot revive that submission or add it to history.
