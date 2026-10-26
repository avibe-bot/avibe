# Claude subscription-cap cooldowns (#2530)

## Change contract

Consume only the existing Claude-only `RawCallOutcome.claude_rate_limit_headers`
projection. A generic HTTP 429 becomes the existing `quota_exhausted` cooldown
only with a shared subscription rejection and a valid active reset. Carry the
absolute deadline through classification into existing recovery admission and
settlement; select the latest of local delay, valid upstream advice, and the
existing Source deadline. No schema, enum, adapter contract, engine, scheduler,
credential, deployment, or UI changes.

## Evidence and bounded interpretation

The managed engine's upstream implementation (`CLIProxyAPI` v8.0.23,
`internal/runtime/executor/helps/claude_ratelimit.go`, cited by #2530) distinguishes
shared 5h/7d rejection from overage/model-specific rejection, parses Unix/aware
ISO/HTTP dates, and bounds recoverable resets to seven days plus one hour.
Avibe owns policy and does not copy its random grace period or engine cooling.

- Explicit rejected shared 5h/7d windows activate their own reset.
- A rejected aggregate with an exact 5h/7d representative claim can activate
  that window when its status is omitted (not when explicitly allowed).
- An aggregate reset is usable for shared rejection only when it is not
  attributed to overage/model-specific claims. Aggregate-only evidence must not
  contradict explicit healthy shared windows or name an overage-only refusal.
- Inactive window resets, utilization alone, overage billing resets and
  model-specific 7d_oi resets cannot independently cool a whole Source.
- Select the latest valid active deadline, bounded relative to the captured
  response receipt. Missing, malformed, expired, negative, excessive, or
  ambiguous evidence leaves ordinary 429 classification/advice unchanged.
- Explicit machine-code remedies remain authoritative; only ordinary
  `rate_limited` decisions are refined. Post-output terminal semantics remain.

Only the derived deadline and existing reason enter recovery/Source state.
Raw header values never enter events, diagnostics or provenance.

## Validation plan

1. Classifier table: shared and aggregate/claim rejection, active-window
   selection, date syntax/bounds, malformed/ambiguous/overage/model-only cases,
   unrelated HTTP results and stronger machine-code remedies. Existing tests
   cover header projection, but none consume it in classification.
2. Production loopback engine -> client -> classifier -> gateway recovery ->
   persistence/provenance -> Source/chain projection. Verify one upstream
   attempt, no short-ladder retries, restoration from persisted cooldown,
   later-deadline semantics, Retry-After/Ms precedence, and no header leakage.
   Existing subscription lifecycle coverage exercises only ChatGPT body resets.
3. Reproduce the primary regression on the unmodified implementation before
   fixing it; run focused suites, changed Python Ruff, structure/authority
   guards, and diff checks before pushing. Leave full CI to GitHub.

## Validation evidence

- Pre-fix classifier regression: rejected 5h header plus valid reset returned
  `rate_limited/60`, failing the expected `quota_exhausted/300` assertion.
- Focused classifier, retry advice/policy/lifecycle, engine header boundary,
  resolution, config/authority, structure guards, and scenario catalog:
  **977 passed**. Includes 24 real loopback lifecycle cases across Claude/Codex
  callers and buffered/streaming requests.
- Worktree-local Python 3.12 environment from the frozen lockfile; no package,
  live runtime, external service or real account mutation. Full CI runs in the
  PR; no user/runtime regression operation is authorized for this lane.

## Delivery

Independent branch `fix/claude-cap-reset-2530` from
`c7f55dc62a28a0964db3b0b74571f53899566685`. Non-draft PR, one owner-specific
persistent PR/CI Watch, exact-head Codex pass, CI and zero unresolved threads.
Report Merge-ready to the orchestrator; do not merge or operate a runtime.
