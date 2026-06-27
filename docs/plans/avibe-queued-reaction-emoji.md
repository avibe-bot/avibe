# Queued-vs-Running Reaction Emoji for IM Turns

Status: APPROVED v2 (opus review + codex review round 2 = APPROVE; implementation constraints folded in)
Owner: qiqi
Scope: IM platforms (Slack / Discord / Telegram / Feishu). WeChat unaffected (no reaction support).

## 0. Changelog

- v4 (real root cause): the queued 👌 never appeared on Slack because
  `SlackBot.add_reaction` only mapped 👀→`eyes` / 🤖→`robot_face`; the raw 👌
  codepoint is rejected by Slack `reactions.add` with `invalid_name`. Fixed by
  mapping 👌→`ok_hand` (Slack, via a shared `_slack_reaction_name` helper) and
  👌→`OK` (Feishu emoji_type). With the platform mapping fixed, the v3 "always show
  👌" workaround was REVERTED back to the `gate.lock.locked()` guard: a non-queued
  message goes straight to 👀 (no 👌→👀 flash), only a genuinely-queued message
  shows 👌. The gate is held for the whole turn (released only on the terminal
  result), so `locked()` reliably detects real queuing.
- v3 (superseded by v4): dropped the `gate.lock.locked()` precondition in
  `AgentService.handle_message`. That single-instant check missed genuinely-queued
  messages — a queued message could show no 👌 at all (observed in testing). The
  gate now ALWAYS calls `show_queued_reaction` before `acquire()`; a non-contended
  turn flips 👌→👀 immediately (brief flash), a contended turn keeps 👌 for the
  whole wait. `promote_reaction_to_running` moved to the first action after the gate
  is acquired to minimize that flash. This supersedes goal #3 ("no 👌 flash on
  non-queued"): reliability of the queued indicator wins over avoiding a brief flash.
- v2: Fix P0 "👌 leaks on cancel-while-queued" (CancelledError escapes the gate try
  block); make reaction fully **gate-owned** with explicit handling of the concise
  status-bubble path and single-mode selection; sync `request` parallel fields on
  promote; correct Telegram `_normalize_reaction_emoji` wording; document subagent
  ordering and Slack reaction rate limits.

## 1. Background

When an IM user sends a message, the agent adds a 👀 reaction to acknowledge it is
processing. Today that reaction is added **too early** — before the turn has actually
started — so a message merely *waiting in line* already shows 👀, which reads as "the
agent is thinking about THIS message" when it is not.

### 1.1 Two independent concurrency mechanisms

- **Web Workbench (`platform="avibe"`)** — `core/session_turns.py`
  `SessionTurnManager` with a **persistent** queue (`messages_service` `QUEUED_TYPE`),
  `submit()` / `flush_queue()` / `in_flight`, and `queue.updated` browser events.
  Reactions do **not** apply (the browser has its own queue UI; the `avibe` IM client
  does not implement reactions). Out of scope.
- **IM (Slack/Discord/Telegram/Feishu/WeChat)** — `modules/agents/service.py`
  `AgentService.handle_message` acquires an **in-memory** `asyncio.Lock`
  (`_RuntimeTurnGate.lock`, keyed by `runtime_turn_key`). A second message for the
  same runtime key **blocks on `await gate.lock.acquire()`** (serial; no persistent
  queue, not merged, not dropped). This is the path the feature targets.

### 1.2 Current reaction lifecycle (the bug)

| Step | Location | Today |
| --- | --- | --- |
| Add reaction | `core/handlers/message_handler.py:390` `processing_indicator.start()` → `_start_reaction_indicator` | adds **👀** immediately, **before** the gate |
| Acquire gate | `modules/agents/service.py:46` `await gate.lock.acquire()` | a 2nd message blocks HERE |
| Turn truly starts | `modules/agents/service.py:68` `_begin_turn_status()` | status bubble posted here (correctly *after* the gate) |
| Remove reaction | `core/processing_indicator.py:427-441` `finish()` | removes `handle.ack_reaction_emoji` on terminal result/error |

The **status bubble** already waits for the gate; the **reaction** does not. The fix
makes the reaction follow the same "real start = gate acquired" rule.

### 1.3 Relevant constants / helpers (verified against code)

