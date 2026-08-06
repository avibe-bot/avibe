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

Platforms without reaction support stay silent: one bubble per queued message is
worse than no receipt.

### 3.3 Platform emoji mappings

Reaction emoji are not portable. Slack needs ASCII short names, Telegram accepts
a fixed set (and lists the writing hand without U+FE0F), Lark needs `emoji_type`
keys.

- `slack.py`: `writing_hand`, `thinking_face`, `shrug` added.
- `telegram.py`: `✍️ → ✍` normalization; 👌/🤔/🤷 pass through.
- `discord.py`: raw unicode, no change.
- `feishu.py`: 🤔 already maps to `THINKING`. ✍️ and 🤷 are deliberately left
  unmapped rather than guessed — `add_reaction` logs the exact "add it to
  `_EMOJI_MAP`" warning and returns `False`, so Lark degrades to no receipt for
  those two until the `emoji_type` keys are verified against the Lark table.

## 4. Known limitations

- A `reconciling_steer` receipt (🤔) is not upgraded when reconciliation later
  settles the Delivery; the outcome shows up in the turn's reply instead.
- Receipts are best-effort and in-memory: after a restart a stale 👌 is not
  cleared by `start()`. On Telegram the reaction is replaced anyway; on Slack it
  can linger until the next turn on that message.
- The ✍️ receipt is intentionally permanent. It records which turn absorbed the
  message, which stays useful after the reply arrives.

## 5. Evidence

- contract/unit: `tests/test_delivery_ack_reaction.py`
  (MESSAGE-DELIVERY-301, MESSAGE-DELIVERY-302)
- regression: `tests/test_processing_indicator_reaction.py`,
  `tests/test_session_delivery_fsm.py`, `tests/scenarios/message_delivery/`
- manual: send a second and third message on Telegram/Slack while a turn runs;
  confirm ✍️ for a steered input, 👌 → 👀 for a queued one.
