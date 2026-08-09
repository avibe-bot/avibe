# Admission Receipts for Mid-Turn IM Input

Status: implemented
Scope: IM platforms with reaction support (Slack / Discord / Telegram / Lark).
WeChat is unaffected (no reactions).

## 1. Background

A user sends a message, the agent answers with 👀, and everything reads fine —
until the user sends a *second* message while that turn is still running. That
message gets no reaction, no typing indicator, no bubble. The user cannot tell
whether it was folded into the running turn, queued behind it, or dropped.

The 👌 "queued" receipt from [`avibe-queued-reaction-emoji.md`](avibe-queued-reaction-emoji.md)
does not cover this. That design is owned by the in-memory runtime gate in
`AgentService.handle_message`, which is only reached once a message is actually
dispatched. Durable Delivery admission now runs earlier:

```python
# core/handlers/message_handler.py
if admitted:
    return None                        # ← mid-turn input stops here
...
if is_human:
    processing_indicator = await ...start(context, agent_name)
```

`_admit_human_delivery` returns `True` for every input it hands to its durable
owner, so the indicator is only ever started by the *dispatch* that follows a
claim. That leaves two silent cases:

| Input | Delivery path | Feedback before this change |
| --- | --- | --- |
| First message, idle session | claimed → dispatched → `start()` | 👀 |
| Steered into a running turn | materialized as `accepted`, never dispatched | none |
| Queued behind a running turn | `queued` until the turn ends, then dispatched | none until it starts |

## 2. Goal

Every IM input gets an immediate, honest receipt of what happened to it, using
the state its durable owner actually recorded — not a guess made before
admission.

## 3. Design

### 3.1 Distinguish started from steered

`accepted` is the terminal state for both a Delivery that started its own Turn
and one that was steered into a Turn already running, so the state alone cannot
drive the receipt. `DeliveryResult` gains `admission: "" | "started" | "steered"`:

- `_committed_delivery_result` reports `started` for an owned state, because
  every caller reaches it right after dispatching a Turn this Delivery joins.
- `_finish_steer` reports `steered` on its acceptance branch.

A `started` input is left alone: its Turn already owns the 👀 indicator.

### 3.2 Report the outcome as a reaction

`ProcessingIndicatorService.ack_delivery_state(context, state=, admission=)`
maps the durable state to one receipt on the sender's own message:

| Delivery state | Receipt | Meaning |
| --- | --- | --- |
| `accepted` (steered) | ✍️ | inserted into the running turn |
| `queued`, `pending_steer` | 👌 | queued; runs when the current turn ends |
| `reconciling_steer` | 🤔 | sent, acknowledgement unconfirmed |
| `retired` | 🤷 | not delivered |
| `claimed`, `steering`, `interrupt_waiting` | — | turn-owned; the indicator answers |

The service keeps a small registry keyed by platform/channel/message so a
receipt can be replaced rather than stacked, and `start()` clears the receipt
before the turn's own indicator takes over — a promoted Delivery is
re-dispatched with its original native message id (`_hydrate_delivery_context`),
so 👌 and 👀 would otherwise land on the same message.

Only *replaceable* states (`queued`, `pending_steer`, `reconciling_steer`) are
remembered. A final receipt (✍️ / 🤷) is never cleared, so keeping its key would
leak one entry per message for the life of the process; a FIFO cap
(`_ADMISSION_ACK_REGISTRY_LIMIT`) backstops the replaceable ones.

A receipt and the turn start it races are serialized per message key by a
refcounted `asyncio.Lock`, and `clear_admission_ack` writes an
`_ADMISSION_ACK_CONSUMED` marker *before* awaiting the removal. Together those
cover both orderings: a clear waits for an in-flight add and then removes it,
and an add that lands after the turn started is suppressed instead of leaving an
orphan 👌 next to 👀.

Platforms without reaction support stay silent: one bubble per queued message is
worse than no receipt.

