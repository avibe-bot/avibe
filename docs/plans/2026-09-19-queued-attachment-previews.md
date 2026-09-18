# Queued attachment previews — implementation contract

Issue: https://github.com/avibe-bot/avibe/issues/2042
Owner authorization: "Implement issue #2042", 2026-09-19. This supersedes the earlier implementation hold recorded in the issue.
Orchestrator: ses69qdtzenet. Implementation session: sestava7bk3zw.
Base inspected: origin/master 8f09a61b03b7c2066266a05526ff0e9b61fd7b4f.

## Problem and chosen scope

A busy session already stores attachments on each queued message, but QueueRow renders only text and annotation stand-ins. A screenshot-only input can look empty or generic. The smallest complete fix is presentation plus evidence that the existing attachment path retains identity; no API, storage, dispatch, recall, or batching redesign is intended.

Design authority: avibe-docs branch `design/queued-message-attachments`, commit `5757864`, documents `docs/plans/2026-09-18-queued-message-attachments.md` and its `-handoff.md`, QMA 01–04 exports. The compact design source hash is `b80a2094c7e97a01cf81aa526e4078c4aa232a7c99d26a1a3dc4d0bd8ec865a0`. Read the isolated design worktree, not the primary design.pen, which does not contain these frames.

## Behavior contract

- Render `content.attachments` in source order beside text, within the existing queue row. All admitted attachments remain discoverable even when text is empty.
- The recognition image occupies 20 by 20 CSS pixels with cover cropping. Loading and unavailable states preserve that box. A collapsed attachment row has the same 36px height as a text-only row. Targets may use surrounding row padding without overlapping neighboring targets.
- Show at most three inline attachments on desktop, reducing capacity on narrow layouts (two at 390px) while retaining meaningful text and action width. An explicit `+N` discloses the remainder and offers collapse; text expansion does not disclose attachments.
- File chips use the same compact height, reducing to a type icon where needed. A touch/keyboard action must expose the complete filename even for unsupported preview types, a single attachment, and an unavailable image. Hover alone does not satisfy this property.
- Preview through existing media viewers. Queued image inspection is single-image mode and must remain so even when the same URL appears in the delivered transcript or the message transitions while open. Do not add queue items to the transcript gallery.
- Automatic media access uses the existing same-origin proxy policy. An arbitrary remote URL must not be fetched automatically. Explicit click-through behavior follows delivered attachments.
- Preserve annotation title/quote context. Available image previews replace the generic screenshot stand-in without hiding the annotation's identity.
- Viewing, disclosure, and text expansion do not remove, send, recall, reorder, or mutate a queued message. Message count, original remove ID, Send now behavior, and text-only recall eligibility are unchanged.
- Queue scroll body stays capped at 128px and composer remains reachable.
- Use existing tokens, primitives, and EN/ZH localization. No new dependency.

## Allowed boundaries

Implementation owns queue rendering, a small attachment component if needed, the existing viewer boundary when necessary for explicit single-image mode, translations, focused component tests, isolated browser fixtures, and this plan. Backend/API production code is out of scope unless a demonstrated defect requires a new decision. Existing API tests may be extended to verify the attachment identity contract.

## Acceptance and evidence

1. Every attachment on an admitted pending message is discoverable and inspectable with or without authored text.
2. Collapsed row geometry remains equal across attachment types, load states, themes, and supported widths; attachment disclosure is the only attachment action that expands the row.
3. Read-only preview operations preserve queue identity, attachment ordering, and all existing delivery actions.
4. Reload, switching away and back, and queue-to-transcript rendering preserve the same attachment identities without loss or duplication. API projection and dispatch evidence complements browser fixture evidence.
5. Arbitrary remote URLs are never auto-fetched; unavailable media stays identifiable without being described as a failed send.

Run focused Vitest tests, relevant lint, and the UI build. Use the existing hermetic Playwright fixture pattern for real desktop/mobile layout measurements, keyboard/touch interaction, viewer isolation, load failure, and the identity transitions above. Reuse existing backend upload/queue tests or add one focused contract case if coverage is missing; do not claim backend integrity from UI fixtures alone.

