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

Stage in the shared turn pipeline, send at the real turn start, gate and format in
the dispatcher.

1. `core/handlers/message_handler.py::_handle_turn` — for `source != human`, calls
   `_stage_harness_prompt_echo(context, control_message)`, which stamps
   `HARNESS_PROMPT_ECHO_SPEC_KEY` (`core/message_output.py`) into
   `platform_specific`. Placement is the design:
   - **after** every `suppress_delivery` resolution (the `agent_run_target` and the
     `find_session_for_anchor` branches, both settled only after
     `get_session_info`), so a backgrounded session stays silent;
   - **before** `_prepend_message_metadata`, so the staged text is the prompt, not the
     decorated backend dispatch text;
   - staging `control_message`, the pre-routing prompt: subagent routing rewrites
     `message` to the prefix-stripped body, and the echo must match what the
     Workbench row shows, prefix included;
   - outside the mirror `if`, so the durable Delivery path is covered too.
2. `modules/agents/service.py::_begin_turn_status` — `_emit_staged_harness_prompt`
   pops the staged key and sends it, next to the existing turn-start hooks. The send
   belongs *here*, not in the pipeline: `AgentService.handle_message` blocks on the
   runtime turn gate first, so a pipeline-side send would show a queued task's prompt
   while another turn is still working — or leave a prompt behind for a turn that was
   cancelled on the gate. Emitted before `begin_status_bubble` so the channel still
   reads trigger -> work -> result. Best-effort, `getattr`-guarded, and bounded at
   `HARNESS_PROMPT_ECHO_TIMEOUT_SECONDS = 5`: this await holds the runtime gate, and an
   adapter's own request budget is much longer than a turn start may wait (Telegram
   allows 60s), so an unbounded send would let a degraded transport delay the
   background task itself plus every turn queued behind it. Same bound, same reason as
   the `begin_status_bubble` post right after. The staged text is popped first, so a
   timeout drops that one echo instead of deferring it.
