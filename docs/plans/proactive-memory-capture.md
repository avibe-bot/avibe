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

> The design below is the one that shipped. It converged through six review
> rounds — the earliest draft made proactive capture unconditional and touched
> only the prompt, which the review rounds recorded at the end of this document
> replaced. Read this section, not that history, when changing the behavior.

Give the Agent a proactive Memory-write habit that an owner switches on
deliberately, with enough noise control that what it records stays useful.

1. Add `memory.proactive_capture`, a persisted flag that **defaults to false**
   and is opted into separately from `memory.enabled`. Enabling Memory consents
   to capturing the user's own messages; letting the Agent decide what else to
   persist is a wider grant and must not arrive with an upgrade.
2. Inject one of two Memory prompt variants per turn. Without the opt-in the
   Agent sees the original requested-only contract; with it, the Agent is told
   to call `vibe memory remember` on its own initiative when a turn produces a
   durable signal.
3. Name the signals worth recording: a preference or identity detail that
   emerged across several turns, a correction of Agent behavior, a decision the
   conversation reached, and a durable user- or machine-specific environment
   fact. Project conventions, architecture, and workflows stay on the
   `AGENTS.md` surface.
4. Give explicit noise controls so proactive capture does not degrade into
   logging: one self-contained distilled fact per call, no paraphrase of a plain
   text message automatic capture already holds, no one-off task detail, nothing
   derivable from code or git, no secrets, a small per-turn budget, and silence
   when in doubt.
5. Keep the shared preferences file an explicit-request surface, and have the
   two sections route to each other instead of competing.
6. Make the opt-in behave correctly at its edges: disabling Memory revokes it,
   and revoking it never waits on provider health.

## Non-goals

- No change to the capture contract: admission, queue, schema, CLI surface, and
  internal server stay exactly as they are.
- No change to the injection gate. `memory_cli_prompt_admitted` keeps requiring
  `memory.enabled`, a human turn, and an admitted principal; the new flag can
  only narrow what that gate advertises, never widen who receives it.
- No proactive writes to the shared preferences file. Everything the Agent
  records on its own initiative stays inside Memory's managed lifecycle, which
  is disclosed before enabling and clearable through Clear all.
- No assistant-message capture and no automatic recall injection; both remain
  non-goals of the Memory plugin system.

## Solution

The change spans the persisted config, the settings surface, the config-to-
runtime seam, and the injection layer.

### Config and settings surface

`MemoryConfig.proactive_capture` (default false, validated as a bool) persists
through `memory_config_to_payload` and `V2Config.from_payload`, so a config file
written before this change simply reads as false. The Memory settings route
accepts and returns it, and Settings -> Memory exposes a "Proactive capture"
switch that requires Memory to be enabled.

Two properties are enforced by the API rather than by the browser, because an
older client, a cached page, or a direct call must not be able to skip them:

- **Disabling Memory revokes the opt-in.** Settings PATCH is a merge, so
  `_memory_settings_patch` clears `proactive_capture` whenever the merged result
  is disabled. Otherwise a request carrying only `{"enabled": false}` would
  leave a stored opt-in armed for the next enable.
- **Toggling the flag never reconciles the runtime.**
  `Controller.reconcile_memory` compares a projection of the runtime-relevant
  settings (`enabled`, both processing endpoints, `embedding_change_pending` —
  deliberately not `proactive_capture`) and, when only the flag differs, adopts
  the new config without touching `memory_runtime`. Reconciliation probes the
  provider and swaps the sidecar, and a failed probe rolls the save back, so
  without this an owner could not revoke Agent-initiated writes while their
  endpoint was down. The comparison is fail-safe: any runtime-relevant
  difference, and any config that cannot be projected, runs the full path.

Because a full reconciliation awaits the sidecar, a prompt-only save can be
requested and published while one is in flight — Avibe's startup reconciliation,
which captures the config as it was at boot, is the common case. A finished
reconciliation therefore publishes its own runtime fields but defers the
prompt-only fields to the newest request, so it cannot resurrect an opt-in the
owner revoked while it was waiting.

### Prompt variants

`_MEMORY_CLI_PROMPT` is the requested-only contract: the four `vibe memory` CLI
examples plus the read guidance, with `remember` described as queuing durable
context the user explicitly asked for.

`_MEMORY_CLI_PROACTIVE_PROMPT` adds, on top of the same examples and read
guidance, a trigger list, a signal-quality block, a silent-recording posture, and
a surface-routing paragraph. Its no-paraphrase rule is scoped to plain text
messages on purpose: automatic capture drops IM turns carrying files while the
prompt gate does not, so a durable fact stated only alongside an attachment is
still the Agent's to record. The exception is phrased in terms the Agent can
observe — whether the message arrived with a file — rather than internal
admission state.

`build_system_prompt_injection` selects between them with
`include_memory_proactive`, which is combined with `include_memory_cli` so the
new parameter can only narrow injection. The three backends resolve
`memory_cli_prompt_admitted` once per turn — it associates or clears the Memory
CLI session scope as a side effect — and combine it with the fail-closed
`memory_proactive_capture_enabled`.

