# Show Page annotations: a harness message, settled like a chat send

## Background

A Show Page annotation currently reaches chat through machinery no other
turn-input uses.

Two distinct oddities, one origin.

### 1. It renders as if the user typed it

`ShowSessionEventStore.append` writes the transcript row with
`author="user"` and no `source`, so `messages_service.append` resolves it to
`type="user"` (`storage/messages_service.py:362`). Chat renders it as the
right-aligned user bubble — indistinguishable from something the user typed
into the composer.

It is not that. It is turn input the user did not type, produced by another
surface — exactly what a scheduled task, a watch, a webhook, and an agent
callback are. Those already have a home: `source="harness"`, a trigger kind in
`author_name`, and a collapsed row with a labelled chip
(`ui/src/lib/chatTrigger.ts`, `ChatPage.tsx:3223`).

### 2. It carries a private dispatch state machine

`show_session_events.dispatch_state` holds a five-state lifecycle
(`none | in_flight | accepted | failed | archived`) plus an owner identity
(pid + process start + attempt id), a 60 s claim TTL, liveness probing of the
claiming process, and a startup sweep. Around it:

| File | refs |
| --- | --- |
| `core/show_session_events.py` | 63 |
| `tests/test_show_session_events.py` | 33 |
| `core/internal_server.py` | 21 |
| `vibe/ui_server.py` | 15 |
| `vibe/cli.py` | 9 |
| `tests/test_internal_server.py` | 8 |
| `tests/test_show_pages.py` | 6 |
| `tests/test_sqlite_state_migration.py` | 5 |
| `storage/alembic/versions/20260726_0035_show_event_dispatch_state.py` | 5 |
| `tests/test_ui_show_pages.py` | 2 |
| `storage/models.py` | 1 |

168 references. A chat send does the same job with none of them.

## Why the machine exists, and why it no longer needs to

A persisted lifecycle is how you answer "did the dispatch land?" **when the
answer cannot be returned in the response**. That was true when the annotation
POST fired the dispatch into a background task. It is no longer true:

- HTTP: `_show_event_response_from_payload` **awaits** the dispatch
  (`vibe/ui_server.py:9143`) and returns 202 `dispatch_pending` or 502 from the
  outcome.
- CLI: `record_local_show_event(..., dispatch_sync=True)`
  (`vibe/cli.py:11209`) runs it through `asyncio.run` and raises on failure.
- The fire-and-forget branch — `_dispatch_show_event_if_requested`
  (`vibe/ui_server.py:9211`) — has **no production caller left**. The CLI is the
  only caller of `record_local_show_event`, it passes `dispatch_sync=True`, and
  it runs with no event loop, so it returns from the sync branch above. Only
  tests reach the async branch, and they reach it by monkeypatching.

Two further jobs people assume the machine does, which it does not:

- **Replay safety.** That comes from `insert().prefix_with("OR IGNORE")` plus
  the `rowcount == 0` early return (`core/show_session_events.py:813-839`),
  inside one transaction. A replayed event id returns the stored row and never
  reaches dispatch — with or without a claim.
- **Archive safety.** `workbench_sessions_service` already clears pending rows
  on archive (`storage/workbench_sessions_service.py:989`), and the dispatch
  path re-checks archive state.

What the machine genuinely covers, and what must survive the deletion:

- **A crash between reserving the transcript row and settling it** leaves an
  invisible `pending` row. The startup sweep repairs it today — for Show events
  only. Chat sends have the identical exposure and no repair at all
  (`ui_server.py:7702` reserves; only the success/failure paths promote).

## Target

**One rule: a reserved transcript row is repaired by type, not by a per-event
lifecycle.** Replace the whole machine with a startup sweep that promotes every
stranded `pending` row to its visible type, whatever wrote it. That is strictly
more coverage than today (chat gains a repair it never had) in an order of
magnitude less code.

### Contract A — the annotation is a harness message

In `ShowSessionEventStore.append`, a human Show event that requests dispatch
writes its transcript row as harness input:

```python
source="harness"
author_name=SHOW_TRIGGER_KIND[event_type]   # closed map, see below
author_id=event_id
```