3. `core/message_dispatcher.py::emit_harness_prompt` — owns the gates and the send.
   Deliberately **not** routed through `emit_agent_message`: this is neither agent
   output nor a lifecycle event, and must not consolidate into the status bubble,
   settle a turn, touch an `agent_runs` row, or persist a row.
   - skipped for `avibe` (Workbench renders the row), `suppress_delivery`, a
     trigger kind outside `HARNESS_PROMPT_ECHO_TRIGGER_KINDS`
     (`HARNESS_TRIGGER_KINDS` minus `activity_recovery`, which is a runtime
     re-injection, not a user instruction), and `harness_prompt_echo = false`;
   - target is `_get_target_context(context)`, so the question lands where the
     answer lands (`post_to` / deliver-key overrides included);
   - text depends on who wrote the prompt:
     - `watch` / `webhook` / `hook` compose it **for the agent** — a watch appends the
       waiter's raw stdout (`core/watches.py::_build_prompt`), an `--on-failure agent`
       escalation appends the generated failure report
       (`core/scheduled_tasks.py::_escalation_prompt`, "read by an AGENT, not shown to
       a user"), a webhook its payload. Publishing that into a shared channel would
       leak command output (tokens, stack traces) before the agent can redact it, so
       for `HARNESS_PROMPT_ECHO_INSTRUCTION_ONLY_KINDS` the echo shows **only** the
       definition's stored instruction (`harness_display_prompt`) and stays silent
       when none resolves. The Workbench transcript still renders the full prompt for
       the operator;
     - otherwise the Delivery display snapshots when present, else the staged prompt:
       `SessionTurnGate` prepends internal instructions to `dispatch_text` (the
       `[Avibe recovery: ...]` guard on an ambiguous-start replay) that must stay
       backend-only. *All* snapshots, de-duplicated: a busy session merges the queued
       deliveries of one definition into a single Turn
       (`_collect_delivery_segment`) and dispatches every prompt
       (`_segment_dispatch_text`), so the singular first snapshot would announce one
       instruction for a result answering several — two `vibe agent run` calls merge
       under the shared `agent_run` definition id. Repeat firings of one scheduled task
       carry the same stored prompt, hence the de-dup. The instruction-only kinds are
       unaffected: a merged batch shares its definition, so its stored instruction is
       already single;
   - mentions are neutralized in the whole body, label included: quoting does not stop
     a renderer from resolving `@everyone` / `<@U…>` / `<@&role>` / `<!channel>`, and
     the Discord adapter sends without `allowed_mentions`, so an echoed broadcast
     would really ping the channel. A zero-width space after the sigil keeps the text
     reading the same;
   - sent with `parse_mode="markdown"` so the `> ` quote renders: Slack builds a
     `plain_text` block for anything else and would show the markers literally;
     Telegram resolves either value to its own HTML default;
   - body: an i18n label per trigger kind, optionally `label · name`, then the
     prompt with every line `> `-quoted (readable on plain-text platforms too),
     truncated at `HARNESS_PROMPT_ECHO_MAX_CHARS = 800`;
   - dedupe: bounded per-process FIFO keyed on target + native/delivery id
     (`HARNESS_PROMPT_ECHO_MEMORY = 256`), so an in-process re-dispatch of one
     delivery cannot read as the task having fired twice. A cross-restart replay is
     not covered on purpose: it writes a new Delivery anyway, a duplicate echo is
     cosmetic, and a missing echo defeats the feature.
4. `core/session_turns.py::_hydrate_delivery_batch_context` stamps `display_texts`,
   every merged Delivery's snapshot in FIFO order, next to the existing
   `delivery_ids` — `_hydrate_delivery_context` set the singular `display_text` from the
   first Delivery only. Read from the snapshots, never `dispatch_text`, so the replay
   guards prepended there stay internal.
5. `core/scheduled_tasks.py::_build_context` stamps `task_definition_name` and
   `harness_display_prompt` next to the existing `task_definition_id`, both resolved
   in one best-effort lookup (`_definition_display_fields`: task row, else watch
   definition), so the label names the task instead of an id and the echo has the
   user-authored instruction to fall back on. A producer that composed the prompt
   itself can override the instruction through `metadata["harness_display_prompt"]`.
   `agent_run` has no definition. Both survive a cross-restart replay: the flush
   restores every provenance key that is not execution routing
   (`_EXECUTION_ROUTING_KEYS` is a blocklist).
6. Config: `V2Config.runtime.harness_prompt_echo` (default `true`), mirrored onto
   `AppCompatConfig` + `to_app_config` (the turn path reads `controller.config`,
   which is the compat object) and hot-reloaded in
   `Controller._refresh_config_from_disk`. The gate calls that mtime-guarded reload
   itself (`_refresh_runtime_config`), because a Harness turn passes through no IM
   inbound handler and would otherwise read the process-start snapshot. No UI,
   matching `harness_run_*` — but it *is* projected in
   `vibe/api.py::config_to_payload`, which is the deep-merge base of every
   `/api/config` save: a runtime key missing there is rebuilt from the dataclass
   default, so an opt-out would silently revert on any unrelated settings save. The
   four `harness_run_*` knobs are projected for the same reason (same latent bug,
   fixed here rather than left next to the new field).
7. Copy: `harness.promptEcho.*` in `vibe/i18n/en.json` and `zh.json`.

## Evidence

- unit — `tests/test_message_dispatcher_scheduled.py::HarnessPromptEchoTests`
  (per-kind coverage, every gate, delivery override, dedupe, memory bound,
  truncation/quoting, silent-only prompt, send failure, hot-toggle reload, a failing
  reload, display-snapshot precedence over internal dispatch text, instruction-only
  echo for the composed kinds — with the waiter output absent from the body and no
  echo at all when no instruction resolves — mention neutralization, markdown parse
  mode, a merged batch echoing every distinct prompt while repeat firings of one task
  collapse to one, blank snapshots falling back to the singular key);
  `tests/test_internal_server.py::test_flush_suppressed_segment_claims_each_delivery_id_in_one_turn`
  (the merged context carries both snapshots);
  `tests/test_api_save_config_merge.py::test_save_config_preserves_harness_runtime_knobs_on_partial_save`;
  `tests/test_message_handler_harness_echo.py` (pipeline staging: raw prompt,
  subagent-prefixed prompt staged unstripped, human turn stages nothing, blank prompt
  stages nothing, backgrounded thread resolution visible to the echo, and no send from
  the pipeline);
  `tests/test_agent_service.py` (turn-start emission: a queued turn stays quiet until
  it owns the runtime gate, the key is popped, no staged prompt echoes nothing, a
  failing echo never breaks turn start, a hanging echo cannot hold the gate);
  `tests/test_scheduled_tasks.py::test_build_context_carries_the_definition_name_for_display`,
  `::test_build_context_prefers_an_explicit_display_prompt_from_the_request` and
  `::test_build_context_survives_a_store_that_cannot_name_the_definition`.
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
