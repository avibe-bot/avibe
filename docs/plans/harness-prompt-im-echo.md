# Harness prompt echo to IM

## Background

A Harness turn (scheduled task, watch, webhook, hook, `vibe agent run`) carries a
stored prompt. That prompt is persisted as a `harness` Message row —
`core/message_mirror.mirror_harness_inbound` on the legacy path, the
`message_deliveries.message_snapshot(...)` write on the durable Delivery path — and
Workbench Chat renders it, so the Web transcript reads *trigger → result*.

An IM conversation had no equivalent. `mirror_harness_inbound` only publishes live
for `platform == "avibe"`, and no outbound send existed for the prompt, so Slack /
Discord / Telegram / Lark / WeChat only ever received the agent's reply: an answer
to a question nobody in the channel could see. A daily digest, a watch firing, or
another agent's `vibe agent run` all arrived as unexplained messages.

## Goal

When a background task triggers an agent turn whose session is bound to an IM
conversation, post the triggering prompt to that same conversation, once, just
before the turn starts — so the channel reads *trigger → work → result*.

Non-goals:

- changing which message the scheduled thread anchors on (still the **result**, via
  `SessionHandler.finalize_scheduled_delivery`);
- persisting a second Message row (the `harness` row already exists);
- a Web UI toggle (the switch is config-only, like the other `harness_*` runtime
  knobs).

## Solution

One platform-agnostic call in the shared turn pipeline, one outbound helper in the
dispatcher.

1. `core/handlers/message_handler.py::_handle_turn` — for `source != human`, calls
   `_echo_harness_prompt(context, message)`. Placement is the design:
   - **after** every `suppress_delivery` resolution (the `agent_run_target` and the
     `find_session_for_anchor` branches, both settled only after
     `get_session_info`), so a backgrounded session stays silent;
   - a queued Harness turn still cannot announce itself early: its durable admission
     happens upstream in `ScheduledTaskService`, so `_handle_turn` is entered only
     when the turn actually runs;
   - **before** `_prepend_message_metadata` and dispatch, so the echo is the raw
     prompt and lands ahead of the status bubble and the result;
   - outside the mirror `if`, so the durable Delivery path is covered too.
   The helper is best-effort and resolves the dispatcher through `getattr`.
2. `core/message_dispatcher.py::emit_harness_prompt` — owns the gates and the send.
   Deliberately **not** routed through `emit_agent_message`: this is neither agent
   output nor a lifecycle event, and must not consolidate into the status bubble,
   settle a turn, touch an `agent_runs` row, or persist a row.
   - skipped for `avibe` (Workbench renders the row), `suppress_delivery`, a
     trigger kind outside `HARNESS_PROMPT_ECHO_TRIGGER_KINDS`
     (`HARNESS_TRIGGER_KINDS` minus `activity_recovery`, which is a runtime
     re-injection, not a user instruction), and `harness_prompt_echo = false`;
   - target is `_get_target_context(context)`, so the question lands where the
     answer lands (`post_to` / deliver-key overrides included);
   - body: an i18n label per trigger kind, optionally `label · name`, then the
     prompt with every line `> `-quoted (readable on plain-text platforms too),
     truncated at `HARNESS_PROMPT_ECHO_MAX_CHARS = 800`;
   - dedupe: bounded per-process FIFO keyed on target + native/delivery id
     (`HARNESS_PROMPT_ECHO_MEMORY = 256`), so an in-process re-dispatch of one
     delivery cannot read as the task having fired twice. A cross-restart replay is
     not covered on purpose: it writes a new Delivery anyway, a duplicate echo is
     cosmetic, and a missing echo defeats the feature.
3. `core/scheduled_tasks.py::_build_context` stamps `task_definition_name` next to
   the existing `task_definition_id`, resolved through
   `_definition_display_name` (task row, else watch definition, best-effort), so
   the label names the task instead of an id. `agent_run` has no definition.
4. Config: `V2Config.runtime.harness_prompt_echo` (default `true`), mirrored onto
   `AppCompatConfig` + `to_app_config` (the turn path reads `controller.config`,
   which is the compat object) and hot-reloaded in
   `Controller._refresh_config_from_disk`. No UI, matching `harness_run_*`.
5. Copy: `harness.promptEcho.*` in `vibe/i18n/en.json` and `zh.json`.

## Evidence

- unit — `tests/test_message_dispatcher_scheduled.py::HarnessPromptEchoTests`
  (per-kind coverage, every gate, delivery override, dedupe, memory bound,
  truncation/quoting, silent-only prompt, send failure);
  `tests/test_message_handler_harness_echo.py` (pipeline wiring: echo-before-dispatch
  ordering, raw prompt, human turn silent, backgrounded thread resolution visible to
  the echo, missing hook and failing echo never block the turn);
  `tests/test_scheduled_tasks.py::test_build_context_carries_the_definition_name_for_display`
  and `::test_build_context_survives_a_store_that_cannot_name_the_definition`.
- scenario — `MESSAGE-DELIVERY-018` (prompt precedes result; the result still owns
  the anchor) and `MESSAGE-DELIVERY-019` (background-visibility turn echoes nothing)
  in `tests/scenarios/message_delivery/`.
- manual — a real scheduled task in one IM channel per platform, plus a
  `vibe agent run` callback, to confirm ordering and formatting against live
  renderers (WeChat has no markdown).

## Todo

- [x] turn-pipeline call site and dispatcher helper
- [x] definition name in the harness context
- [x] runtime switch through V2 config, compat config, and hot reload
- [x] en/zh copy
- [x] unit + scenario coverage, catalog entries
- [ ] manual IM pass in the Incus regression environment
