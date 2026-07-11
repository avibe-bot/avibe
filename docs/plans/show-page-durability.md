# Show Page Git Checkpoints

Issue: [#669](https://github.com/avibe-bot/avibe/issues/669)

## Goal

Make Show Page workspaces durable without relying on agent behavior. Avibe owns a
native Git repository outside the served workspace and converges its `main`
branch to the existing worktree at each session turn boundary.

## Design

- Resolve the checkpoint Git binary through one seam: vendored first, then a
  macOS-CLT-aware system lookup, otherwise degrade without blocking Show Pages.
- Lazily adopt only existing workspaces into external gitdirs under
  `~/.avibe/show-git/`; write the workspace `.git` pointer only when Avibe owns
  it, and use shadow checkpoints beside user-managed repositories.
- Subscribe in the controller to `turn.start` and `turn.end`; project checkpoint
  lifecycle from the shared inbound/terminal turn chokepoints every backend and
  source traverses. Existing Workbench and streaming dispatch events remain for
  their UI/cancellation lifecycle, while context-local state makes overlap
  idempotent. Terminal authority is recorded before delivery, then the checkpoint
  runs after delivery/persistence and before the event loop can start the next
  gated turn. Checkpoint paths never create Show Page workspaces.
- Isolate every platform Git invocation from ambient Git environment, global
  configuration, signing, hooks, and automatic GC.
- Self-heal only provably Avibe-owned or dangling state, bound retained history,
  keep static dot-path denial strict while preserving Vite dependency assets on
  the hardened runtime proxy, and publish ownership-gated Git guidance from the
  running checkpoint service's startup-latched capability state.

## Validation

- Focused unit coverage for resolution, adoption/ownership, checkpoint
  semantics, repair, pruning, event wiring, and Git environment isolation.
- Route coverage for private/public runtime proxy and static fallback boundaries.
- Incus edit/overwrite/restore/forward-commit verification remains an
  integration-pass check after the coordinated milestone lands.

## Integration Follow-up

The first implementation attached lifecycle publication to the known transport
paths, so background Agent Runs, scheduled tasks, and watch callbacks could reach
the backend without checkpoint events. The follow-up closes that architectural
gap at `SessionTurnManager.on_running` / `on_terminal_result`, after the runtime
gate and at the authorized terminal output respectively; controller delivery
completion executes the post-turn checkpoint. `MESSAGE-DELIVERY-006` enumerates
every current turn entry point against this shared contract.