`SHOW_TRIGGER_KIND` is a closed map over the two event types that
`show_event_requests_dispatch` accepts:

| event type | trigger kind | zh label | en label |
| --- | --- | --- | --- |
| `human.annotation.created` | `show_annotation` | 页面批注 | Page annotation |
| `human.intent.submitted` | `show_intent` | 页面操作 | Page action |

Both are the same mechanism and must not be split: classifying one and leaving
the other as a fake user bubble is the defect, restated.

Reserved rows keep `message_type=PENDING_TYPE`; the promote target becomes
`HARNESS_TYPE` instead of `"user"`. `HARNESS_TYPE` is already in
`TRANSCRIPT_TYPES`, so transcript visibility is unchanged.

Assistant/system marks (`actor in {"assistant", "system"}`) are **not** touched
— they stay agent-authored rows.

Frontend:

- `ui/src/lib/chatTrigger.ts` — `harnessChipLabelKey` returns
  `chat.source.showAnnotation` / `chat.source.showIntent` for the two kinds.
  `chatTriggerLink` returns `null` for both (non-navigating), matching
  `webhook`. Deep-linking a chip back to the Show Page is a separate change and
  is **out of scope**.
- `ui/src/i18n/{zh,en}.json` — the four strings above under `chat.source`.

### Contract B — dispatch settles in the response, and the row is repaired by type

1. Delete `show_session_events.dispatch_state` (column + migration) and every
   symbol built on it: the five state constants, `SHOW_DISPATCH_CLAIM_TTL_SECONDS`,
   `_ACTIVE_SHOW_DISPATCH_ATTEMPTS`, `ShowDispatchStatus`,
   `ShowDispatchSettlement`, `_ShowDispatchOwnerIdentity`,
   `show_dispatch_attempt`, `claim_show_dispatch`, `settle_show_dispatch`,
   `observe_and_settle_show_dispatch`, `reconcile_show_dispatch_settlement`,
   `get_show_dispatch_status`, `ShowSessionEventStore.get_dispatch_status`,
   `reconcile_dispatch_settlement`, `reconcile_dispatch_messages`.
2. Delete `_dispatch_show_event_if_requested` and the `dispatch_sync=False`
   branch of `record_local_show_event`. Local recording dispatches
   synchronously, full stop.
3. `_run_show_event_dispatch` keeps its shape — reserve, dispatch, promote,
   return an outcome — and drops `dispatch_owner` from the dispatch payload and
   from the internal endpoint's contract.
4. `core/internal_server.py` drops the `show_event_id` state re-derivation
   branch. It keeps the `PENDING → QUEUED_TYPE` promotion when the session is
   busy (`core/session_turns.py:105`), which is orthogonal.
5. New startup sweep, origin-agnostic: promote every `pending` message row to
   its visible type. Show/harness rows → `HARNESS_TYPE`; chat rows →
   `"user"`. Derive the target from the row's own `author`/`source`, so the
   sweep needs no join to `show_session_events`.

The 202 `dispatch_pending` / 502 responses stay exactly as they are — they are
the reporting surface that replaces the state machine, and the SDK already
throws on `!response.ok` (`packages/sdk/src/index.ts:468,586`).

## Non-goals

- Renaming `mark` / `unmark` or any Show Page URL parameter.
- Touching host-page routing, parameters, or behaviour.
- Deep-linking the new chip back to the Show Page.
- Any release tag.

## Evidence layers

- **unit** — trigger-kind map is a closed pure map; the pending-sweep target
  resolver is pure and tested per origin.
- **contract** — `tests/test_show_session_events.py` and
  `tests/test_internal_server.py` rewritten against the new shape: a replayed
  event id does not re-dispatch; a failed dispatch promotes the row and answers
  502; an archived session rejects; a stranded `pending` row from **either**
  origin is repaired by the sweep.
- **frontend** — `chatTrigger` label-key branch per kind; `npm run build`.
- **manual** — Incus regression: annotate a page, confirm the chat row is the
  collapsed harness row titled 页面批注, and that the agent still answers it.
