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

## Verification

- Vary native defaults while holding Avibe selection constant; outgoing model
  and visible model choices remain unchanged.
- Missing Avibe selection produces a localized failure before native dispatch.
- Verify the same invariant for every registered backend and for connection
  probes, catalog projection, and IM model selectors.
- Run focused Python tests, UI tests/build for changed consumers, and the
  repository review/CI gates. Local production services are not restarted.
