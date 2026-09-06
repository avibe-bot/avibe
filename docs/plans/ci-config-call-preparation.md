# Config Save Test Preparation

## Diagnosis and Scope Decision

This is phase three of the approved order: stability, build/install preparation,
then repeated work inside test calls. PRs #1905 and #1906 are merged and their
exact master sources passed all 17 gates. The latter passed 405 unit files with
one exit and metrics record each, 21 distribution contracts and 196 installer
cases in 375 seconds. No stable whole-workflow speed guarantee follows from it.

The config-save module spent 31.84, 34.64, 33.91 and 39.87 seconds in four green
CI runs, almost entirely in pytest's call phase. A local profile found 41 full
migration calls taking 14.32 of 22.53 seconds, reached through 112 real
ensure_sqlite_state calls. This is repeated preparation inside the business
call, not expensive merge logic. The existing exclusive raw-schema copy factory
can remove that repeated DDL without replacing the business call or importer.

The orchestrator inspected the whole test inventory, save_config, its config
lock, guild-scope persistence, importer and default-Agent consumers. Opt in only
26 ordinary preference, redaction, validation and serialization definitions
(30 expanded cases). Add one explicit raw-schema copy after their existing
per-test home selection. Do not add global autouse behavior, change the AST
opt-in detector, or use the fully imported default template. Bootstrap, legacy
conversion, persisted recovery, session initialization and lock-fresh-base tests
keep their original preparation. All original assertions and business calls
remain unchanged; so do real historical migration and locking suites.

An uncommitted selection probe passed all 96 cases in 6.73 seconds; a following
unmodified run in the same environment passed in 12.49 seconds. An earlier
unmodified sample took 24.52 seconds, so local timings also vary. These probes
justify the scope, not a claimed CI speedup.

## Contract

Each copied database starts with only the real migration schema, no importer or
default-Agent state. It is a closed, checkpointed, exclusive file under that
test's private home. Existing factory contracts compare the entire schema,
rows, persisted pragmas, integrity and independent mutations, and reject
overwrites or out-of-home paths.

A new fresh-versus-prepared config-save contract observes and delegates to the
original importer write boundary. Both paths must perform the real import,
persist guild scopes, create every enabled default Agent and retain those
results across a partial save. It also asserts the module still does not opt
into the global imported-template fixture. No production methods are replaced
by fake results. No dependency, timeout, workflow, runner or selection changes.

## Verification and Gates

Run all config-save cases normally, compare all original AST bodies and
assertions after removing only explicit fixture plumbing, and run factory,
workflow, shard, settings, Agent and real-migration consumers in separate Python
processes. Require lint and dependency consistency, full exact-head bot review,
all 17 checks in every exact-head lint run, zero whole-PR unresolved threads and
clean scope/integration before guarded squash. Verify merged-source CI again.
Keep all existing durable delivery watches through the final ordered outcome.

Initial findings-bearing review-head inventory is zero. No new identifiers or
cross-lane contracts are allocated. Actual CI phase and write-volume comparisons
must use complete first-attempt green logs; no speculative shard rebalance.

Local final code passed all 98 config cases (16.10 seconds in its fresh private
environment), plus 233 separately run fixture, workflow, shard, real-migration,
settings and default-Agent consumer cases. All 72 original definitions and 251
assertions are AST-identical after normalizing only explicit preparation calls
and fixture parameters. Ruff 0.4.9, all 84 installed package requirements and
whitespace checks pass. The earlier probe timing is not the final suite timing.

Before the first push, master advanced through #1907 (b930160ed), which changes
Model Hub routing UI only. Its drawer and consuming tests were inspected; no
overlap exists with this two-file scope, database initialization or identifiers.
Integrate that source before opening this independent PR and require its full CI.
After that fast-forward, all 98 config cases passed again in 6.95 seconds;
the original-body AST comparison, lint and whitespace checks remained green.