- `core/processing_indicator.py`
  - `ACK_REACTION_EMOJI = "👀"` (running)
  - `ProcessingIndicatorHandle.ack_reaction_message_id` / `.ack_reaction_emoji`
  - `start()` selects ONE mode in the normal path (`_processing_modes()` → first of
    `message`/`typing`/`reaction` that succeeds wins and returns), EXCEPT the
    **concise-status-bubble** branch (lines 199-210) which starts reaction **and**
    typing together and returns early.
  - `_start_reaction_indicator(handle)` adds the reaction; `_reaction_target_message_id`
    picks `platform_specific["processing_indicator_message_id"]` or `context.message_id`
  - `finish()` removes whatever emoji `handle.ack_reaction_emoji` holds (not hardcoded
    👀) and clears the matching `request.*` fields when a request is present.
  - `apply_to_request()` copies `handle.ack_reaction_message_id/.ack_reaction_emoji`
    onto the request; `handle_from_request()` prefers the live handle object's fields.
  - terminal-token registry: `track_turn` / `finish_terminal_turn` (keyed by turn token)
- `modules/agents/service.py`
  - `handle_message` gate chokepoint; the `try` block starts at line 51 — **AFTER**
    `await gate.lock.acquire()` (line 46). `except CancelledError` (line 71) therefore
    does NOT cover a cancellation raised *by* `acquire()` while queued. `_RuntimeTurnGate`,
    `_begin_turn_status`, `_track_processing_indicator_turn` (reads `request.processing_indicator`).
  - `runtime_turn_key`: `BaseAgent.runtime_turn_key` → `backend_composite_session_id or
    composite_session_id` (includes channel/session). Same channel → same gate → serial;
    different channel → independent. So 👌 semantics ("this channel is queued") is correct.
- `core/handlers/message_handler.py`
  - `start()` called at line 390 (only for `is_human`); subagent 🤖
    (`SUBAGENT_REACTION_EMOJI`) added at 392-405; error/cleanup near 484-500. The
    outer handler is `except Exception` (line ~491) — does NOT catch `CancelledError`.
- per-platform `add_reaction`/`remove_reaction`: `slack.py`, `discord.py`,
  `telegram.py` (`_normalize_reaction_emoji` is a **pass-through** with a small alias
  map `{👀, 🤖}`; it does NOT whitelist — Telegram's API server validates the emoji,
  and 👌 is in Telegram's supported reaction set, so it is accepted; a rejected emoji
  is swallowed and returns False), `feishu.py`, `wechat.py` (returns `False`), routed
  via `modules/im/multi.py`, default no-op in `modules/im/base.py`.

## 2. Goal

For IM platforms whose reaction indicator is the active mode:

1. A message **waiting behind a running turn** (blocked on the gate) shows **👌**
   (`:ok_hand:`) — "received, queued".
2. When that message's turn **actually starts** (gate acquired) the reaction switches
   **👌 → 👀**.
3. A message that runs **immediately** (gate free) shows **👀** directly — no 👌 flash.
4. On terminal result / error / **cancellation (including while still queued)**, the
   active reaction (👌 or 👀) is removed — no leaks.

Non-goals: Web Workbench queue; persistent IM queue; typing/ack-message modes; WeChat
reactions.

## 3. Design — gate-owned reaction (refined B1)

### 3.1 Principle

`AgentService.handle_message` is the single chokepoint that knows "queued vs running".
The **reaction** indicator is therefore **owned by the gate**, not by `start()`.
`start()` keeps owning the **typing / ack-message** modes (eager, immediate feedback);
it only *records* that the reaction indicator is the selected mode and defers the
actual add to the gate.

### 3.2 `core/processing_indicator.py`

- Add `QUEUED_REACTION_EMOJI = "👌"`.
- `ProcessingIndicatorHandle`: add `reaction_indicator_selected: bool = False`.
  (P1.2) It is NOT load-bearing in the OpenCode snapshot/restore path — restore only
  happens after `promote_reaction_to_running` has already set `ack_reaction_emoji=👀`,
  and `finish()` keys off that field directly — so omitting it from
  `to_snapshot`/`from_snapshot` is acceptable for this scope. Document the reason
  inline so a future reader doesn't add it cargo-cult.
- `_start_reaction_indicator(handle, *, emoji=ACK_REACTION_EMOJI)` — parametrize emoji.
- Change `start()` so the reaction mode is **selected but not added**:
  - Normal mode loop: when the candidate mode is `reaction` and `_mode_supported(...,
    "reaction", context)` is true, set `handle.reaction_indicator_selected = True` and
    `return handle` (preserves single-mode "first match wins"; does NOT call
    `_start_reaction_indicator`). `message`/`typing` cases stay eager as today.
  - Concise-status-bubble branch (lines 199-210): keep starting typing eagerly; for
    reaction, set `handle.reaction_indicator_selected = True` (do NOT add now).
