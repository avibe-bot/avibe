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

- `ui/src/lib/messageAttachments.ts` reads `content.attachments` and decides, on
  its own, which URL may become an `<img>`: only a same-origin media-proxy URL,
  via the existing `isProxyMediaUrl`. The rule is a safety property, so it lives
  where a test can hold it without rendering a row. Round 2 made this the one
  read boundary for every attachment surface (see below).
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

## Review round 1 — head f1e97c0c, Codex review 5253087669

One findings-bearing head. Three P2 findings, all verified against the code
before acting, all three a distinct root-cause class, so no circuit-breaker
threshold applies. The orchestrator independently re-inventoried the same three
and withdrew a fourth, alleged duplication in the disclosed sheet: the expanded
branch *replaces* the inline previews, so the sheet lists each attachment once.
No slicing or deletion was introduced for that withdrawn finding.

1. **Admitted attachment shape normalization.** Two producers write
   `content.attachments`: a Web upload records `{url, mime, kind}` (the proxy URL
   is minted at upload time), an IM inbound records `{token, name, mimetype,
   size}` (`core/handlers/message_handler`) and leaves the URL to the renderer.
   `public_delivery_payload` passes `content` through verbatim, so the second
   shape reached the queue as a chip with no URL behind it — a screenshot pasted
   into Feishu arrived nameless and unopenable. Fixed in `readQueuedAttachments`,
   which now mints `/api/media/<token>` when no URL is present and reads
   `mimetype` beside `mime`. The token is percent-encoded, which is what makes
   the minted URL satisfy `isProxyMediaUrl` by construction rather than by
   assumption. No backend change: the read boundary already owned this decision.
2. **Disclosure focus lifecycle.** The expanded branch mounted a different
   control in place of the `+N` the user had just activated, so browser focus
   fell to the document. Both width-specific controls now hold their position
   across the toggle and are relabelled in place; a row disclosed at a narrow
   width also keeps a way back after the window widens.
3. **Media action routing by URL trust.** A non-proxy URL was being handed to the
   in-app file viewer, which refuses to fetch a third-party host and can only
   answer "preview failed". It now uses the same explicit external link a
   delivered attachment uses (`FileCard`: `target="_blank" rel="noopener
   noreferrer"`), with the URL passed through untouched so a signed query still
   verifies. Nothing is fetched to paint the row.

Round-1 evidence: `ChatQueueRow.test.tsx` grows to 28 cases — focus retention at
both capacities, the external link including a signed query, the IM token shape,
and the token escape that keeps a minted URL inside the proxy route. The browser
suite grows to 14 cases x 2 projects = 28, adding two fixture rows (a signed
third-party link, an IM token pair): real focus assertions through expand and
collapse, a popup whose URL is byte-for-byte the signed one with a request
counter proving the row fetched nothing, and a token-minted thumbnail decoded to
`naturalWidth > 0`. Full UI unit suite 4561 passed / 321 files; `npm run lint`
with no baseline drift; `typecheck:tests` and `npm run build` clean.

Not changed, reported instead: `file-viewer-modal.tsx` builds its download href
as `${mediaUrl}?download=1` inline rather than through `mediaDownloadHref`, which
already handles an existing query. Pre-existing, unrelated to this PR, and after
fix 3 unreachable from the queue.

Superseded: this section originally recorded the delivered transcript renderer's
token-only attachment loss as out of this lane's scope. The orchestrator traced
the same root cause through the delivery projection and extended the scope to a
shared read boundary; round 2 below is that decision and its evidence.

## Review round 2 — supplemental scope, head 43475cca

Head `f1e97c0c` remains the only findings-bearing head: Codex review 5253087669,
three classes, all three replied to and resolved. Head `43475cca` took a clean
Codex pass ("Didn't find any major issues"). No circuit-breaker threshold applies.
An independent read-only reviewer's focus, token-shape and external-URL
observations name those same three root causes and add no reviewed head; reviewed
heads are counted from a review's own commit, not from `ReviewComment.commit`,
which moves with the diff.

The orchestrator then reproduced two further defects in an isolated pinned-head
copy and granted a bounded supplemental scope. Both are measurements, not
inferences, and both are now covered by tests that fail without the fix.

4. **One read boundary instead of three.** The queue row, the transcript row and
   the lightbox gallery each re-derived "is this an image, and may we fetch it"
   from the raw record. Round 1 fixed the queue's copy only, which is not enough
   for the contract's identity property: the projection passes `content` through
   verbatim, so a token-shaped attachment that the queue could now draw still
   disappeared from the transcript, which read `url` directly and skipped the row.
   `queuedAttachments.ts` became `messageAttachments.ts` — a neutral name for what
   it now is — and all three surfaces read through it. `width`/`height` cross the
   boundary so the transcript still reserves its box; source order is unchanged;
   the gallery's membership rule is `att.image`, so a third-party URL still cannot
   enter a list the viewer pages through; and the gallery is still built from the
   transcript alone, so a queued preview remains the isolated view it opens as.
   Reported and deliberately not built: no new flow for a record carrying neither
   URL nor token. The queue keeps its existing inert chip and the transcript keeps
   skipping it — no admitted producer writes that shape.
