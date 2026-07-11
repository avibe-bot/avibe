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
- Subscribe in the controller to `turn.start` and `turn.end`; normalize the
  legacy streaming Show dispatch onto that bus without touching the shared turn
  manager, and never create Show Page workspaces from checkpoint paths.
- Isolate every platform Git invocation from ambient Git environment, global
  configuration, signing, hooks, and automatic GC.
- Self-heal Avibe-owned state, bound retained history, deny dot-leading asset
  segments before runtime proxying and static fallback, and publish the
  native-Git contract only when checkpoint Git is available.

## Validation

- Focused unit coverage for resolution, adoption/ownership, checkpoint
  semantics, repair, pruning, event wiring, and Git environment isolation.
- Route coverage for private/public runtime proxy and static fallback boundaries.
- Incus edit/overwrite/restore/forward-commit verification remains an
  integration-pass check after the coordinated milestone lands.
