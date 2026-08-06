# OpenCode Active-Turn Timeout

## Background

OpenCode can accept an asynchronous prompt and keep its assistant message
incomplete while its provider runtime retries indefinitely. Avibe currently polls
that message without a wall-clock limit. The accepted Turn therefore retains its
runtime ownership, and the shared OpenCode runtime can prevent unrelated Agent
work from making progress. An operator abort eventually releases the runtime, but
the empty completion can then be reported as a successful "No response" result.

The async prompt-start confirmation behavior from #1189 and the rejected-prompt
handling from #1186 are earlier lifecycle phases and remain unchanged.

## Goal

- Bound every accepted OpenCode Turn with a configurable wall-clock deadline.
- Abort the native OpenCode session before releasing Avibe's Turn ownership.
- Settle a timed-out Run as failed with an explicit diagnostic, never as an empty
  successful result.
- Apply the same remaining budget when an active poll is restored after restart.
- Preserve user Stop, service shutdown, explicit provider errors, and normal
  responses inside the configured limit.

## Solution

Add `agents.opencode.active_turn_timeout_seconds`, defaulting to 90 minutes. The
deadline begins only after `prompt_async` is accepted. Persist that accepted time
on the existing active-poll record so restart recovery consumes the original
budget rather than granting a new one.

At deadline expiry, the poll owner:

1. aborts the exact native OpenCode session with a bounded cleanup request;
2. records the timeout as a Model Hub native failure when applicable;
3. emits the existing structured backend-failure terminal result; and
4. returns without entering the empty-success fallback.

The existing outbound terminal-result chokepoint remains the sole owner of Run
failure settlement, runtime-gate release, and queued successor admission.

## Scope

The `create_per_run` cwd report is not part of this lifecycle failure. Current
`master` already keeps per-run Session placement separate from a command's cwd
and resolves new Session workdirs from explicit metadata, Scope, or runtime
defaults. This change does not alter task routing or cwd inheritance.
