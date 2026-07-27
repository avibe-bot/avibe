# Show Page annotations: one message type, visibility by type alone

Frozen interface artifacts live in
`docs/plans/show-annotation-message-type/contract.ts` and
`docs/plans/show-annotation-message-type/examples.json`.

## Reported defects

1. Annotating a Show Page while the agent is busy shows the annotation twice:
   once in the queue strip and once as an already-delivered chat bubble.
2. An agent reverse mark arrives as an ordinary agent message with its label
   embedded in the body.
3. A reverse mark written while Chat is open does not stream live and appears
   only after a history refetch.

## Root cause

Reverse marks had no transcript message type. `ShowSessionEventStore.append`
wrote them as `author="agent"`, which became `type="assistant"`.
`assistant` is intentionally absent from the transcript type set.

History compensated with a metadata back door:
`list_session_messages(..., include_metadata_sources=("show_page",))` retained
every row whose `metadata.source` was `show_page`, regardless of type. Forward
reservations carry that metadata while they are `pending` and `queued`, so the
history fetch exposed an undelivered queue row as a chat message.

The live message publisher had no equivalent override, which made live and
refetched visibility disagree. Activity grouping and session-fork anchoring
also grew `metadata.source == "show_page"` exceptions. All are consequences of
the missing type.

## Invariants

| Question | Decide with | Never with |
| --- | --- | --- |
| Does the row appear in Chat? | `type` alone | `author`, `source`, `author_name`, or `metadata` |
| Does the row drive or checkpoint a turn? | the catalog's `(author, type)` input pairs | `author_name`, metadata, or type alone |
| Does the row group as activity? | catalog `activityRole` | Show-specific metadata |
| Which side and title does the card draw? | `content.annotation.direction` | `author` or `source` |
| Does it preview, settle, notify, or become unread? | the corresponding catalog property | the assumption that visible means terminal |

An annotation is an input turn only for `(harness, annotation)`. It is never an
activity-grouping event. These are independent axes.

## Contract A: one annotation type

The only new message type is `annotation`. `status` is not introduced. The
catalog entry is:

```json
"annotation": {
  "transcript": true,
  "searchable": true,
  "inputAuthors": ["harness"],
  "acceptedReservation": true,
  "render": "annotation"
}
```

The inherited defaults are deliberate:

- `inboxPreview: false`
- `inboxSettlesReply: false`
- `activityRole: "none"`
- `terminalWhenEvents: []`
- `unread: false`
- `webPush: false`
- `inboxActivity: true`

`acceptedReservation: true` makes a settled Show replay idempotent.

### Row identities

| Flow | `author` | `source` | `author_name` | Written type |
| --- | --- | --- | --- | --- |
| dispatching forward annotation | `harness` | `harness` | `show_annotation` | `pending` -> `queued` -> `annotation` |
| non-dispatching new forward annotation | `user` | null | null | `annotation` |
| reverse mark | `agent` | null | null | `annotation` |
| Show intent | `harness` | `harness` | `show_intent` | existing `harness` flow |

`pending_message_target_type(author, source, author_name)` returns
`annotation` only for the exact harness Show-annotation identity. Every caller
supplies all three fields.

### Lifecycle write rule

A chat row is written only when someone wrote words that were not already
delivered:

| Event | Chat row | Reason |
| --- | --- | --- |
| `human.annotation.created` | yes | new user-authored words |
| `human.annotation.updated` | no | lifecycle update to the existing page annotation |
| `human.annotation.resolved` | no | lifecycle update to the existing page annotation |
| `human.annotation.dismissed` | no | lifecycle update to the existing page annotation |
| `assistant.mark.created` | yes | new agent-authored body |
| `assistant.mark.updated` | yes | new agent-authored body |
| `assistant.mark.resolved` | yes | new agent-authored body |

A user resolving an existing agent mark does not create another agent-authored
row.

`assistant.page.updated` and `system.runtime.error` continue to resolve to
`type="assistant"`. Removing the history back door intentionally hides them.
They do not receive a replacement type or a data migration.

## Contract B: display text and dispatch text

One text field cannot serve both a human transcript and an agent prompt.
`ShowSessionEventStore.append` therefore produces:

- `transcript_text`: authored human words only. Forward annotations use the
  user's comment; reverse marks use the agent's body. It may be empty.
- `dispatch_text`: the full agent-facing prompt, including the existing
  annotation family tag, anchor details, selector, screenshot path and region,
  event id, and reply command guidance.

The row layout is:

| Field | Content |
| --- | --- |
| `content_text` / `content.text` | `transcript_text` |
| `content.annotation` | frozen display record |
| `content.attachments` | materialized screenshot in upload attachment shape |
| `metadata._queued_dispatch_text` | `dispatch_text` from reservation time |
| remaining `metadata` | machine facts only |

`_queued_dispatch_text` is stored under the shared
`QUEUED_DISPATCH_TEXT_KEY` before dispatch. Immediate dispatch, queued flush,
and retry dispatch all read the stored value. Promotion to the delivered row
strips the private queue metadata.

Pre-upgrade reservations can reconstruct their former prompt from the stored
Show event. A current annotation with a display record never falls back to its
human-only transcript body.

