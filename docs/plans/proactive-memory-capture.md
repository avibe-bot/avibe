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

> This section describes the shipped end state. An interim release (#1092)
> gated the behavior behind a `memory.proactive_capture` opt-in; that design
> was removed after a product re-evaluation and survives only as history in
> the review-round sections and the removal follow-up at the end of this
> document. Read this section when changing the behavior.

Give the Agent a proactive Memory-write habit with enough noise control that
what it records stays useful.

1. With Memory enabled, the injected guidance tells the Agent to call
   `vibe memory remember` on its own initiative when a turn produces a durable
   signal. There is no separate opt-in: the Memory enable disclosure states
   Agent-recorded durable facts as part of what enabling Memory means.
2. Name the signals worth recording: a preference or identity detail that
   emerged across several turns, a correction of Agent behavior, a decision the
   conversation reached, and a durable user- or machine-specific environment
   fact. Project conventions, architecture, and workflows stay on the
   `AGENTS.md` surface.
3. Give explicit noise controls so proactive capture does not degrade into
   logging: one self-contained distilled fact per call, no paraphrase of a plain
   text message automatic capture already holds, no one-off task detail, nothing
   derivable from code or git, no secrets, a small per-turn budget, and silence
   when in doubt.
4. On Memory-admitted turns, route eligible explicit requests for durable,
   non-secret personal facts and stable habits to `vibe memory remember` unless
   the user names another permitted destination. The shared preferences file
   remains writable only when the user names that file itself; project knowledge,
   transient details, and secrets keep their existing exclusions and surfaces.

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

The behavior lives entirely in the injection layer plus the enable disclosure;
there is no new persisted config.

### Config and settings surface

`memory.enabled` is the only Memory switch. A config written by the interim
opt-in release still loads: the stored `proactive_capture` flag is dropped on
read and no longer serialized. The settings PATCH whitelist is
`{enabled, processing}`, so a stale client sending the retired field gets
`memory_invalid_input` instead of a silent merge. `Controller.reconcile_memory`
has no prompt-only classification: every accepted save runs the full runtime
reconciliation.

The Settings -> Memory disclosure, subtitle, and enable hint state
unconditionally that durable facts the Agent records while working for the user
are part of Memory.

### Injected prompt

`_MEMORY_CLI_PROMPT` is the single Memory section: the four `vibe memory` CLI
examples and the read guidance, plus a trigger list, a signal-quality block, a
silent-recording posture, and a surface-routing paragraph. Its no-paraphrase
rule is scoped to plain text messages on purpose: automatic capture drops IM
turns carrying files while the prompt gate does not, so a durable fact stated
only alongside an attachment is still the Agent's to record. The exception is
phrased in terms the Agent can observe — whether the message arrived with a
file — rather than internal admission state. An explicit request overrides only
that no-paraphrase rule: the existing durability, secret, task-detail, and
surface filters still apply. The Agent confirms a requested write only after
`remember` returns `accepted` or `duplicate`; any nonzero outcome is reported as
a failure without an unbounded retry.

`build_system_prompt_injection` injects it whenever `include_memory_cli` is
true. The three backends resolve `memory_cli_prompt_admitted` once per turn —
it associates or clears the Memory CLI session scope as a side effect — and
pass the result straight through.

### `_USER_PREFERENCES_PROMPT`

Without Memory admission, the file keeps its historical explicit-request role.
When Memory is admitted, eligible general remember requests and proactive writes
route to `vibe memory remember`; the file becomes read-only unless the user names
it as the destination. This named-file exception is explicit because the file
lives under the Avibe state directory, while Memory's SQLite and runtime-owned
state files remain prohibited write targets.

### Test coverage

Prompt assertions anchor on semantic keywords rather than whole-paragraph
equality. The injected-command contract test keeps every `vibe memory` example
in the single prompt under the parser-backed live-caller check. Config-load
tolerance and settings-PATCH rejection of the retired flag each have their own
cases.

## Todo

- [x] Inject the proactive contract whenever Memory is admitted, as the single
      Memory prompt.
- [x] Route eligible explicit remember requests to Memory on admitted turns,
      while preserving the named preferences-file destination and all existing
      eligibility and safety filters.
- [x] Drop the retired `proactive_capture` flag on config load and reject it in
      the settings PATCH whitelist.
- [x] Describe Agent-recorded facts unconditionally in the Settings disclosure,
      subtitle, and enable hint (en/zh).
- [x] Update the prompt, live-caller CLI contract, config, and settings-route
      tests to the single-prompt contract.
- [x] Update `docs/plans/memory-plugin-system.md` and the CLI references.
- [x] Run the focused tests and `ruff check` on changed Python files.

The review-round sections below record the interim opt-in design (#1092) as
history; they are not normative.

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
      explicit-request surface. This was the round-3 behavior; the later
      admitted explicit-request routing contract below supersedes the automatic
      cross-project offer while retaining writes when the user names the file.

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

## Follow-up: the opt-in switch is removed (2026-07-30)

The `memory.proactive_capture` opt-in shipped in #1092 as a response to the
round-4 consent finding, but it was never part of the approved product design,
and it carried most of the PR's accidental complexity: the flag lived inside
`MemoryConfig`, whose changes otherwise drive sidecar reconciliation, so every
config path had to learn to classify prompt-only versus runtime-relevant
changes (runtime identity projection, a prompt-only fast path, a
sequence-numbered publication merge against a reconcile race, and a
settlement-equality exclusion).

After re-evaluation the owner decided proactive capture is part of what
enabling Memory means. The switch, its UI toggle, and the whole prompt-only
machinery are removed:

- `MemoryConfig` no longer carries `proactive_capture`; a stored flag from the
  interim release is dropped on load and no longer serialized.
- `build_system_prompt_injection` has a single Memory prompt: enabling Memory
  injects the proactive contract (When to remember / noise controls / surface
  routing) directly. The requested-only variant is gone.
- `reconcile_memory` is back to its pre-#1092 shape; the settlement comparison
  normalizes only `embedding_change_pending` again.
- The settings PATCH whitelist is `{enabled, processing}`; a stale client
  sending the retired field gets `memory_invalid_input`.
- Settings UI and disclosure describe Agent-recorded facts unconditionally.

Consent posture: the Memory enable disclosure now states Agent-recorded
durable facts as part of what enabling Memory means, instead of gating them
behind a second toggle.

## Follow-up: admitted explicit-request routing (2026-08-12)

General requests to remember durable, non-secret personal facts and stable
habits now use Memory whenever the CLI is admitted for the turn. This does not
broaden eligibility: project knowledge still goes to `AGENTS.md`, transient and
one-off task details remain out, and secrets are never queued. A user who names
the shared preferences file can still select it directly, despite its location
under Avibe state; runtime-owned Memory files and SQLite remain off-limits.

The acknowledgement contract follows the CLI outcome: `accepted` and
`duplicate` permit a short success confirmation, while every nonzero outcome is
reported as a failure and must not be described as saved.
