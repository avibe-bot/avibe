# Feishu/Lark: Concise Progress Mode Support

## Background

Avibe models mid-turn agent "progress" (thinking / tool calls / status) as a
single tri-state setting `agent_progress_style` = `concise | verbose | off`
(`config/v2_config.py:77, 435-438`).

- `off` — no process bubble (default).
- `concise` — ONE self-updating status bubble: short body line (`🔧 <action>`)
  plus a de-emphasized liveness footer (elapsed / step count), edited in place,
  finally rewritten into the answer.
- `verbose` — legacy append/split process log.

The concise machinery in the shared core is already fully platform-agnostic:
- `core/controller.py:919-948` resolves style + `uses_concise_status_bubble`.
- `core/message_dispatcher.py:340-370` `_concise_progress_style` gates on the
  per-platform capability `supports_status_bubble`; non-capable platforms are
  hard-capped to `verbose`.
- `_render_concise_status` (`:372-473`) + `begin_status_bubble` (`:475-521`)
  create/edit the bubble, passing `subtext=footer` to
  `im_client.edit_message` / `send_message`.

**Gap:** only Slack and Discord set `supports_status_bubble=True` AND render the
`subtext` footer. Feishu/Lark advertises neither, so it always falls back to
`verbose`.

Current Feishu state:
- `config/platform_registry.py:220-230` lark descriptor has no
  `supports_status_bubble` (→ False).
- `modules/im/feishu.py` `send_message` (:367-425), `_reply_message` (:427-461),
  `edit_message` (:634-689) accept `subtext` but **ignore** it.
- `_build_card_json` (:463-532) renders only the body markdown element (card
  schema 2.0), no footer element.

## Goal

Let a user who selects `agent_progress_style = concise` get the single
self-updating status bubble on Feishu/Lark, matching Slack/Discord semantics.
No core dispatcher changes — close the gap purely in the Feishu adapter + the
one capability flag.

## Reference: how Slack/Discord render the footer

- Slack: renders `subtext` as a native context block (small grey text)
  (`modules/im/slack.py:762-766`).
- Discord: appends `-# subtext` small text, handles footer-only empty body
  (`modules/im/discord.py:474-497`).
- Contract doc: `modules/im/base.py:325-348`.

Feishu card schema 2.0 has a `note` element that renders small grey secondary
text — the direct analog.

## Changes

### 1. Enable the capability
`config/platform_registry.py` lark descriptor: add
`supports_status_bubble=True`.

### 2. Render the footer in the card
`modules/im/feishu.py` `_build_card_json`: add a `subtext: Optional[str] = None`
parameter. When non-empty, append a `note` element after the body:

```python
if subtext:
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": subtext}],
    })
```

Buttons (if any) stay after/around per existing layout; footer note goes last.

### 3. Handle footer-only (empty body) bubble
`begin_status_bubble` posts an empty body + footer. Feishu `send_message`
currently raises on empty text (`if not text: raise ValueError`).
- Relax the guard: allow empty `text` when `subtext` is non-empty.
- In `_build_card_json`, only add the body markdown element when `text` is
  non-empty, so a footer-only card renders the note alone (no empty markdown
  element). Preserve existing behavior when both empty (still reject upstream).

### 4. Thread `subtext` through all send/edit paths
- `send_message` (:367): pass `subtext` into `_build_card_json`; on the
  thread-reply branch, pass `subtext` into `_reply_message`. While here, clean
  up the pre-built-then-discarded `body_builder`/`request` (:389-401) so the
  subtext-aware build isn't shadowed by a stale non-subtext build on the
  thread-reply branch.
- `_reply_message` (:427): add `subtext` param, forward to `_build_card_json`.
- `edit_message` (:634): pass `subtext` into `_build_card_json` (both the
  text-present and keyboard-only branches). No guard relaxation needed here —
  it already treats `text=""` as "build empty-text card", so the terminal
  collapse path (`message_dispatcher.py:887`, `edit_message(text="",
  subtext=marker)`) works once `_build_card_json` skips the empty body element.

### 4b. Thread `subtext` through the buttoned result path (FUNCTIONAL BLOCKER)
`core/message_dispatcher.py:1823-1854` (`_send_result_inline`) and
`_send_split_result_messages` (:1856-1908) call `send_message_with_buttons(...,
subtext=...)` whenever the final result has quick-reply buttons AND a done
footer (concise turns almost always have one, since `begin_status_bubble` runs
at turn start). Feishu's `send_message_with_buttons` (:534) has no `subtext`
param → raises `TypeError`, which the caller's try/except swallows by falling
back to plain `send_message` — silently DROPPING the quick-reply buttons.
Slack (`slack.py:1532-1540`) and Discord (`discord.py:529-537`) both accept it.