5. **Wrapped targets overlapping.** Measured at 390px: two stacked 24px bands 4px
   apart, each extended 6px above and below by `TARGET`'s `::after`, overlap by
   8px — `elementFromPoint` three pixels under one thumbnail returned the file on
   the next line. The extensions are what make the target 36px for a finger, so
   the fix is the sheet's row gap, not the targets: 12px, which is exactly what
   two 6px extensions need to meet without crossing, and the same geometry the
   disclosure-to-sheet boundary already had. The 20px visual, the 24px band and
   the 36px collapsed row are unchanged. The separately reported disclosure-to-
   sheet overlap was measured at a 12px gap and is not a defect; nothing there
   was changed.
6. **A filename that could not be read.** `FileViewerModal`'s title used
   `truncate`: measured after a real mobile-emulated tap, `clientWidth` 220 against
   `scrollWidth` 653 with an ellipsis. For an unsupported type that title is the
   only place the name exists — the body can only say it cannot render the file —
   and a touch user has no hover to fall back on, so the complete name was
   unreachable. The title now wraps (`break-words`, which also breaks an unbroken
   run); the header is `items-start` and `shrink-0` so the icon and actions stay
   on the first line and the scrolling body is never squeezed by it. Nothing else
   in the modal changed — the pre-existing `?download=1` construction is still
   only reported.
7. **Whitespace-only text.** The row asked `!item.text`, so a stray space or a
   newline left by a paste counted as authored words and suppressed the filename
   summary, leaving a blank line. Judged on the trimmed value now, the same way
   empty text is judged elsewhere. The stored text is untouched; this decides what
   is shown, not what was written.

Round-2 evidence: `messageAttachments.test.ts` (8 cases) holds the boundary on its
own — both producer shapes, source order, dimensions, the token escape, the
gallery's membership rule, and what is not an attachment.
`MessageAttachmentIdentity.test.tsx` (4 cases) renders the same token-shaped image
and log through `QueueRow` and `MessageRow`: the same URL before and after
delivery, the log opened at the URL the delivered one links to, the uploader's
800x600 box still reserved, and no `<img>` at a third-party host on either
surface. `ChatQueueRow.test.tsx` grows to 31 with the whitespace cases. The
browser suite grows to 17 cases x 2 projects = 34, adding three fixture rows (20
attachments that genuinely wrap at both widths, a long unsupported filename, a
failed image with a long name): the wrapped-target test hit-tests the pixels just
outside each band and, with the 4px gap restored, reproduces the orchestrator's
finding exactly — a point below `wrap01.png` resolving to a `.log` on the next
line — then passes at 12px; the title tests measure the rendered box rather than
asserting text presence, requiring no horizontal clipping at any width and a
genuine wrap where one line cannot hold the name.

Full UI unit suite 4575 passed / 323 files; `npm run lint` with no baseline drift;
`typecheck:tests` and `npm run build` clean.

## Review round 3 — circuit breaker, head 9776b729

### Ledger

Codex reviews on PR #2045, counted by each review's own commit / originalCommit
rather than the mutable `ReviewComment.commit`:

| head | Codex evidence | findings |
| --- | --- | --- |
| `f1e97c0c09` | review 5253087669 | 3 — normalization, disclosure focus, URL trust |
| `43475ccaba` | issue comment 5736966752 | 0 — clean pass |
| `9776b729fc` | review 5253281275 | 1 — disclosure focus again (4051258561) |

Threads: 4 total, single page. Three resolved (two flagged outdated by the file
rename, not withdrawn); `PRRT_kwDOPbFPYs6j6qYW` unresolved and carrying the new
finding.

The focus class therefore appears on two findings-bearing heads. The intervening
clean pass does not reset the count — that head simply was not read for this
state. The breaker tripped, the lane stopped before any further edit, and the
orchestrator discharged it with the bounded decision recorded below. A future
repeated-class finding must stop again; this is not a waiver.

### Root cause of the whole class

Focus ownership was attached to a *layout's* DOM node instead of to one logical
disclosure action. Capacity was chosen in CSS, so one logical control existed as
two elements — `sm:hidden` and `hidden sm:flex` — and the third inline slot
existed as a `hidden sm:flex` wrapper. Two consequences, both reproduced in a
browser on an isolated archive of `9776b729` with exactly three attachments:

- Crossing the breakpoint turns the focused element into `display:none`. The
  browser blurs it and CSS cannot hand focus to the replacement: 390px expanded
  with focus on *Collapse attachments*, widened to 1024px, gives
  `document.activeElement === BODY`.
