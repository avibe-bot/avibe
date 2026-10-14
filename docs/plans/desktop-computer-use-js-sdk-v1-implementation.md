# Computer Use JavaScript SDK v1 Implementation

**Status:** In progress
**Date:** 2026-10-14
**Authority:** `docs/plans/desktop-computer-use-js-sdk-design.md`

## Background

The shipped `vibe computer` command already routes each desktop operation
through Controller admission and `ComputerUseServer`. JavaScript scripts need
the same boundary, a stable object model, and a runner that uses Avibe's private
desktop Node runtime without resolving packages from the working directory.

The complete v1, including Web/IM confirmation ownership, is expected to exceed
the implementation brief's approximate 1,800-line split threshold. Delivery is
therefore divided into two independent PRs:

1. SDK core, runner, Controller script Session, and exact Turn binding.
2. `avibe.confirm`, `avibe.notify`, and expected-approver binding.

The second PR starts only after the first PR is delivered through its normal
review and CI gates.

## Change Contract

### Intended behavior

- `vibe computer run <file.mjs> [--arg k=v | --arg k=@file]` runs a normal ESM
  module on Avibe's verified private Node runtime.
- The runner injects resolution for `@avibe/computer`; project-local and global
  npm state cannot replace the shipped module.
- One script uses one keep-alive Controller connection for SDK calls and one
  total deadline.
- The SDK exposes the `computer -> app -> window -> element` model, declarative
  selectors, hidden element tokens, one safe re-observation for stale tokens,
  waits, expectations, typed errors, and an effect result for every action.
- The Controller binds a script connection to the exact live Turn object that
  launched it. Turn completion, Stop, or the script deadline invalidates the
  connection, so a later Turn in the same Session cannot reuse it.
- Script desktop operations continue through `ComputerUseServer`; neither the
  runner nor the SDK connects to the driver socket.
- Failures identify the current SDK step and include the most recent screenshot
  path when an observation produced one.

### Affected boundaries

- Controller internal HTTP API: script open, request, and close endpoints.
- Session Turn admission: expose the exact admitted Turn in the internal
  admission record.
- Python CLI/runtime packaging: script runner and shipped JavaScript assets.
- Built-in Computer Use Skill: script authoring and invocation guidance.

### Deferred behavior

- `avibe.confirm`, `avibe.notify`, pending-question ownership, and the
  verify-confirm-reverify-act continuation are delivered in the second PR.
- Existing single-operation `vibe computer` leaves are not rebased onto the SDK.
- Scheduled or unattended runs, automatic Skill saving, browser support,
  Windows desktop actions, and a persistent REPL remain out of scope.

## Validation

- Parser-backed coverage for every documented CLI example.
- Hermetic Controller route tests for exact-Turn binding, deadline invalidation,
  and stale connection rejection.
- Hermetic Node tests against a fake loopback Controller for module injection,
  selectors, stale-token re-observation, effects, waits, expectations, typed
  errors, failing-step reporting, and connection reuse.
- Focused pytest, Ruff on changed Python files, wheel content inspection, and
  exact-head review plus CI before close-out.