## Contract C: reservation recovery

A stale `pending` reservation proves only that the row was persisted. It does
not prove that an agent turn started.

Startup recovery therefore:

1. deletes pending rows whose session is missing or archived;
2. makes ordinary user-authored Chat rows visible without redispatching;
3. leaves harness-owned reservations `pending` with
   `_queued_dispatch_text` intact;
4. publishes neither `queue.updated` nor `message.new` for the unrun turn.

Replaying the originating Show event reuses the same reservation and initiates
the real dispatch. Only controller acceptance promotes it to its final catalog
type. Recovery never creates an ownerless `queued` row and never fabricates a
delivered transcript receipt.

## Contract D: frontend consumption

The frontend consumes the frozen row shape in `contract.ts` and
`examples.json`. It admits transcript rows by catalog type and chooses the
annotation card's side and title from `content.annotation.direction`.
Because `annotation` is shared by harness, user, and agent authors, search
results classify the row from its actual `author`; the type's
`inputAuthors=["harness"]` capability identifies only the harness input pair
and is not a display-role shortcut.

The backend and frontend implementations keep separate file ownership, then
land together so no release contains the new type without its consumer.

## Migration

Revision `20260727_0038` performs two pinned changes.

### Inbox input index

SQLite cannot alter a partial index. The migration drops and recreates
`ix_messages_inbox_user_send` with the literal predicate:

```sql
session_id is not null and (
  (author = 'user' and type = 'user')
  or (author = 'harness' and type = 'harness')
  or (author = 'harness' and type = 'annotation')
)
```

The migration does not call the live catalog. A drift test compares the pinned
literal with today's `build_partial_index_predicate` output.

The other predicates do not change. `ix_messages_inbox_activity` includes
`annotation` automatically through its exclusion model, and
`ix_messages_inbox_agent_reply` remains limited to `result`, `notify`, and
`error`.

### Legacy Show rows

The migration is defined by supported event behavior, not by the event counts
observed in one installation. It converts every delivered legacy row that had
depended on the Show metadata visibility exception:

- `assistant.mark.created`, `assistant.mark.updated`, and
  `assistant.mark.resolved` become agent-authored `annotation` rows;
- delivered `human.annotation.created` rows with stored `harness` or `user`
  type become user-authored `annotation` rows.

Reverse marks receive a minimal `content.annotation` record:

```json
{"direction": "agent", "action": "created"}
```

The action comes from the event suffix (`created`, `updated`, or `resolved`).
Forward rows receive `{"direction": "user", "action": "created"}`. Existing
`content_text` is not rewritten. Invalid `metadata_json` is skipped instead of
aborting startup.

`assistant.page.updated` and `system.runtime.error` deliberately remain
`assistant`; removing the metadata exception intentionally removes them from
the transcript.

The downgrade does not depend on event metadata. It converts every
`annotation` row, including rows created after the upgrade, according to stored
authorship: `harness` to `harness`, `user` to `user`, and `agent` to
`assistant`. It removes `content.annotation` from all converted rows and
restores the prior input-index predicate.

## Deleted back doors

- Remove `include_metadata_sources` from `list_session_messages` and all
  production calls.
- Remove metadata-source exceptions from activity grouping and session-fork
  anchoring.
- Remove the obsolete `show.mark.*` backend translations.
- Derive message-type behavior from `vibe/message_types.json`; do not add
  hand-maintained vocabulary mirrors.

## Acceptance criteria

1. **Visibility is decided by type alone.** History and live streaming admit a
   row exactly when its catalog type is transcript-visible.
2. **One annotation is in one place.** While busy it is only queued; after
   drain it is only in the transcript.
3. **Chat bodies contain authored words only.** Selectors, event ids, file
   paths, pixel rectangles, tags, and CLI hints remain machine-only.
4. **The prompt is not degraded.** Immediate, queued, and retried dispatch
   replay the stored full prompt.
6. **Reservations converge honestly.** Startup leaves a stranded Show
   reservation pending and retryable until a real dispatch is accepted; it
   never creates an ownerless queue row or a receipt for an unrun turn.
7. **Live equals refetch.** A reverse mark publishes live and a refetch returns
   the same single row.

## Evidence

- **Unit:** catalog policy; target type by `(author, source, author_name)`;
  pure transcript/dispatch builders against frozen examples.
- **Contract:** production-argument transcript fetch; busy-session queue
  exclusivity; all dispatch paths; recovery with prompt preservation; live
  reverse-mark publication.
- **Migration:** upgrade and downgrade a copied database; every supported
  reverse-mark event and historical forward annotation converts, while
  post-upgrade rows downgrade by author; the pinned index predicate matches the
  catalog.
- **Static invariant:** no backend query, filter, or branch uses Show metadata
  to widen or narrow transcript visibility.
- **Manual integration:** deferred to the combined backend/frontend regression
  pass.

## Non-goals

- Deep-linking a card back to a Show Page anchor.
- Rendering `human.intent.submitted` as an annotation.
- Renaming `mark`, `unmark`, or Show Page URL parameters.
- Changing the `show_session_events` schema.
- Adding a `status` message type.
- Modifying frontend source in this lane.
