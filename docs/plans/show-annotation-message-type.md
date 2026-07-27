# Show Page annotations: one message type, visibility by type alone

Written against `master` at `9fdd4d3d` (`refactor(show): classify annotations as
harness messages`, #1039). Frozen interface artifacts live in
`docs/plans/show-annotation-message-type/`.

## The reported defect

1. Annotating a Show Page while the agent is busy shows the annotation twice:
   once in the queue strip (correct) and once as an already-delivered chat
   bubble (wrong).
2. The agent's reverse mark arrives as an ordinary agent message with a label
   glued onto the front of its text.

## Root cause — one cause, both symptoms

The reverse mark was never given a transcript message type.
`ShowSessionEventStore.append` writes it with `author="agent"`, which
`messages_service.append` resolves to `type="assistant"`, and `"assistant"` is
not in `TRANSCRIPT_TYPES`. To make it visible anyway, a back door was added to
the transcript **read** filter — `list_session_messages(...,
include_metadata_sources=("show_page",))` keeps any row whose
`metadata.source == "show_page"` whatever its type
(`storage/messages_service.py:496-540`).

That door is unconditional, and a forward annotation row carries
`metadata.source == "show_page"` in **every** state, including `pending` and
`queued`. So the transcript fetch returns the queued row
(`vibe/ui_server.py:6393`, `:6776`), `ChatPage.isTranscriptMessage` mirrors the
same bypass (`ui/src/components/workbench/ChatPage.tsx:113`), and `MessageRow`
picks its branch from `author`/`source` and never consults `type`
(`:3086-3100`). The queued annotation therefore renders as a delivered bubble
while it is still in the queue.

The queueing logic is correct. `core/session_turns.queue_pending_user_message`
moves one row in place from `pending` to `queued`; no second row is written.
Symptom 1 is a read-filter defect, not a queue defect.

Symptom 2 is the same missing type seen from the other side: with no type of its
own, the mark gets no card of its own, so its label had to be glued into the
message body to be visible at all.

A third symptom nobody reported, same cause: `core/message_mirror.py:470-474`
publishes live only `TRANSCRIPT_TYPES` and has **no** back door, so a reverse
mark never streams to an open Chat page — it appears only after a refetch. The
two visibility gates disagree, which is what a back door on one of them
guarantees.

## Invariant

The contract has four independent axes:

| Question | Decide with | Never with |
| --- | --- | --- |
| Does this row appear in the chat transcript? | `type` alone | `author`, `source`, `author_name`, `metadata` |
| Does this row drive or checkpoint a turn? | the `(author, type)` pair via the shared `INPUT_TURN_AUTHOR_TYPES` predicate | `author_name`, `metadata`, or type alone |
| Which side and title does the card draw? | `content.annotation.direction` alone | `author`, `source` |
| Does this row end the turn, supply the session preview, or notify outward? | `type` membership in the explicit terminal / preview / notifiable / mirror sets | `source`, `metadata`, or the assumption that visible implies terminal |

The first row is the visibility invariant: no query, filter, or component may
consult `author`, `source`, `author_name`, or `metadata` to widen or narrow
what the chat transcript shows. If a row must be visible, give it a type. The
live publish gate and the history fetch read the same allowlist, so what streams
equals what a refetch returns.

`type` says how a row appears. `source` says where it came from. Neither may
stand in for another axis, and `author_name` is not a behavior decision key.
Adding a type to `TRANSCRIPT_TYPES` is not enough: every new message type must
declare its membership in every behavior set explicitly, including the "no"
decisions.

`STATUS_TYPE = "status"` is the transcript-visible, behaviorally inert type for
`assistant.page.updated`, `system.runtime.error`, and migrated legacy Show
assistant rows. It is searchable, but it is not input, a reservation receipt, a
turn terminal, a session preview, an unread reply, or an outward notification.

## Contract A — one annotation type, both directions

`ANNOTATION_TYPE = "annotation"`, added to `TRANSCRIPT_TYPES`, **not** added to
`NON_CONVERSATION_TYPES`. Both directions use it, so one card family renders
both and the title states which one it is.

| | forward, dispatching | forward, non-dispatching | reverse |
| --- | --- | --- | --- |
| event types | `human.annotation.created` | `human.annotation.updated` / `.resolved` / `.dismissed` | `assistant.mark.created` / `.updated` / `.resolved` |
| `author` | `harness` | `user` | `agent` |
| `source` | `harness` | none | none |
| `author_name` | `show_annotation` | none | none |
| written type | `pending` → `queued` → `annotation` | `annotation` | `annotation` |
| `direction` | `user` | `user` | `agent` |

`human.intent.submitted` is a page-button action, not an annotation. It keeps
`source=harness` / `author_name=show_intent` / promote target `harness` and
renders as today's harness chip 「页面操作」. Out of scope, and it is why
`SHOW_TRIGGER_KIND` stays a two-entry map.

`messages_service.pending_message_target_type` gains the row's `author_name`
and returns `ANNOTATION_TYPE` when it is `show_annotation`; every caller passes
it — `core/internal_server.py:372`, `core/session_turns.py:614`,
`vibe/ui_server.py:268`, `vibe/ui_server.py:9413`.

Delete the back door: the `include_metadata_sources` parameter of
`list_session_messages` and both production call sites. Remove the parameter
rather than defaulting it — an unused escape hatch grows a caller.

## Contract B — the display record, and the prompt as a separate field

One field cannot serve a human reader and an agent prompt. Today `content_text`
is both, so the chat body carries `[show-annotation] comment`, `Anchor kind:
element`, `Anchor: <selector>`, `Screenshot: /path (1200x800)`, `Region: …`, and
a `vibe show reply` CLI hint — none of which is for the person reading chat.

`ShowSessionEventStore.append` produces two texts:

- **`transcript_text`** — human words only. Forward: the user's comment.
  Reverse: the agent's `body`. Either may be empty; the card then renders its
  title, quote, and screenshot alone.
- **`dispatch_text`** — the agent-facing prompt: exactly today's
  `_format_transcript_text` annotation branch plus the event id and the reply
  hint that `vibe/ui_server.py:9481 _show_event_dispatch_text` appends. The
  content does not change; only its destination does.

Row layout:

| field | content |
| --- | --- |
| `content_text` / `content.text` | `transcript_text` |
| `content.annotation` | the display record — frozen in `docs/plans/show-annotation-message-type/contract.ts` |
| `content.attachments` | the materialized screenshot, in the exact record shape a Web chat upload writes |
| `metadata` | machine facts, none rendered: `source`, `show_event_id`, `show_event_type`, `show_event_scope`, anchor selector, primary anchor kind, regions, classification, matched-element count |
| `metadata[QUEUED_DISPATCH_TEXT_KEY]` | `dispatch_text`, written at reserve time |

The screenshot moves from a path in the body to a real attachment because chat
already renders `content.attachments` — `ChatPage.tsx:3103-3107` builds the node
and the harness branch already mounts it at `:3279`. A thumbnail costs no new UI
and answers "which region did I mark?", which a file path never did.

The last row is what makes the split safe.
`core/session_turns.py:1291` already prefers `QUEUED_DISPATCH_TEXT_KEY` over
`text` when flushing, and `internal_server._enqueue` already stores it when it
promotes `pending → queued`. Writing it at reserve time means every path —
immediate dispatch, queued-then-flushed, swept-then-flushed — replays the full
prompt, and no path can silently fall back to the stripped display text.

The card title is **not** localized at write time. Direction is data; the label
is frontend i18n, so switching UI language re-labels existing rows. Delete the
now-unused backend keys `show.mark.created` / `.updated` / `.resolved` /
`.quoteSuffix` from `vibe/i18n/{en,zh}.json` once no other caller is left.

## Contract C — a stranded reservation repairs to `queued`, never to a receipt

`vibe/ui_server.py:255-292` sweeps `pending` rows at startup and deliberately
skips harness-origin rows, so a crash between reserving the row and settling the
dispatch leaves it stuck at `pending`: invisible forever, agent never told.

Promoting it to its visible type would be worse — the user would get a
delivered receipt for a turn that never ran. The honest repair is the state that
describes what actually happened, accepted but not yet processed: promote
`pending → queued`. It reappears in the queue strip, drains on the next turn,
and the user can see it and remove it.

Stated deliberately: a crash in the narrow window after the dispatch was
accepted but before the row was promoted re-delivers that annotation once. A
visible duplicate the user can remove beats an invisible loss. Rows swept this
way already carry their `dispatch_text` (Contract B), so the sweep needs no join
back to `show_session_events`.

## Contract D — frontend

- `ui/src/lib/chatMessageTypes.ts` owns the transcript predicate: drop the
  `metadata.source === 'show_page'` clause and add `'annotation'` and `'status'`.
  It must be a literal mirror of `TRANSCRIPT_TYPES`, nothing more. The chat
  bootstrap payload is a fourth visibility decision on the first-paint path and
  is filtered through the same guard.
- `MessageRow` — `isAnnotation = message.type === 'annotation'` is evaluated
  first; every other branch flag gains `!isAnnotation`.
- The annotation card walks the reuse ladder from the existing harness row
  (`ChatPage.tsx:3223-3286`): same collapsed geometry, a title where the chip
  is, plus quote / resolved state / attachments. Extend with props; do not fork
  the row, and do not hand-roll anything `ui/src/components/ui/` already has.
- A queued annotation shows the same title in the queue row, so the queue and
  the transcript name the same thing the same way.
- `ui/src/lib/chatTrigger.ts` — `show_annotation` no longer reaches
  `harnessChipLabelKey`; delete that branch and the now-unused
  `chat.source.showAnnotation` string. `show_intent` / 「页面操作」 unchanged.
- New strings, frontend i18n only: `chat.annotation.titleUser` = 用户批注 /
  `User annotation`; `chat.annotation.titleAgent` = Agent 批注 /
  `Agent annotation`; `chat.annotation.resolved` = 已处理 / `Resolved`.

Titles are exactly those two strings. `updated` gets no marker: a re-rendered
body already says it changed, and 「（已更新）」 spends a word without adding
information. `resolved` does change meaning — the annotation is closed — so it
renders as a distinct state element, never as part of the title.

The card's visual definition is the approved frame in `../avibe-docs/design.pen`
(see "Design" below). Build against that frame, not against this prose.

## Acceptance criteria — properties that must hold

1. **Visibility is decided by `type` alone.** For any row, whether the chat
   transcript shows it depends only on `type ∈ TRANSCRIPT_TYPES`. A row whose
   type is outside that set is absent from the history fetch and from the live
   stream regardless of its `author`, `source`, `author_name`, or `metadata` —
   verified with the exact argument set production passes, not a reduced one.
2. **One annotation is in exactly one place at any instant.** While the session
   is busy it is in the queue only; after it drains it is in the transcript
   only. Never both, in no ordering of events.
3. **Chat bodies contain only authored words.** For every annotation row the
   chat renders, the displayed text is what a human wrote — the user's comment
   or the agent's `--message`. Selectors, event ids, filesystem paths, pixel
   rects, bracketed family tags, and CLI hints appear only in the agent-facing
   dispatch text and in `metadata`.
4. **The split does not degrade the prompt.** For every dispatch path
   (immediate, queued-then-flushed, swept-then-flushed) the text the agent
   receives carries the same fields as today's annotation branch plus the event
   id and reply hint.
5. **The title states the direction.** Every annotation card shows 用户批注 or
   Agent 批注 according to `content.annotation.direction`, resolved through
   frontend i18n, and follows a UI language switch without rewriting the row.
6. **A reserved row converges to a delivered turn or a visible queue entry** —
   never to a transcript receipt for a turn that did not run, and never to a
   permanently invisible orphan.
7. **What streams equals what a refetch returns.** A reverse mark written while
   a Chat page is open appears without a reload, and a reload shows the same
   single row.

## Evidence layers

- **unit** — `pending_message_target_type` per (`author`, `source`,
  `author_name`, `content.annotation`); `transcript_text` and `dispatch_text` builders as pure
  functions, asserted against the frozen examples in
  `docs/plans/show-annotation-message-type/examples.json`.
- **contract** — `tests/test_messages_service.py`,
  `tests/test_show_session_events.py`, `tests/test_internal_server.py`,
  `tests/test_ui_show_pages.py`: the transcript fetch called with production
  arguments hides `pending`/`queued` annotation rows; a busy session yields one
  queue row and no transcript row; flushing replays `dispatch_text`; the sweep
  moves a stranded reservation to `queued` with its prompt intact; a reverse
  mark is published live.
- **frontend** — `npm run build` in `ui/`; `isTranscriptMessage` and the branch
  cascade covered by whatever test pattern already exists there; the card
  rendered side-by-side against the approved design frame.
- **manual (Incus regression)** — annotate while the agent is busy: one queue
  entry, no bubble; after it drains: one card titled 用户批注 with the comment
  and a screenshot thumbnail, no selectors; `vibe show mark`: one card titled
  Agent 批注 appearing without a reload; `vibe show reply` on a resolved
  annotation: the resolved state renders.

## Non-goals

- Deep-linking an annotation card back to its Show Page anchor.
- `human.intent.submitted` rendering.
- Renaming `mark` / `unmark` or any Show Page URL parameter.
- Any change to the `show_session_events` storage schema.
- Any release tag.

## Lanes

Two lanes, no shared files, contracts frozen before either starts.

**Lane BE** (`codex`) — `storage/messages_service.py`,
`core/show_session_events.py`, `core/internal_server.py`,
`core/session_turns.py`, `vibe/ui_server.py`, `vibe/i18n/{en,zh}.json`, the
tests named above, this plan file, and the frozen `contract.ts` /
`examples.json` artifacts beside it.

**Lane UI** (`claude`) — `ui/src/components/workbench/ChatPage.tsx`,
`ui/src/lib/chatTrigger.ts`, `ui/src/i18n/{en,zh}.json`, any new component under
`ui/src/components/workbench/`, and `ui/src/components/ui/` only to extend an
existing primitive.

Neither lane touches the other's files. The row shape both lanes build against
is frozen in `docs/plans/show-annotation-message-type/contract.ts` and
`examples.json`; a deviation goes through the orchestrator, never lane-to-lane.

## Design

The annotation card is defined in `../avibe-docs/design.pen`, extending the
approved harness-row anatomy. Owner approval of that frame gates the UI lane;
the backend lane does not wait for it.

## Lane BE implementation record

- `messages.type` is the only transcript visibility input. The metadata-source
  override and its parameter were removed rather than retained as a dormant
  option.
- `_mark_locator` builds the optional `content.annotation.quote` for both
  directions. The display record contains only `direction`, `action`, and the
  optional condensed quote.
- The annotation prompt is built once at reservation and stored under the
  shared `QUEUED_DISPATCH_TEXT_KEY`. Immediate dispatch and queue flush read
  that value; the flushed transcript row strips it.
- A materialized screenshot becomes an upload-shaped attachment only when its
  media id, local path, MIME type, width, and height are all available.
- Startup recovery promotes every harness-owned stale reservation to `queued`.
  It publishes a queue update and never a visible message receipt.
- Review round 1 made every current Show transcript producer explicit. Round 4
  corrected their behavior classification: `assistant.page.updated` and
  `system.runtime.error` write inert `status`, and historical `assistant` rows
  whose metadata source is `show_page` migrate to `status`. Historical forward
  harness inputs do not change type.
- A harness-authored `annotation` is an input turn for inbox, fork-boundary,
  Show Git checkpoint, and activity grouping. A user-authored `annotation`
  remains display-only. Settled annotations also participate in dispatch
  deduplication and global message search.
- A vocabulary completeness contract names every backend consumer that mirrors
  `TRANSCRIPT_TYPES`, plus Contract D's frontend mirror. Adding a transcript
  type now fails the test until each consumer handles or deliberately excludes
  it.
- Review round 2 tightened identity at the boundaries: only a
  harness-authored, harness-sourced `show_annotation` reservation promotes to
  `annotation`; Show Git derives its author/type SQL pairs from the shared
  input-turn identity; accepted annotations publish `show_event` activity.
  Replays of legacy settled Show inputs are accepted before looking for the
  dispatch-text field that older rows do not have.
- Review round 3 made the legacy data migration tolerate malformed
  `metadata_json`, matching the runtime reader's existing empty-metadata
  fallback instead of aborting startup.
- Review round 4 separated transcript visibility from turn completion:
  `status` is visible and searchable but explicitly excluded from every input,
  preview, terminal, unread, activity, web-push, and agent-output set by the
  completeness guard. Legacy Show reservations promote to `annotation` only
  when `content.annotation` carries the frozen display record.
- The frozen contract artifacts are committed at the spec's referenced path.
  `examples.json::_frozen_fields` is the machine-readable boundary: display
  fields compare by value, while `_queued_dispatch_text` freezes presence only.
  Its illustrative screenshot metadata and prompt use the production
  `screenshot` anchor and `x:120, y:340, 1240x620` rect format; the generic
  `state/media/med_9a71c33f8b2e.png` fixture path keeps the prompt both
  reproducible and free of a developer-machine path.
- Pre-upgrade pending Show reservations have no `_queued_dispatch_text`; retry
  reconstructs their former prompt from the stored Show event only when the
  message lacks the current `content.annotation` display record. Current
  annotations never fall back to their stripped display text.
- Backend evidence: the required five-file pytest group passes 580 tests; the
  six additional affected suites pass 202 tests; Ruff passes on every changed
  Python file.

## Open follow-up

Message-type vocabulary is still mirrored by several independently owned
consumers. This change guards the mirrors with an explicit completeness
contract; a later cross-lane change should consolidate that vocabulary into one
shared source rather than expanding this review repair into an architectural
refactor.