- New service methods (best-effort, idempotent; both sync the request parallel fields):
  - `async def show_queued_reaction(self, request_or_handle) -> bool`
    - resolve handle; if `not handle.reaction_indicator_selected` → return False.
    - if `handle.ack_reaction_emoji` already set → return False (no double-add).
    - add `QUEUED_REACTION_EMOJI` via `_start_reaction_indicator(handle,
      emoji=QUEUED_REACTION_EMOJI)`; on success set handle fields, mirror onto
      `request.ack_reaction_message_id/.ack_reaction_emoji` (P1.1). Return success.
  - `async def promote_reaction_to_running(self, request_or_handle) -> None`
    - resolve handle; if `not handle.reaction_indicator_selected` → no-op.
    - **idempotency guard (P2.1): if `handle.ack_reaction_emoji == ACK_REACTION_EMOJI`
      → return** (already promoted; never double-add 👀).
    - if `handle.ack_reaction_emoji == QUEUED_REACTION_EMOJI`: `remove_reaction(👌)`
      then `add_reaction(👀)`; else (nothing shown yet) `add_reaction(👀)`.
    - update handle fields to 👀 and **mirror onto the request parallel fields** (P1.1).
- `finish()` unchanged: it already removes `handle.ack_reaction_emoji` (👌 or 👀) and
  clears `request.*`.

### 3.3 `modules/agents/service.py handle_message` (with P0.1 fix)

```python
agent = self.get(agent_name)
runtime_key = self._runtime_turn_key(agent, request)
gate = self._get_turn_gate(runtime_key)
was_busy = gate.lock.locked()
indicator = getattr(self.controller, "processing_indicator", None)
queued_shown = False
if was_busy and indicator is not None:
    try:
        queued_shown = bool(await indicator.show_queued_reaction(request))
    except Exception:
        logger.debug("show_queued_reaction failed", exc_info=True)
try:
    await gate.lock.acquire()
except BaseException:
    # P0.1: cancellation (e.g. SIGTERM) while still queued escapes here, OUTSIDE
    # the main try below. Clean up the queued 👌 (+ any eager typing) so it doesn't
    # leak permanently on the user's message.
    if queued_shown and indicator is not None:
        try:
            await indicator.finish(getattr(request, "processing_indicator", None) or request)
        except Exception:
            logger.debug("queued reaction cleanup on cancel failed", exc_info=True)
    raise
gate.token = uuid.uuid4().hex
gate.backend = agent.name
gate.runtime_started = False
self._stamp_runtime_turn(request, runtime_key, gate.token)
try:
    manager = getattr(self.controller, "session_turns", None)
    if manager is not None:
        manager.on_running(request.context)
    await self._begin_turn_status(request.context)
    if indicator is not None:
        try:
            await indicator.promote_reaction_to_running(request)
        except Exception:
            logger.debug("promote_reaction_to_running failed", exc_info=True)
    self._track_processing_indicator_turn(request)
    await agent.handle_message(request)
except asyncio.CancelledError:
    ...  # existing
```

Notes:
- `was_busy = gate.lock.locked()` is synchronous; the only await before `acquire()` is
  the `show_queued_reaction` call. If the holder releases during it, `acquire()` returns
  immediately and `promote_reaction_to_running` swaps 👌 → 👀 (brief 👌). Acceptable.