### `_USER_PREFERENCES_PROMPT`

The file stays an explicit-request surface on every turn. The only variation is
a routing rule, injected only when proactive capture is actually on, pointing
anything the Agent decides to record on its own at `vibe memory remember`.

### Test coverage

Prompt assertions anchor on semantic keywords rather than whole-paragraph
equality, and cover both variants. The injected-command contract test builds the
prompt with and without the opt-in so every `vibe memory` example in either
variant stays under the parser-backed live-caller check. Config, settings-route,
and controller-seam behavior each have their own cases, including a gated
runtime stub that reproduces the startup-reconciliation race.

## Todo

- [x] Add `MemoryConfig.proactive_capture` with validation and persistence, and
      accept/return it on the Memory settings route.
- [x] Clear the flag server-side whenever the merged settings patch is disabled.
- [x] Skip runtime reconciliation when only the flag differs, and keep a
      superseded reconciliation from republishing a stale value for it.
- [x] Split the injected Memory guidance into requested-only and proactive
      variants, selected by `include_memory_proactive`.
- [x] Keep the preferences file explicit-request, adding only a routing rule on
      proactive turns.
- [x] Add the Settings -> Memory toggle, its i18n strings, and the conditional
      disclosure bullet.
- [x] Cover both variants in the prompt and live-caller CLI contract tests; add
      config, settings-route, and controller-seam cases.
- [x] Update `docs/plans/memory-plugin-system.md` and the CLI references.
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

## Review follow-ups, round 6 (Codex review of c95b07ca)

- [x] Close a race that could resurrect a revoked opt-in for the life of the
      process. `Controller.run` starts a background reconciliation carrying the
      config as it was at boot; while it awaits the sidecar, a prompt-only save
      can publish a revoked `proactive_capture`, and the boot reconciliation
      then republished its stale candidate on success — disk and UI showed the
      flag off while the live controller kept injecting proactive guidance until
      the next restart. `reconcile_memory` now claims a monotonic sequence
      number before its first await, and a completed reconciliation whose number
      has been overtaken publishes its own runtime fields but takes the
      prompt-only fields from the newest request. A lock spanning the
      reconciliation was rejected deliberately: it would queue the prompt-only
      path behind a provider probe, which is exactly the round-5 failure of an
      owner unable to revoke while their endpoint is down. The prompt-only
      branch has no await between classification and publication, so it is
      already atomic on the loop.
- [x] Rewrite Goal, Non-goals, and Solution to describe the opt-in design that
      shipped. They still described the first draft — proactive capture as
      unconditional default behavior, no new switch, changes confined to the
      prompt — which would have led a later reader to reintroduce the consent
      problem these rounds removed. The follow-up sections below are kept as
      evolution history, and the rewritten body says so.
- [x] Disclose machine-derived capture in the CLI references. `docs/CLI.md`,
      `docs/CLI_ZH.md`, `docs/COMMANDS.md`, and `docs/COMMANDS_ZH.md` now match
      the Settings disclosure: conclusions distilled from conversations *and*
      from work on this machine, gated on the proactive-capture opt-in.

## Review follow-ups, round 7 (Codex review of 3fb1a549)

- [x] Generalize the automatic-capture exception in
      `_MEMORY_CLI_PROACTIVE_PROMPT`. Round 6 excepted only messages arriving
      with a file, but the exclusion is much wider: adapters mark forwarded or
      shared content non-ordinary (for example Slack's `not has_shared_content`),
      and `_is_ordinary_human_text` drops every non-ordinary turn, so the prompt
      still told the Agent that text was already stored and must not be
      restated. The wording now says coverage stops at plain text and names the
      shape rather than any platform's internal rule — a turn carrying a file,
      forwarded or shared content, or any other non-plain form. The
      "record it rather than assume" failure direction and the no-paraphrase
      rule for plain text are both unchanged, and the no-echo bullet already
      carried the same plain-text qualifier from round 5.
- [x] Stop the Memory enable copy from promising a data flow that is off by
      default. `memory.subtitle`, `memory.settings.enableHint`, and both
      `sourceNote` strings asserted unconditionally that Agent-recorded facts
      are included, while `proactive_capture` defaults to false. All four are
      now conditional in en and zh: the subtitle and enable hint name the
      separate switch, and the source notes hedge with "any notes your Agent
      recorded" so they read accurately in either state. Copy-only, no
      conditional rendering — the strings are honest without one, and the
      pre-enable disclosure bullet keeps its existing conditional form.

## Review follow-ups, round 8 (Codex review of 6b13a0f2)

- [x] Exclude `proactive_capture` from settlement equality:
      `_same_memory_configuration` normalized only the pending marker, so an
      owner toggling the prompt-only flag while a pending embedding change was
      settling made the candidate look like a different configuration, failing
      the settlement with `memory_runtime_install_failed` and leaving both the
      marker and the runtime unreconciled. The comparison now normalizes both
      settlement-irrelevant fields, mirroring the controller's runtime identity.
