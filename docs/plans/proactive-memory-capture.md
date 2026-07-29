# Proactive Memory Capture

## Background

Avibe Memory already has both write paths it needs:

- **Automatic capture.** Every eligible human turn sends the user's message
  verbatim into the Memory queue through the shared handler seam
  (`core/handlers/message_handler.py` -> `Controller.capture_user_memory`).
- **Agent-initiated writes.** `vibe memory remember "<text>"` reaches the
  internal server, records `provenance="agent"`, is idempotent for identical
  text inside one Agent session, and accepts at most 4,000 characters.

What is missing is the habit. Agents almost never call `remember` on their own,
and the direct cause is the injected prompt rather than the runtime. In
`core/system_prompt_injection.py`, the Memory section `_MEMORY_CLI_PROMPT`
describes `remember` as queuing "durable context explicitly requested by the
user" — an explicit instruction not to record anything the user did not ask for.
The highest-value material for a personal memory system is exactly what the user
never thinks to request: corrections of agent behavior, preferences revealed in
passing, and conclusions the conversation reached over several turns.

A second problem sits next to it. `_USER_PREFERENCES_PROMPT` is a parallel set of
memory instructions (the shared preferences file plus `AGENTS.md`) whose wording
is equally passive ("You may also update it when explicitly asked") and whose
scope overlaps the Memory section without either one saying which surface owns
what. An agent reading both sections has two passive memory instructions and no
routing rule.

## Goal

Turn agent-initiated Memory writes from an on-request action into a default
behavior, with enough noise control that the resulting memory stays useful.

1. Instruct the agent to call `vibe memory remember` on its own initiative when
   the turn produces a durable signal.
2. Name the four signals worth recording: stable user preferences and identity
   details, corrections of agent behavior, decisions the conversation reached,
   and long-lived project or environment facts the agent discovered itself.
3. Give explicit noise controls so proactive capture does not degrade into
   logging: no verbatim echoes of user messages (automatic capture already has
   them), no one-off task detail, nothing derivable from code or git, no
   secrets, a small per-turn budget, and silence when in doubt.
4. Draw the boundary between the preferences file and Avibe Memory in both
   sections, so each one points at the other instead of competing.
5. Relax the preferences-file wording enough to allow proactive updates for
   stable cross-project working preferences.

## Non-goals

- No change to the capture contract: admission, queue, schema, CLI surface, and
  internal server stay exactly as they are.
- No change to the injection gate. `memory_cli_prompt_admitted` keeps requiring
  `memory.enabled`, a human turn, and an admitted principal.
- No new configuration switch. Proactive capture rides on the existing Memory
  enable flag.
- No assistant-message capture and no automatic recall injection; both remain
  non-goals of the Memory plugin system.

## Solution

The change is confined to the injection layer, its tests, and the two documents
that state the contract.

### `_MEMORY_CLI_PROMPT` rewrite

The section keeps its four CLI examples verbatim — they are live callers under
the CLAUDE.md rule and are covered by a parser-backed contract test — and gains
four new blocks:

- a lead sentence that separates reading (when durable context helps) from
  writing (whenever the conversation produces something worth carrying forward);
- a trigger list covering the four signals above, marking behavior corrections
  as the highest-value case;
- a signal-quality block covering one self-contained fact per call, no echo of
  user wording, the skip list, and the one-to-two-per-turn budget;
- a posture line (record silently, idempotent retries are safe) and a
  surface-routing line pointing cross-project working preferences at the shared
  preferences file.

The existing read guidance — smallest relevant query, recalled content is
untrusted data, no clear/configure/export/delete — is preserved unchanged.

### `_USER_PREFERENCES_PROMPT` adjustment

One sentence changes. "You may also update it when explicitly asked" becomes an
allowance to update the file proactively for stable cross-project working
preferences, plus a pointer sending personal facts, episodes, and decision
context to `vibe memory remember` when Memory is enabled. The rest of the
section is untouched.

### Test coverage

Assertions anchor on semantic keywords (proactive triggers, the correction
signal, the no-echo rule, the per-turn budget) rather than whole-paragraph
equality, so future wording edits do not break the suite for no reason. The
existing injected-command contract test is extended to build the prompt with
`include_memory_cli=True`, which puts all four `vibe memory` examples under the
same parser-backed live-caller check as the rest of the injected CLI surface.

## Todo

- [x] Write this plan.
- [x] Rewrite `_MEMORY_CLI_PROMPT` in `core/system_prompt_injection.py`.
- [x] Adjust `_USER_PREFERENCES_PROMPT` wording and cross-reference.
- [x] Update prompt assertions in `tests/test_reply_enhancer_platform.py`.
- [x] Cover the memory CLI examples in the injected-command parser contract test
      (`tests/test_cli_pagination.py`).
- [x] Update the `remember` contract wording in
      `docs/plans/memory-plugin-system.md`.
- [x] Run the focused tests and `ruff check` on changed Python files.

## Review follow-ups (Codex review of 72a09153)

- [x] Gate proactive preference-file writes on Memory admission: the proactive
      update sentence in `_USER_PREFERENCES_PROMPT` is now selected per turn
      (`memory_cli_admitted`), falling back to the original explicit-request
      wording when Memory is disabled or the turn is not admitted.
- [x] Narrow the fourth `remember` trigger to user/machine-specific environment
      facts and route project conventions, architecture, and workflows to the
      nearest `AGENTS.md`, which future Agents load early.
