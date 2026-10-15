# Python Test Value and CI Performance Audit

This lane audited Python tests under `tests/`, excluding `test_ci_*.py`, from
the shared contract at `e089ae442`. The goal was to remove tests without an
independent contract and reduce measured setup or harness overhead while
preserving real API, authentication, state, migration, and failure semantics.

## Deletions

### `tests/test_memory_removal.py::test_config_write_transaction_loads_test_owned_snapshot`

The test only loaded a freshly saved default config through
`config_write_transaction` and compared one default field. It did not exercise
locking, nesting, concurrent writers, mutation, or fresh state. Those are the
actual transaction contract and remain covered by the primary owner tests:

- `tests/test_config_write_transaction.py::test_shared_config_lock_allows_nested_load_save_transaction`
- `tests/test_config_write_transaction.py::test_transaction_reentrant_with_save`
- `tests/test_config_write_transaction.py::test_transaction_load_and_mutator_wait_for_migration_file_lock`
- `tests/test_config_write_transaction.py::test_transaction_blocks_second_process_until_first_releases`

The retired-memory tests still cover removal from round trips, migration
persistence, concurrent-writer custody, opaque user data preservation, and
legacy shape handling.

### `tests/test_removed_feature_contract.py::test_removed_cli_command_is_unknown`

The test launched the source checkout with `python -m vibe memory status` and
asserted argparse exit code 2. This is the same parser contract already probed
by the installed artifact owner:

`tests/test_distribution_artifacts.py::test_installed_wheel_imports_and_cli_without_memory`

That test imports the built wheel and runs the same unknown command, alongside
the installed-package import and removed API route checks. The distribution
lint/installer job requires this installed-wheel test. The removed HTTP method
matrix and SPA fallback test remain because they cover distinct route and UI
semantics.

## Fixture and harness changes

### Reuse migrated SQLite templates

`test_vibey_agent.py` and `test_claude_output_recovery_ledger.py` now use the
existing `sqlite_db_factory` template facility instead of replaying the full
SQLite importer migration chain for every test. Each test still receives its
own mutable database copy. Vibey tests continue to use the default SQLite
path because the real adapter and message mirror resolve that path themselves;
the module fixture reuses the shared test-owned copy when the autouse seeder
has already created it.

The factory's no-overwrite and isolation guarantees were checked separately
by `tests/test_ci_test_fixtures.py`; that file was not modified.

### Reduce feedback test server shutdown polling

`test_feedback_intake_recovery.py` keeps the real 10-second client network
timeout in both stall cases. Its local HTTP server now runs
`serve_forever(poll_interval=0.01)`, reducing only the time for fixture
shutdown to observe the stop request. No production timeout, barrier, retry,
429 persistence, or recovery assertion changed.

## Measurements

The complete CI attribution files
`/tmp/avibe-ci-attribution/38258683681-metrics.json` and
`/tmp/avibe-ci-attribution/38261750469-metrics.json` contained all 536 files
and successful exits. They identified repeated migration setup and feedback
fixture teardown as actionable costs.

Focused local measurements used isolated uv Python 3.12.

| Focus | Before | After | Result |
| --- | ---: | ---: | --- |
| `test_feedback_intake_recovery.py` | 43 passed, 88.29s pytest | 43 passed, 66.61s pytest | 21.68s faster; stall calls remained 10.30s and 10.33s |
| `test_vibey_agent_context.py` | 27 passed, 121.00s pytest / 134.54s real | 27 passed, 22.30s pytest / 27.22s real | migrated template reused |
| `test_claude_output_recovery_ledger.py` | CI reference: 38.35s PR / 30.02s master | 75 passed, 23.44s local | migrated template reused |
| `test_vibey_agent.py` + `test_vibey_agent_context.py` | — | 157 passed, 281.40s pytest | full focused consumer verification |
| `test_ci_test_fixtures.py` | — | 12 passed, 30.20s | factory consumer contract |

The full changed-test verification also passed:

- `test_memory_removal.py` plus `test_removed_feature_contract.py`: 36 passed
- `test_claude_output_recovery_ledger.py`: 75 passed on an isolated rerun
- `test_feedback_intake_recovery.py`: 43 passed

The first ledger run was concurrent with the removal tests and had one
time-sensitive retry assertion; the isolated rerun passed all 75 tests.

## Retained slow coverage

No deletion was justified in the remaining high-cost files. The auth setup
scenarios and SQLite migration tests exercise durable custody and released
schema transitions. The pairing and scheduled-task suites cover independent
recovery and lifecycle boundaries. The model-hub source-probe matrix varies
protocol, outcome, credential identity, and mutation races; the shell-writer
matrix reaches the real scan and apply boundary with distinct parser roles.
They remain follow-up profiling candidates after the integrated CI run.

