# Claude session recovery errors

## Problem

A persisted Claude session can outlive its native transcript. Resuming it
fails before a model request is sent. The shared authentication classifier
currently matches `401` anywhere in an error string, including inside a
session UUID, and incorrectly offers OAuth login. A pending Workbench input
can also be retried repeatedly after this permanent startup failure.

## Change contract

- Classify HTTP authentication failures from actual status evidence, not
  digits embedded in identifiers, paths, or unrelated numeric values.
  Preserve supported real authentication-error shapes across all backends.
- Keep Claude's missing-native-session error distinct from authentication,
  transport, and model-supply failures through its user-facing error path.
- Preserve the Avibe session, native-session binding, conversation history,
  working directory, and unsent user input. Never silently create an empty
  replacement session or replay a failed input in an unbounded loop.
- Reuse existing terminal-turn and queued-input ownership. Investigate the
  retry path on current master before changing it; add no parallel queue or
  persistent lifecycle unless the existing model cannot express the outcome.
- User-facing recovery guidance is localized and explains how to resume in
  the original location or explicitly start a new conversation.
- No automatic OAuth reset, model switch, runtime restart, or live data
  mutation belongs to this code change.

## Boundaries and ownership

The shared authentication classifier produces the recovery decision;
backend error handling consumes it and owns the concrete failure kind.
The session handler produces the missing-transcript error. Existing turn
settlement and Workbench queue services own failure completion and pending
input. Model Hub routing and engine behavior remain independently diagnosed
by the orchestrator and are outside this implementation lane.

## Validation

- Reproduce a missing-session error containing a UUID with `4016`, and
  verify a variant without those digits has the same classification.
- Cover identifier/path/number false positives and genuine HTTP 401 and
  authentication messages for each existing backend.
- Exercise the consuming Claude error path and durable user-visible
  notification, preserving session binding and input ownership.
- Reproduce any queue change through its real service/controller boundary
  with test-owned state, including unchanged state and explicit retry.
- Run focused tests and changed-file Ruff, then exact-head Codex review
  and the repository's complete required CI gates.

## Implementation

- The authentication classifier recognizes contextual HTTP/CLI status forms
  while retaining backend-specific authentication messages.
- Claude's typed missing-session failure bypasses OAuth recovery. Its localized
  notification includes the existing `MessageOutput` Turn provenance so the
  failed-notice Retry action can resolve the original input.
- Before native write, Claude records `failure.reason=native_session_not_found`
  and `failure.requires_explicit_retry=true` in the shared dispatch evidence.
  The existing terminal owner copies this evidence to the `not_written` start
  receipt in each affected Delivery's history. No schema or lifecycle is added.
- Automatic start claims respect that receipt until the existing failed-notice
  Retry action or explicit Send Now authorizes a new attempt. Input snapshots
  remain unchanged. Other startup failures and unknown acceptance keep their
  existing policy; a repeated missing-session failure requires another explicit
  retry.
- MESSAGE-DELIVERY-029 covers Claude's consuming error path, durable input
  retention, periodic/restart recovery, and both explicit retry paths.

## Operational recovery

The orchestrator separately checks native transcript availability and live
model requests using supported Avibe APIs and CLI. Recovery must preserve
existing user data and distinguish reconstruction from an exact native
resume. Live operational results are not substituted for hermetic tests.