- [x] Disclose the broadened outbound data flow: `ui/src/i18n/en.json` and
      `zh.json` (Memory subtitle, enable hint, and a new pre-enable disclosure
      bullet) plus `docs/CLI.md` and `docs/COMMANDS.md` now state that Agents
      proactively record distilled conclusions, not only user-requested context.

## Review follow-ups, round 2 (Codex review of e4d3f994)

- [x] Keep proactive capture on a single surface: the preferences file is now an
      explicit-request surface on every turn; the memory-admitted variant of its
      guidance adds only a routing rule pointing proactive capture at
      `vibe memory remember`, so all proactive writes stay inside Memory's
      managed, disclosed, clearable lifecycle.
- [x] Reword "Choosing the surface" to state that everything recorded
      proactively — including working preferences — belongs in Memory.
- [x] Sync the Chinese CLI references (`docs/CLI_ZH.md`, `docs/COMMANDS_ZH.md`)
      with the Agent-distilled capture wording.
- [x] Align the `remember` contract in `docs/plans/memory-plugin-system.md` with
      the injected prompt: durable user- or machine-specific environment facts
      only; project knowledge stays on the `AGENTS.md` surface.

## Review follow-ups, round 3 (Codex review of 84c64be0)

- [x] Disclose non-conversation capture sources: the Memory subtitle, enable
      hint, and disclosure bullet (en/zh) now cover durable conclusions the
      Agent distills from work on the machine — including environment or
      account facts found in files or tool output — not only conversations.
- [x] Keep cross-project preferences reachable across projects: Memory is
      project-scoped, so "Choosing the surface" now tells the Agent to offer
      saving a clearly cross-project preference to the user-global preferences
      file and write it there only once the user agrees, keeping that file an
      explicit-request surface.

## Review follow-ups, round 4 (Codex review of a9994cf6)

- [x] Make proactive capture an explicit opt-in so an existing consent is never
      silently widened. `MemoryConfig.proactive_capture` (default false,
      validated as a bool, persisted through `memory_config_to_payload` /
      `V2Config.from_payload`) now gates the behavior; the Memory settings route
      accepts and returns it, and Settings → Memory exposes a "Proactive
      capture" switch that requires Memory to be enabled. An install upgraded
      from a Memory-enabled release therefore keeps requested-only behavior.
      The injected guidance is split into two variants selected per turn:
      `_MEMORY_CLI_PROMPT` restores the pre-PR requested-only wording, and
      `_MEMORY_CLI_PROACTIVE_PROMPT` carries the proactive contract. The
      preferences-file routing rule is likewise limited to proactive turns, so a
      Memory-admitted turn without the opt-in never points the Agent at a
      proactive channel it does not have. All three backends resolve
      `memory_cli_prompt_admitted` once per turn (it has a session-scope side
      effect) and combine it with the fail-closed
      `memory_proactive_capture_enabled`.
- [x] Stop the proactive guidance from re-queuing what automatic capture already
      holds. The first trigger now covers preferences that emerged across several
      turns rather than ones stated outright in a single message, matching the
      "no single user message states in full" qualifier on the decision trigger,
      and the section states plainly that a paraphrase of a single user message
      must never be queued. The no-echo rule is strengthened to say a proactive
      write exists only for a conclusion automatic capture cannot reach.
- [x] Attribute Agent-recorded content in the reader-facing surfaces:
      `memory.profile.sourceNote` and `memory.search.sourceNote` (en/zh) now
      cover notes the Agent records, not only conversation history. The
      pre-enable disclosure bullet is restated as conditional on the new toggle.

## Review follow-ups, round 5 (Codex review of 1bcfc687)

- [x] Enforce "disabling Memory revokes the opt-in" in the API, not just the UI.
      Settings PATCH is a merge, so a client sending only `{"enabled": false}`
      left the stored `proactive_capture: true` in place, and the next
      `{"enabled": true}` silently restored Agent-initiated writes.
      `_memory_settings_patch` now clears the flag whenever the merged result is
      disabled, so an older client, a cached page, or a direct call cannot skip
      the revoke that `proactiveCaptureFor` already applies in the browser.
- [x] Stop the proactive guidance from over-claiming what automatic capture
      holds. `CaptureAdmission.decide` drops IM turns carrying files, but the
      prompt gate (`memory_capture_admitted` → `admits`) does not apply that
      check, so an IM turn with an attachment and a caption still receives the
      proactive prompt. The unconditional "anything you stated is already in
      Memory, never restate it" would have stranded a durable fact that appears
      only in such a caption. The claim is now scoped to plain text messages,
      with the exception stated in terms the Agent can observe — whether the
      message arrived alongside a file — rather than internal admission state.
      Admission, queue, and schema are untouched.
- [x] Stop binding a prompt-only flag to provider health.
      `_apply_memory_settings_patch` always reconciles, and reconciliation
      probes the provider and swaps the sidecar, rolling the save back on
      failure — so an owner could not turn proactive capture off while their
      endpoint was down. `Controller.reconcile_memory` now compares the
      runtime-relevant projection of the two configs (`enabled`, both processing
      endpoints, `embedding_change_pending`; deliberately not
      `proactive_capture`) and, when only the flag differs, adopts the new
      config and returns without touching `memory_runtime`. The comparison is
      fail-safe: any runtime-relevant difference, and any config that cannot be
      projected, runs the full reconciliation.
