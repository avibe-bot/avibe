# Reply Block Parsing CI Repair

## Evidence and Scope

The first-attempt master run 34018535654 at `e72b9870a` failed only the
reply-enhancer opener stress case and its unit aggregate. Parsing 100,000 open
brackets followed by one valid file link took 11.609 seconds against the existing
10-second assertion. The other 164 tests and 40 subtests in that process passed.
The full file consumed 16.03 seconds of launcher-thread CPU in a 16.19-second
observation interval. This supports inspecting computation, not attributing the
failure to unproven host contention. The failed run is not performance evidence
for a successful workflow.

A local profile of the original input found four CommonMark inline parses:
two attachment scans, each preceded by a block scan which unnecessarily parsed
inline children. The two block consumers inspect only top-level token source
maps, content, type and nesting level. Neither reads children. The profile spent
4.28 of 9.07 seconds in those discarded inline passes. A separate unprofiled
in-process probe measured 3.07 seconds originally and 1.62 seconds with the block
parser's inline rule disabled, with identical reply output. These local timings
are not CI speedup claims.

The orchestrator selects only `core/reply_enhancer.py`, its existing platform
test module, and this plan. Configure the existing dedicated CommonMark block
parser to omit inline children. Keep the actual attachment/code parsers and all
syntax ownership, file policy, destination normalization, masks and offsets.
There is no replacement parser, dependency, retry, timeout or workflow change.

## Contracts and Validation

- Block tokens retain all fields except intentionally unused children, compared
  against the original CommonMark parser across existing block shapes.
- Both block consumers must work while inline parsing on their dedicated parser
  is forbidden. Public reply/strip and media replacement paths remain real.
- Preserve every original test body and the 100,000-opener, 10-second assertion.
- Run the full reply platform and secret-request tests plus dispatcher/media
  consumers, workflow contracts and lint. Require all 17 exact-head CI checks,
  current-head bot PASS, zero unresolved whole-PR threads and clean integration.
- The merged-source CI must independently pass before ordered delivery.

Local validation completed: 520 cases across 11 separate test processes,
including all 167 reply-platform cases and 46 subtests, plus real media and
message-dispatch consumers. Ruff 0.4.9 and the 84-package dependency check pass.
All 181 original function definitions and 455 original unittest assertions are
AST-identical. Re-enabling only the old inline rule makes the new consumer guard
and all six child-elimination subcases fail, confirming that the regressions
detect the discarded computation. The revised profile takes 4.55 seconds with
two inline parses, against the original four in 9.07 seconds.

Complete archived logs of failed run 34018535654 confirm all 405 selected files,
metrics and exit records exactly once, with only the named reply file nonzero.
The separate distribution 21 cases and installer 198 cases passed (96.48 and
104.68 seconds), including the released 3.0.13 upgrade. No CI rerun was requested.

## Review and Residuals

Initial findings-bearing review heads: zero. This is a narrow production
computation repair, not a change to the stress test's acceptance policy.
CommonMark attachment scans remain necessary; further parser rewrites and
arbitrary performance threshold increases are out of scope. CI must confirm
the benefit on hosted runners. Existing installation-chain no-change decision
and all real migration, private database, wheel/sdist, Docker and Windows gates
remain intact.
