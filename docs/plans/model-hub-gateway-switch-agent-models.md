# Agent models across a switch to the Gateway (MH-SWITCH-001/002)

Status: change contract for the fix branch `fix/opencode-gateway-first-turn`.
The owner decides the merge.

## Background

Observed on a fresh install at master `219c81d`: all backends were on Direct and
OpenCode's native `opencode.json` configured `anthropic` and `openai` providers.
The built-in `opencode` Agent carried Avibe's recommended Direct model
`openai/gpt-5.6-sol`.

1. The user switched OpenCode to the Gateway, either through the migration dialog
   (which deduplicated both providers into the existing Sources) or through
   "Later" (a mode-only switch).
2. OpenCode's Gateway menu stayed empty, so the Agent's model had no menu row and
   the first turn failed with `modelHub.errors.mapping_target_unavailable`.
3. The Models page reported "`openai/gpt-5.6-sol` is not a known model name".
   Adding `gpt-5.6-sol` through Manage models did not help, because an OpenCode
   selection had to equal a menu id byte for byte. Only rewriting the Agent's
   model to `gpt-5.6-sol` made the turn run.

Claude Code and Codex were unaffected only because their fixed menus already
contain the built-in ids their Agents use. A Codex or Claude Agent pinned to a
non-built-in model has the same gap.

## Cause

Two facts meet at the switch, and neither owner handled it:

- Every Gateway turn runs a menu row (`effective_model_route` requires menu
  membership; OpenCode's overlay projects only menu rows). Entering the Gateway
  never added the models Agents already select, even when a Source lists them.
- OpenCode's two modes spell models differently (`model-hub.md` §4.8.3). A
  Direct selection is an OpenCode reference, `<native provider>/<model>`; a
  Gateway menu id is the bare model. Nothing read a Direct selection in the
  Gateway world, so even a matching row was unreachable.

## Contract

1. **Carry Agent selections on entering the Gateway (MH-SWITCH-001).** When a
   backend switches from Direct to Hub, through `set_agent_mode` or the
   migration transaction, each enabled Agent's model that the menu does not
   list gains the row the model picker adds for a provider model: origin
   `provider`, supplier metadata and models.dev enrichment as the picker derives
   them, and OpenCode's `native_protocol` from the vendor family. It is added
   only with inventory evidence: a configuration-eligible Source in the backend's
   default order lists the model (the automatic route tier). Speculative API-key
   passthrough is not evidence. For OpenCode the whole selection is tried first,
   then its model part. The rows are committed atomically with the mode, inside
   the existing switch guard or migration record.
2. **Read an OpenCode selection as an OpenCode reference (MH-SWITCH-002).** In
   Gateway mode an OpenCode selection names the menu row equal to the whole
   selection, otherwise the row equal to its model part after the first `/`,
   provided both parts are nonempty. An exact id always wins, so a namespaced
   upstream id such as `moonshotai/kimi-k2` keeps its own row. The same pure rule
   (`identifiers.opencode_menu_model_id`) serves the turn's overlay snapshot and
   the service projections that consume Agent selections: the default Agent's
   requested model, `named_agents`, and the interruption/removal guards.
3. **Selections are not rewritten.** Agents, channel overrides and session pins
   keep their spelling, so switching back to Direct runs them unchanged. Direct
   mode projects each selection exactly as spelled.

## Interaction with "Agent needs attention"

- A carried selection resolves to its row, so it no longer appears as an issue,
  and `effective_model_id` names the row that actually runs. The UI's supply
  attribution therefore counts that row as claimed instead of unassigned.
- A selection nothing serves keeps today's issue and copy, naming the selection
  as spelled. The repair the copy implies now works for OpenCode: adding the
  model part (`gpt-5.6-sol`) through Manage models makes `openai/gpt-5.6-sol`
  runnable without editing the Agent.
- Removing a row an Agent runs through its model part is reported by the
  existing interruption guard, because the guard reads the same resolution.

## Known by design

- Carry-over runs only on the Direct-to-Hub transition. A Source added later, or
  a Hub-mode backend whose Agent selects an absent model, keeps using the
  attention repair; the menu stays user-owned between switches.
- A row in `removed_model_ids` is never re-added: the user removed it on purpose.
- The reverse switch is unchanged. An Agent whose model was set to a bare Gateway
  id still needs a provider in Direct mode (`opencode.default_provider`).
- The attention copy is unchanged; it stays truthful for both populations.

## Validation

- `MH-SWITCH-001`: service-boundary scenario from Direct with two API-key
  Sources and four OpenCode Agents (served, served by another vendor, removed,
  unserved), plus a Codex non-built-in selection. Asserts the carried rows and
  their metadata, the per-Agent projection, a real loopback gateway turn from
  the prepared overlay reaching the right Source and upstream id, and the Direct
  round trip. A related case applies the real migration transaction.
- `MH-SWITCH-002`: overlay unit contract, replacing the former
  `test_opencode_overlay_never_repairs_a_prefixed_identifier`, which asserted
  the opposite.
- Existing Model Hub API, routing-modes, resolution, injection, takeover and
  scenario suites; changed-file Ruff.
- Real E2E on a temporary worktree Incus environment: Direct install with native
  OpenCode providers, switch to the Gateway both ways, first turn succeeds.