- The desktop control's mount condition was still `expanded || total > 3`, so at
  exactly three attachments collapsing from it unmounted it — the original
  `f1e97c0c` defect, alive in one region.

Round 1 fixed "unmounted on toggle" and asserted `activeElement` across expand and
collapse at each capacity, but its desktop case used `total = 5`, where the mount
condition is true regardless of `expanded`. The assertion passed for a reason that
does not generalise. jsdom has no media queries, so the breakpoint half was
invisible to every unit test by construction; it needs the browser suite, and the
browser suite asserted focus across both toggles but never across a viewport
change. The contract also named no focus destination for a control that
legitimately ceases to exist.

This is a local rendering/lifecycle defect. It is not a data-model or queue
redesign.

### Focus fallback contract

- One stable interactive disclosure button per row, its count and accessible name
  matching the current inline capacity. The same DOM element is retained whenever
  expand/collapse or a breakpoint change leaves a disclosure action available. Two
  focusable controls handing off between them is not an acceptable shape.
- When this control holds focus and a user action or a capacity change makes it
  unnecessary — `total = 3`, wide and collapsed — focus moves to the row's
  existing text control before the button is removed. The row text is the stable
  in-row inspection anchor. Focus never goes to Remove/Send and never falls to
  `document.body`; no empty `+0` control and no invisible focused element.
- The adjacent case in the same class is closed the same way: if narrowing drops
  the focused third inline attachment, focus moves to that same row-text anchor.
- Surviving attachment and disclosure nodes keep focus. Expanding or collapsing
  never duplicates an attachment. Focus that belongs to something outside this
  group is never moved. The original message removal/flush lifecycle is outside
  this local control policy.

### Scope decision

Allowed: `QueuedAttachments.tsx`, `QueueRow`'s text ref in `ChatPage.tsx`, the
focused tests and browser fixture, this document, and a small local capacity hook.
A `matchMedia('(min-width: 640px)')` **change** listener is explicitly approved as
the one authoritative capacity projection; the earlier in-code note rejecting a
resize listener conflated that with element measurement and per-pixel resize
handling, and is not a contract constraint. Still excluded: `ResizeObserver`,
layout remeasurement, any generalized focus framework, new dependencies, backend
or storage changes, and any other UI redesign. Capacity stays 2/3, and the compact
geometry, `+N` count, keyboard/touch behavior and i18n keys are unchanged.

### What round 3 built

Capacity became one number instead of two mirrored sets of classes. A
`matchMedia('(min-width: 640px)')` change listener holds `2` or `3` in state, and
that number drives both how many previews the collapsed line renders and whether
the disclosure has anything left to disclose. The disclosure is a single element
in a fixed child position, so React keeps the same DOM node — and therefore the
browser keeps focus on it — across expand, collapse and breakpoint changes alike.
The listener is not element measurement: it fires twice per crossing for the whole
page, reads no geometry, and does not scale with the number of queued rows.

Focus ownership is captured inside that listener (and inside the toggle handler),
because that is the last moment at which "who has focus right now" is still
answerable: once React has committed, a removed element has already taken focus to
the document with it. A `useLayoutEffect` then checks whether focus survived
inside the group; only if it did not does it call `onFocusEscape`, which
`QueueRow` wires to `.focus()` on its existing row-text control. The flag is set
nowhere else, so a group that never owned focus cannot take it from whoever does.

`QueueRow` gained a ref on the row-text element it already rendered. No new
control, no new state, no focus framework.

### Round 3 evidence

- 46 focused unit tests, including a controllable `matchMedia` mock that emits a
  real `change` event — jsdom has no media queries, which is why the CSS-driven
  half of this defect was invisible to every earlier unit test by construction.
  Eight of them cross the `sm` boundary in both directions.
- 48 browser tests across desktop (1280) and mobile (390), covering 639/640 exactly,
  a `data-focus-probe` stamp that proves the disclosure is the *same node* after a
  real resize, `activeElement` and Tab-continuation assertions for each case where a
  control ceases to exist, and the pre-existing geometry, hit-target and viewer tests.
- Both new guard families were mutation-proved: removing the focus transfer fails
  exactly the three row-text-anchor tests; forcing node replacement with a keyed
  remount fails exactly the two identity tests plus the reported sequence. The
  tests hold the invariant, not the mechanism.
- Lint (baseline, no drift), `tsc -b`, e2e tsconfig and `vite build` all clean.

## Status

- [x] Owner instruction and compact design recovered.
- [x] Isolated implementation branch created from current origin/master.
- [x] Implementation and focused validation.
- [x] Browser and API contract evidence.
- [ ] Exact-head Codex review and CI gate.
- [ ] Owner-approved merge and deployed acceptance.