Owner acceptance after authorized deployment: queue text with an image, an image-only message, and mixed attachments while a session is busy; inspect inline and overflow previews; reload and return to the session; send the queue and compare delivered files; verify the composer remains usable on mobile. Workspace AGENTS.md names the cloud acceptance instance and retires this owner's local Incus workflow. Do not restart the user's local service or overwrite a shared test deployment as part of isolated verification.

## Delivery

One PR against master, closes #2042. Exact-head Codex review, all expected CI green, and zero unresolved threads required. The lane owns its fix watch; the orchestrator independently gates and checks scope. No merge or shared deployment without owner instruction. Record material deviations and validation evidence below as they land.

## What was built

- `ui/src/lib/queuedAttachments.ts` reads `content.attachments` and decides, on
  its own, which URL may become an `<img>`: only a same-origin media-proxy URL,
  via the existing `isProxyMediaUrl`. The rule is a safety property, so it lives
  where a test can hold it without rendering a row.
- `ui/src/components/workbench/QueuedAttachments.tsx` draws the inline group and
  the disclosed remainder. Every target sits in the same 24px band the row's
  existing icon buttons occupy, with a 20px visual inside it, so no attachment —
  of any type, in any load state — can make a collapsed row taller. Capacity is
  chosen in CSS (three at `sm` and above, two below) with one `+N` per
  breakpoint, so `display:none` also keeps the hidden variants out of the tab
  order and the markup stays deterministic without a resize listener.
- `QueueRow` renders the group and the sheet as *siblings* of its text control,
  never children, so no preview or disclosure click can reach the expand/collapse
  handler. `flex-wrap` is added to the row only while disclosed.
- A wordless queued message takes its line from its files (`first filename`, or
  `X and N more`), which covers both the annotation-screenshot row and a plain
  image-only row; the generic "Screenshot" stand-in is suppressed only when at
  least one attachment actually renders. `annotationStandIn()` is untouched.
- `ImageViewerOpenOptions.isolated` makes single-image mode a *stated* property
  of the open call rather than an inference from the URL's absence from the
  gallery — the queue strip passes it, and the viewer records it in its own state
  at open time, so a gallery that grows under an open viewer cannot hand it
  paging controls.
- A failed image keeps its 20px slot, says so in its accessible name, and routes
  its click to the file viewer, which titles itself with the whole filename even
  for a type it cannot render. Same for a chip: the full name is one action away,
  not hover-only.

Deviation from the contract as written: none. No backend production change was
needed — the projection already carries the attachments, now proven below.

## Evidence

- Focused component tests: `ui/src/components/workbench/ChatQueueRow.test.tsx`
  (23 cases) and `ui/src/components/ui/image-viewer.test.tsx` (5 cases, including
  isolation surviving a gallery that grows under an open viewer). With the i18n
  and annotation suites: 64 tests across 7 files, all passing.
- Browser evidence: `npm run test:queue-attachments` — 22 tests, desktop and
  iPhone 13, all passing. Real `boundingBox` measurements (20px visual in a 24px
  target, row-height parity across all five row types, 128px queue body with the
  composer on screen), per-width capacity and matching `+N`, keyboard tab order
  and Enter-to-disclose without touching the text control's `aria-expanded`, a
  real tap on a touch device, viewer isolation against a gallery that *contains*
  the queued URL, and a genuinely aborted image request rather than a test-only
  failure prop.
- API contract: `tests/test_ui_session_stream.py::test_queued_projection_carries_the_uploaded_file_identity`
  — a real upload, a send that settles to `queued`, and the same
  `url` / `name` / `kind` / `mime` on both the live queue read and the bootstrap
  reload path, with the token still resolving to the uploaded bytes through
  `resolve_attachment_specs`. Full module: 65 passed.
- `npm run lint` (no baseline drift), `npm run typecheck:tests`, `npm run build`,
  `ruff check` on the changed Python file: all clean.

Residual manual checks, not covered here: light/dark theme parity was not
measured in the browser fixture (the fixture runs one theme); real
`/api/media/...` bytes and real annotation screenshots were served by route
fulfilment rather than by a running service; owner acceptance on a deployed
instance is still outstanding.

## Status

- [x] Owner instruction and compact design recovered.
- [x] Isolated implementation branch created from current origin/master.
- [x] Implementation and focused validation.
- [x] Browser and API contract evidence.
- [ ] Exact-head Codex review and CI gate.
- [ ] Owner-approved merge and deployed acceptance.