- Non-busy path: no 👌; `promote_reaction_to_running` adds 👀 directly (goal #3).
- `finish()` on the cancel path also stops the eager typing keepalive — correct full
  cleanup for a never-run queued message; idempotent with any outer cleanup.

### 3.4 Cleanup matrix (verified)

| Exit path | Mechanism | 👌/👀 removed? |
| --- | --- | --- |
| Normal terminal result | `finish_terminal_turn`/agent `_remove_ack_reaction` → `finish()` (handle is 👀 after promote, tracked after promote) | yes |
| Backend exception in turn | `service.py except Exception` emits silent result → outbound cleanup; handle reachable via token (stamped pre-exception) | yes |
| Cancel **while running** | `service.py except CancelledError` → `_tidy_on_cancel` emit | yes |
| Cancel **while queued** (P0.1) | new `except BaseException` around `acquire()` → `indicator.finish(...)` | yes (FIXED) |
| `request` unbound before build (`message_handler` `except NameError`, ~500) | falls back to `finish(processing_indicator)`; handle object carries fields | yes |

### 3.5 Subagent coexistence (P2.2)

🤖 is added in `message_handler` (392-405) *before* the gate; 👌/👀 are added at the
gate. So a queued subagent message shows 🤖 first, then 👌 once it reaches the gate,
then 👀 on promote — both reactions coexist; `promote_reaction_to_running` only touches
the ack reaction. The add order (🤖 then 👌) is accepted; platforms do not guarantee
reaction display order anyway.

## 4. File-by-file change list

1. `core/processing_indicator.py` — `QUEUED_REACTION_EMOJI`; handle field
   `reaction_indicator_selected`; parametrized `_start_reaction_indicator`;
   `start()` selects-but-defers reaction (normal + concise paths);
   `show_queued_reaction()` / `promote_reaction_to_running()` (both sync request fields).
2. `modules/agents/service.py` — `handle_message`: `was_busy` peek +
   `show_queued_reaction` before acquire + `except BaseException` cleanup around
   acquire (P0.1) + `promote_reaction_to_running` after `_begin_turn_status` (all guarded).
3. `core/handlers/message_handler.py` — no functional add/remove change at 390 (reaction
   no longer added there because `start()` defers it); confirm nothing else relied on
   `start()` having added the reaction synchronously.
4. No IM adapter changes. No i18n (emojis are not localized text).

## 5. Test plan

- `tests/test_processing_indicator_reaction.py` (new):
  - reaction selected + not busy → `promote` adds 👀 once; `finish` removes 👀.
  - reaction selected + busy → `show_queued_reaction` adds 👌; `promote` removes 👌 +
    adds 👀; `finish` removes 👀. Assert request parallel fields synced after each step.
  - typing/message mode (reaction not selected) → `show_queued`/`promote` are no-ops
    (no `add_reaction` calls).
  - WeChat `add_reaction` returns False → `show_queued_reaction` returns False, handle
    stays clean, no crash.
- `tests/test_agent_service.py` / `tests/test_runtime_service_lock.py`:
  - 2nd message with gate held → `show_queued_reaction(👌)` before `acquire()`;
    `promote` after the 1st releases.
  - **cancel while queued** → `acquire()` raises CancelledError → `finish()` called →
    👌 removed (P0.1 regression test).
- `tests/test_message_handler_typing.py`: the eager-👀 assertions (around lines
  285-297, 592-594, 690-691, 939) must now assert `controller.im_client.reactions == []`
  AND `request.ack_reaction_emoji is None` / `request.ack_reaction_message_id is None`
  immediately after `handle_user_message` (reaction is selected-but-deferred, the stub
  agent service never promotes). The promote/queued add/remove assertions move into the
  new `tests/test_processing_indicator_reaction.py`.
- `tests/test_status_bubble_indicator.py`: update any assertion that the concise path
  eagerly adds the reaction (now deferred to the gate).
- Scenario: inspect `tests/scenarios/message_delivery/`; extend or add a
  queued→running reaction scenario if a matching shape exists; surface the scenario ID
  in the test + PR description.

## 6. Breakage risks

- Tests asserting "reaction added at message arrival" must be updated
  (`test_message_handler_typing.py`, `test_status_bubble_indicator.py`). Expected.
- Reaction now appears at the gate rather than at 390. For a non-busy turn the gate is
  acquired ~immediately, so the latency delta is one event-loop hop; for messages with
  attachments the reaction already followed file processing only loosely — confirm the
  perceived latency is acceptable (typing indicator, when active, still fires at 390).
- Slack reaction rate limit (~1/s per conversation): the promote does remove+add (2
  calls) only for genuinely queued messages; non-queued messages do a single add.
  A queued **subagent** message is the worst case — 🤖 add, 👌 add, 👌 remove, 👀 add
  = 4 sequential reaction calls (P2.3). Acceptable; note for high-frequency bursts.
- Telegram: 👌 is accepted by the API (no code change needed); a future Telegram emoji
  rejection is swallowed → no reaction, no crash.
- P0.1 cancel-while-queued cleanup is the key correctness item; covered by a dedicated
  regression test.

## 7. Rollout

- Worktree from `alex/master`.
- Implement; focused pytest (processing_indicator + agent_service + runtime lock +
  message_handler typing); `ruff check` on changed files.
- Optional Slack Incus regression to eyeball 👌 → 👀 → cleared, and queue cancel.
- PR; codex review + simplify loop; keep CI gates; PR body names the capability +
  scenario IDs + evidence layers.

## 8. Resolved decisions

1. Reaction is **gate-owned (refined B1)**: non-busy → 👀 directly; busy → 👌 then 👀.
2. `start()` selects-but-defers the reaction; typing/message stay eager. Concise path
   defers the reaction too.
3. Emoji: 👌 `:ok_hand:` (user-selected; not the hourglass used elsewhere).
4. P0.1 fixed via `except BaseException` around `gate.lock.acquire()`.
5. request parallel fields synced in both new methods (P1.1).
6. Scenario catalog: to be confirmed against `tests/scenarios/message_delivery/` during
   implementation.
