# Phase 1: lazy CLI entry

## Change contract

Make ordinary static CLI parsing and public version output independent of runtime,
API, scheduling and database modules. Keep the existing parser, command functions,
JSON encoding, stdout/stderr selection and exit codes. Selected commands import
their actual dependencies at the function that consumes them, rather than loading
every command's dependencies at startup.

Identity resolution, command floors, invocation context reset, placement checks
and resource checks retain their existing decisions. No readiness/importer,
read-only-engine or warm-service changes belong to this phase. The branch started
independently from master. Before final validation, it was synchronized without
history rewriting to master `42e510d6a3dce1cfb90e921762cbd547089da2b5`, which
contains the merged Phase 2 readiness probe (#3306). Final Phase 1 benchmarks
compare against that exact base, with readiness present on both sides; this PR's
diff does not change the readiness/importer contract.

### Existing-contract exception

The no-database guarantee covers ordinary static argparse help, version and
argument errors. Raw MCP and internal activation entry points keep their current
admission-before-parsing order. MCP tool help also needs controller-owned server
and scope information. A remote invocation on those paths may read durable
identity binding state before printing help or refusing malformed arguments.
Changing that precedence would change authorization/error behavior and is not
part of this refactor. Bare `vibe` and bare `remote` remain real commands.

## Implementation

- Keep handlers and parser in `vibe/cli.py`; use ordinary function-local imports,
  postponed annotations and light canonical shared constants.
- Remove eager package-facade dependencies from `config`, `storage` and
  `modules.agents` without dropping their existing public exports.
- Keep executable-path caching light and shared with upgrade callers. Restart
  state vocabulary also has a light canonical owner; Doctor's existing finite
  projection and its producer/retention/display tests remain unchanged.
- Preserve raw retention-config help without loading or migrating V2Config.
- Retarget tests using incidental `cli` import aliases to the actual dependency
  owner; preserve real helper boundaries such as `_task_store`. No test-only
  production hooks or compatibility namespace.

## Validation

The new cold-entry subprocess contract protects the observable ability to ask
for static help/version or receive an argument error without a database/runtime.
Its credible regression is an eager import or authority lookup before parsing.
Existing in-process parser tests miss this because test collection has already
loaded product modules. The subprocess forbids heavy imports and SQLite opens
before importing the entry point, using test-owned home/config directories.

Selected-command subprocess coverage additionally rejects a lazy entry that
simply loads the old global dependency graph after parsing. Existing parser,
error-envelope, identity-provenance, context-reset, MCP and activation tests
remain the primary coverage for their respective contracts.

- [x] Demonstrate cold-entry contract fails on the pre-fix source: eager
      `apscheduler` import is rejected before the first command.
- [x] Implement light entry dependencies and local handler imports.
- [x] Retarget accidental test aliases and run focused regressions/lint:
      2,043 cases passed across five focused invocations, including 57 cold-entry,
      parser-tail and selected-command cases. Those 57 cases pass on Python 3.12
      and 3.14; only stdlib-controlled usage whitespace is normalized, while
      usage tokens and the rest of each error envelope remain exact. The
      Show/Watch bundle required a
      process-local `ulimit -n 4096` after macOS's default descriptor limit was
      exhausted; the isolated rerun passed all 239 cases. No product change was
      made for that test-process limit.
- [x] Integrate merged master `42e510d6a` without history rewriting. After the
      sync, 163 focused cold-entry/readiness/startup/authority/pagination cases
      pass on Python 3.12, and all 57 cold-entry cases pass again on Python 3.14.
- [ ] Benchmark exact candidate head through the coordinated phase harness:
      cold timings; 200-call / 20-way mixed load; lock errors; transcript latency;
      rollback. Benchmark owner coordinates runs; no production state.
- [ ] Exact-head review and CI; report candidate to parent for final verification.

## Delivery boundary

This lane never merges, restarts or deploys. An open PR is not Merge-ready until
benchmark evidence, exact-head review, CI and unresolved-thread checks are
complete. Parent Session owns the final verification and reporting decision.