### 3.4 Carrying the receipt target across hydration

The reaction target is not always the sender's own message — a quick reply
reacts on the bot echo it was attached to — and it cannot be rebuilt from the
Delivery snapshot. The ingress handler stores it in the durable
`admission_context` (`processing_indicator_message_id`), and every consumer
reads it back through `_delivery_ack_target`:

- `MessageHandler._restore_reaction_target` when the durable owner re-dispatches
  a hydrated context;
- `_delivery_receipt_context` when a Delivery settles away from ingress. A
  quick-reply callback is dispatched with `message_id=None` (to bypass platform
  event dedup), so a persisted echo target — not a native message id — is what
  qualifies it for a receipt;
- `_hydrate_delivery_batch_context`, which publishes `delivery_ack_targets` for
  the whole merged batch. Deliveries without a native message id merge into one
  Turn and only the first hydrates the dispatch context, so `start()` clears
  every target in the batch, not just its own.

### 3.3 Platform emoji mappings

Reaction emoji are not portable. Slack needs ASCII short names, Telegram accepts
a fixed set (and lists the writing hand without U+FE0F), Lark needs `emoji_type`
keys.

- `slack.py`: `writing_hand`, `thinking_face`, `shrug` added.
- `telegram.py`: `✍️ → ✍` normalization; 👌/🤔/🤷 pass through.
- `discord.py`: raw unicode, no change.
- `feishu.py`: 🤔 already maps to `THINKING`; ✍️ → `Typing` and 🤷 → `Shrug`,
  both verified against the Lark `emoji_type` table (Lark has no writing-hand
  glyph, so `Typing` — the pen/being-written-into icon — carries the steered
  receipt). Both `✍️` and `✍` are listed because `_normalize_emoji` does not
  strip U+FE0F. `emoji_type` keys are case-sensitive: an unmapped emoji stays
  non-ASCII, so `add_reaction` logs the "add it to `_EMOJI_MAP`" warning.

### 3.5 Reporting outcomes that settle away from ingress

The ingress caller only sees the admission result. Two paths resolve a Delivery
afterwards and are therefore the only possible reporters of its real outcome:

- `_run_pending_steers` — a Delivery admitted as `pending_steer` can be accepted
  (👌 → ✍️), definitively refused after the Session went inactive and retired
  (👌 → 🤷), or left unconfirmed (👌 → 🤔). Every row of an attempt settles
  together, so the leader's result is reported for the whole claimed batch.
- recovery — `recover_durable_delivery_state` and `_resume_delivery_observation`
  re-enter `deliver()` for a reservation committed before the service stopped.
  The ingress handler is long gone, so recovery reports that result itself.

Both go through `_report_delivery_receipts`, which is best-effort: a failure to
react is logged and never blocks the Delivery.

## 4. Known limitations

- A `reconciling_steer` receipt (🤔) is upgraded only when reconciliation settles
  through the pending-steer path; other reconciliation outcomes still show up in
  the turn's reply instead.
- Receipts are best-effort and in-memory: after a restart a stale 👌 is not
  cleared by `start()`. On Telegram the reaction is replaced anyway; on Slack it
  can linger until the next turn on that message.
- The ✍️ receipt is intentionally permanent. It records which turn absorbed the
  message, which stays useful after the reply arrives.

## 5. Evidence

- contract/unit: `tests/test_delivery_ack_reaction.py`
  (MESSAGE-DELIVERY-301, 302, 304, 305, 309) and
  `tests/test_session_delivery_fsm.py`
  (MESSAGE-DELIVERY-303, 306, 307, 308, 309)
- regression: `tests/test_processing_indicator_reaction.py`,
  `tests/test_session_delivery_fsm.py`, `tests/scenarios/message_delivery/`
- manual: send a second and third message on Telegram/Slack while a turn runs;
  confirm ✍️ for a steered input, 👌 → 👀 for a queued one.
