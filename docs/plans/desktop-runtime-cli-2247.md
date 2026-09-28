# Desktop Runtime CLI (#2247)

## Contract

Desktop-launched agents can resolve ordinary `vibe` commands to the bundled
Runtime from an arbitrary working directory, including after relocation to an
installation path containing spaces. The bundle builder owns the entry point;
the archive and installed-tree digest cover it. CLI execution uses the private
interpreter with `-I -B -m vibe` and must not mutate the verified payload.

Only the dedicated CLI directory and existing Node directory are prepended to
PATH. The private Python scripts directory must never shadow user Python tools.
Remove build-bound Python console scripts before packaging. No install-time
patching, runtime generation paths in commands, or user shell profile changes.

## Scope decision

The builder, Rust launcher, packaging probe, and their existing tests own this
change. The baseline is `a78be44a1` on master. Another local working tree contains
an uncommitted related candidate; it remains untouched.

Login profiles run after inherited PATH is set. Profiles that introduce an
earlier global CLI directory or replace PATH can override the bundled command.
As explicitly permitted in the issue comment, document that limitation rather
than taking ownership of user shell startup. Codex's BASH_ENV/zsh caller-context
issue remains separate.

## Validation

- Extend the Rust launcher contract test to protect CLI precedence and preserve
  user Python resolution; the old launcher exposes only Node.
- Exercise the generated command through real shells after archive extraction
  and relocation, including a conflicting inherited global CLI. Assert argument
  forwarding and exit status. Existing tests only probe direct module startup.
- Verify console-script removal and archive inclusion at the builder boundary.
- Have the packaging probe invoke bare `vibe` through the platform shell and
  load both reported built-in skills against a test-owned HOME.
- Run the complete changed Python test file, Ruff, Rust formatting and runtime
  host tests; require current-head automatic review, green CI and no unresolved
  review threads before handoff. Do not merge or deploy.

## Progress

- [x] Confirm issue, constraints, baseline and isolated worktree.
- [x] Implement builder, launcher and packaging probe.
- [x] Verify regression against the unchanged baseline and fixed behavior.
- [ ] Complete PR review and CI.

Local checks: all 17 builder tests and 110 Runtime host library tests pass,
along with Ruff, Rust formatting and the Workbench production build. Restoring
the baseline launcher's PATH construction makes the updated consumer contract
fail for the missing CLI directory; restoring the fix passes it. Real shell
tests extract the generated archive, remove its build location, and check CLI
precedence, unchanged Python/pip resolution, arguments and exit status.

Full native bundle verification is in progress. The system Python and uv are
older than the build pipeline requires, so use the existing development Python
and an isolated invocation of CI's pinned uv 0.12.10.

The current Codex desktop task has no Avibe Session ID; `vibe watch add` returns
`missing_session_policy`. Do not create a callback in an unrelated Avibe session.
Keep the bundled PR/CI waiter and its durable cursor associated with this task.
