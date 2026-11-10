# Avibe-Owned Model Selection

Owner decision, 2026-09-08: backend-native default models have no role in
Avibe. This applies equally to OpenCode, Claude, and Codex, in Gateway and
Direct mode.

## Contract

- Avibe resolves a model from the Session, scope, Agent, or an explicitly
  selected subagent. Existing Avibe Agent recommendations remain owned by
  Avibe and are materialized when Agents are created.
- Every inference request carries an explicit model. An unresolved selection
  fails before backend dispatch; it never delegates selection to a CLI.
- Native default models do not populate, rank, or label Avibe model catalogs,
  and do not select connection-probe models.
- The Claude `default` pseudo-model is not a selectable model. Existing stored
  selections are preserved and produce an actionable error when invoked.
- Native authentication, provider configuration, model capability discovery,
  and explicit subagent definitions retain their independent responsibilities.

## When a Session's change applies

Owner decision, 2026-11-10: a change to a Session's model, reasoning effort, or
speed (the Web picker, `vibe session update`) reaches a running Vibey Turn at
its next model request. `vibe session update` reports this per backend as
`applies_from`.

- **Vibey: the next model request.** Vibey resolves its route again for every
  model request. Before each one after a Turn's first, it reads its Session's
  row (one read by primary key, off the controller loop) and applies dispatch's
  own rule, `session_model_settings`, to the row as it is now.
  - Only a Turn that dispatch resolved from the Session's row follows it: its
    context carries that row as `agent_session_target`.
  - A setting the row does not name is what the Session inherits: channel
    routing, then the Agent's default, as dispatch recorded them on the request
    (`vibe_agent_inherited_model`, `vibe_agent_inherited_reasoning_effort`). A
    restart rerun inherits what its Turn was accepted with.
  - An explicit subagent model or effort is the request's own and never changes.
  - The effort still passes the hop's capability filter, and the new model is
    resolved through Model Hub. A model nothing can serve fails that request
    with Model Hub's refusal, as at any resolution.
  - The Turn keeps its prefix: the tool list, the hosted-tool declarations, and
    the system prompt, including its model-specific sections. They follow the
    new model from the next Turn.
  - The gateway replaces the Turn's route once its previous request has ended,
    and the Turn's provenance record names the model it asked for last.
  - The transcript needs no change: thinking and opaque payloads go back only
    to the origin and Source identity that produced them
    (`agent-core-contracts/cross-provider.md`), and each request is budgeted on
    its own route, so a smaller window compacts before the request is sent
    (`agent-core-contracts/context.md`).
- **Claude Code, Codex, and OpenCode: the next Turn.** Codex takes a model and
  an effort only on `turn/start`. Claude Code's effort is a process option, and
  a model change inside a Turn is unverified and would need new plumbing.

## Verification

- Vary native defaults while holding Avibe selection constant; outgoing model
  and visible model choices remain unchanged.
- Missing Avibe selection produces a localized failure before native dispatch.
- Verify the same invariant for every registered backend and for connection
  probes, catalog projection, and IM model selectors.
- Run focused Python tests, UI tests/build for changed consumers, and the
  repository review/CI gates. Local production services are not restarted.
