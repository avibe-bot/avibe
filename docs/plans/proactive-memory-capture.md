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