Fix: add `subtext: Optional[str] = None` to `send_message_with_buttons`, thread
it into the single `_build_card_json(text, button_rows)` call (:564), and apply
the same empty-text guard relaxation as `send_message`. `_reply_message_with_card`
needs no change (card JSON is built once before the thread/non-thread branch).

### 5. i18n / display text
Footer text is composed in core (`_compose_status_message`) from existing
i18n-safe pieces; no new user-facing strings in the Feishu adapter. Verify the
`note` content carries no hardcoded English added in the adapter.

## Turn-end: collapse, do NOT recall (keeps push, no tombstone)

Feishu message *recall* (`im.v1.message.delete`) works, but it leaves a visible
"此消息已撤回" tombstone in the thread — worse than the bubble it removes. And a
card *edit* fires no push, while the result must push. So on Feishu:

- lark does NOT declare `supports_message_deletion` (recall would tombstone).
- The result is delivered as a fresh, **pushing** message (unchanged).
- `_retire_status_bubble` (`message_dispatcher.py:891-911`) falls through to
  `_collapse_status_bubble`, which EDITS the bubble in place to a terminal
  marker (no recall → no tombstone).
- The collapsed marker always shows the run time: `✅ done · 1:30 · 240k tok`
  (`_status_footer_text(..., force_duration=True)`), independent of the
  `show_duration` config, because the residual bubble IS the turn's run summary.
  The result-message footer keeps `show_duration` gating so it doesn't
  double-surface the duration.

Net turn-end state on Feishu: a small grey `✅ done · <time> · <tok>` marker
(the former bubble, edited in place) + the pushing answer message. No tombstone.

## Out of scope / follow-up

- True single-message finalize (bubble edited into the full answer) would drop
  the residual marker but also the final push; deferred since push is wanted.
- Per-channel progress style override (core already notes it can layer later).

## Risks / breakage surface

- Card schema 2.0 `note` element must be valid for both `acreate` and `apatch`
  (edit) requests, and inside `reply_in_thread` cards.
- Empty-body guard relaxation must not let a truly empty (no text, no subtext)
  card through.
- Non-concise platforms and Feishu `verbose`/`off` paths must be untouched:
  when `subtext` is None (the normal non-concise send/edit), `_build_card_json`
  output is byte-for-byte identical to today.
- The thread-reply path (`_reply_message`) previously dropped buttons; only add
  `subtext`, do not change button behavior.

## Early de-risk (before full plumbing)

Feishu card schema 2.0 `note` element validity is the central design bet.
Before wiring all the Python paths, do ONE raw smoke call (`im/v1/messages`
create, msg_type `interactive`, schema 2.0 card with a single `note` element)
against a real chat to confirm the API accepts it. Card content schema is
identical across create/apatch/reply, so one create check covers all three
call sites. Fail fast here rather than during the Incus pass.

## Test / verification plan

- Unit: extend Feishu adapter tests (stub lark client) to assert
  `_build_card_json` includes the `note` element iff `subtext` is set, and that
  `subtext=None` produces the current card exactly; assert footer-only
  (empty text + subtext) builds a valid single-note card.
- Contract: assert `send_message`/`edit_message`/`send_message_with_buttons`
  forward `subtext` into the card content sent to the stubbed client.
- Buttons + concise: assert that concise mode + quick-reply buttons on the final
  result renders BOTH the buttons and the footer note (mirror the Discord case
  `tests/test_status_bubble_footer_render.py:89`
  `test_buttons_subtext_composes_footer_into_content`).
- Capability: assert `_concise_progress_style` returns `concise` for lark once
  the flag is set (dispatcher-level test if a pattern exists).
- Regression (Incus, Lark): with `agent_progress_style=concise`, verify the
  bubble lifecycle — turn-start footer-only bubble → in-place update on tool
  calls → terminal collapse to a footer-only marker (`✅ done`) PLUS a separate
  final-answer message (two messages; the bubble is NOT rewritten into the
  answer, see "Expected v1 behavior difference"); also verify in-thread.
- Lint: `ruff check` on changed Python files before push.
