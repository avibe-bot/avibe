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
DirectoryBrowser, NewProjectDialog, API/project providers and retained Settings
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
