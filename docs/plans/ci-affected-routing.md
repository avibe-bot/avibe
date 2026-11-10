# CI scope: routed PRs, thin merge groups, full master

Owner decision: November 9, 2026. This replaces PR/queue affected routing while
preserving the narrative-only PR fast path. The merge queue still validates its
exact combined tree, but does not repeat every test already required on a PR.

## Change contract

- Keep the same nine required contexts; no transitional ruleset change is needed.
- Proven narrative-only PRs keep their existing fast path: only classification,
  dependency-free repository-guidance checks and skip-verifying aggregates.
  They do not run unit tests, UI checks, builds, manifest checks or collection.
  Other PRs run the cheap Linux checks.
- A merge group runs only the combination-sensitive fast checks, targeting about
  five minutes p50 CI wall time. This is not a five-minute enqueue-to-merge promise.
- Master runs the full suite after landings. A running pass finishes; only the
  newest pending master pass waits. Release builds require exact-SHA full-green proof.
- No test result is reused from a different SHA. No lock is regenerated in CI.
- Unknown paths and uncertain diff classification select all heavy **PR** jobs.
  `MG_THIN` is a deliberate mode-level exclusion, not a claim of path independence.

## One scope authority

`scripts/ci_scope.py` owns the literal modes `PR`, `MG_THIN`, `MASTER_FULL`, the
complete producer disposition map, and required-gate validation. `changes`
emits the mode and a JSON plan with an explicit boolean `docs_only`. A job's
condition reads its own disposition. The gates verify the mode against the
actual event, not just an output string. Only a proven narrative-only PR may
set `docs_only`; neither queue nor master can inherit that shortcut.

For PR routing, `scripts/ci_docs_only.py` still owns the complete Git diff:
checked-out synthetic merge and both event parents, no rename detection, NUL
framing, no API file-list truncation. Deleted and added sides of a move both
count. Missing parents, unreadable Git objects, an empty/incomplete diff,
non-regular modes, non-default targets, unknown paths, and newly added
non-portable names fail closed. Its standalone CLI remains a diff diagnostic,
not the workflow's authority to skip cheap checks.

Except for `changes`, the PR column below describes **non-narrative PRs**;
proven narrative-only PRs explicitly skip every producer.

| Producer family | PR | MG_THIN | MASTER_FULL |
| --- | --- | --- | --- |
| `changes` and narrative-only guidance check | Classification always; guidance on narrative-only PRs | Classification | Classification |
| Ruff, UI static/types, Linux artifact build, migration guard | Always | Always | Always |
| `memory-insight-contract`, Show router integration, npm wrapper | Always | Always | Always |
| Lock/manifest/workflow guard, Python collect smoke | Always | Always | Always |
| Eight Python unit shards, Vitest | Always | Out of scope | Always |
| Hosted-wire PR decision job | Always; compare only for an engine-changing PR | Out of scope | Successful no-op; the label decision belongs to the PR |
| Scenario execution evidence | Validate selected producers | Out of scope; no scenario rows attested | Validate every producer |
| Four browser lanes | Existing conservative browser routing | Out of scope | All |
| iOS simulator | Native/contract/unknown paths; separate **advisory** workflow until #3418 is fixed | Out of scope | Required |
| Desktop shell | Native/contract paths, unknown paths; required | Out of scope | All OS legs |
| Installer/distribution shards, Windows x64 + ARM install smoke | Packaging/install inputs, unknown paths | Out of scope | All |
| Windows agent, Model Hub, control IPC | Python/runtime/test inputs, unknown paths | Out of scope | x64 + ARM where supported |
| Linux/Windows web-fetch | Existing installed-package routing | Out of scope | All |
| Both macOS web-fetch legs | Fetch/runtime/build/dependency inputs; otherwise skip | Out of scope | Both |

The Linux artifact job also builds and verifies the bundled mobile UI. Ordinary
UI work therefore checks that bundle without allocating a simulator. Native
bridge inputs (`ui/src/lib/native*`, `ui/src/lib/useNative*`, `ui/src/mobile/`),
iOS sources, message/catalog contracts, and the bundled mascot select iOS.
Desktop sources, native bridge inputs and i18n contracts select desktop.

**iOS remains advisory on PRs:** NavigationPolicyTests #3418 is not fixed. The
November 6–9 sample has 13 NavigationPolicy failures across 109 queue trees;
making it required now would frequently block native PRs. `ios-shell.yml`
therefore runs affected PRs independently using `ci_scope.py --optional-ios`,
with real red results (no `continue-on-error`), but no required aggregate waits
for that advisory workflow. The main lint iOS producer is explicitly out of PR
scope. It still runs and must pass in `MASTER_FULL`, so failed native validation
cannot qualify a release. Once #3418 is fixed, return the affected PR iOS call
to lint and retire its separate PR trigger. Desktop remains required on affected
PRs now. This is a quarantine decision, not a flake fix.

Shared CI scripts/workflows, root manifests/locks, installer entry points,
unknown roots, and UI build configuration/plugins are conservative full-PR
inputs. Python/runtime roots remain broad Windows inputs: a Linux pass does not
establish Windows handles, process, filesystem, or ARM behavior. Mac fetch
selection is narrower than other platform checks; it is not a blanket macOS
exemption. The explicit path predicates live in `ci_scope.py`, not duplicated
workflow-level `paths` filters.

The browser boundary is unchanged: sparse checkout contains exactly `ui/`,
`vibe/message_types.json`, `vibe/data/`, the bundled mascot, and `Dockerfile`.
The existing classifier excludes build configuration and non-portable added UI
filenames from the installed-package skip allowlist.

### Parallel UI static checks

The November 10 static-check optimization keeps the `ui-checks` producer and
all its callers, but runs two independent matrix legs on existing hosted Linux
runners. `core` runs theme validation, lint, test/i18n types and design types;
`e2e` runs the existing all-suite browser-fixture type checker. Each command runs
exactly once per selected workflow, with the same package lock, Node version,
compiler and flags. Each runner performs its own `npm ci`.

The E2E checker already uses up to four CPU workers; its worker count and suite
coverage do not change. The matrix has `fail-fast: false`, no permissive error
handling and no serial concurrency limit. The `unit-tests` gate and full-master
certificate wait for the entire matrix and still reject a failed, cancelled or
unexpectedly skipped producer. Narrative-only PRs still skip the whole producer;
queue/full-master routing, browser execution, result reuse and release gates do
not change.

Before this split, twelve recent completed thin queue runs on November 10 had a
4m18s median workflow-created-to-final-job time (3m35s–5m32s); all twelve were
limited by UI static validation. This is not enqueue-to-merge time. For example,
run `42748505399` spent about 94s in lint followed by 146s in E2E type checking,
with only 9s installing dependencies. The split removes that serialization at
the cost of one extra hosted runner and dependency install, not fewer checks.
Compare real post-change PR/queue timings before claiming a speedup; a roughly
three-minute queue median is a target, not measured evidence.

The workflow coverage contract in `tests/test_ci_pipeline.py` expands each
matrix leg's actual command expression, protecting against omitted, duplicated
or unused commands. Existing `tests/test_ci_scope.py` negative cases continue to
cover producer failure, cancellation, missing results and wrong-mode skips.

## Required contexts and fail-closed aggregation

The six required leaf names remain substantive checks:

`ruff`, `show-router-integration`, `memory-insight-contract`,
`migration-release-guard`, `npm-wrapper`, `build-linux-artifacts`.

The three existing required aggregate names remain:

- `unit-tests`: unit shards, scenario evidence, UI static/types, Vitest, browser,
  hosted-wire, manifest guard, Python smoke, the six required leaves, desktop,
  and the main iOS producer (explicitly skipped on PR/MG, required on master).
- `install-upgrade-regression`: installer/distribution shards.
- `windows-install-smoke`: x64/ARM installation, all selected web-fetch legs,
  Windows agent, Model Hub and IPC.

Every in-scope producer must report **exactly `success`**. Every explicitly
out-of-scope producer must report **exactly `skipped`**. A missing/unknown mode,
route, dependency or result, cancellation, unexpected skip, or unexpected run
fails. Gates run with `always()` so a skipped producer cannot skip its gate.
Matrix and reusable-workflow failure/cancellation must propagate; no job uses
`continue-on-error` to manufacture a pass.

Scenario evidence receives the same mode and plan. Each selected JUnit producer
must leave its exact matrix artifact count. Missing reports are not treated as
path-skipped jobs; unexpected reports from out-of-scope producers fail. PR-only
omissions are labeled **out of scope**, not passed. `MG_THIN` skips the entire
scenario evidence job and explicitly attests no rows. `MASTER_FULL` has no
out-of-scope producers; release-only cases keep their separate release gate.

## Combination checks

`manifest-consistency` runs real read-only resolvers:

- `cargo metadata --locked --format-version 1` for the desktop workspace,
  including workspace member manifests. Do not add `--no-deps`: it can miss a
  stale lock. This is resolution, not native compilation/linkage.
- `npm ci --ignore-scripts --dry-run --no-audit --no-fund` for UI and desktop.
- `uv lock --check`; `--frozen` alone would skip the freshness check.
- Before/after manifest and lock hashes, including failed resolver invocations.
- Tracked JSON/YAML syntax, JSON Schema dialect validation and tracked companion
  data/schema consistency, pinned actionlint for the CI/release workflow boundary,
  and repository workflow-contract tests.

The dependency-free npm launcher has an explicit no-lock contract. Two existing
TypeScript JSONC configs remain owned by `tsc`. Actionlint 1.7.12 has one exact
`publish.yml` compatibility exception for GitHub's newer `concurrency.queue`;
an independent guard validates that field. Other unknown keys still fail.
Caches contain downloaded dependencies, never permission to rewrite a lock.

`python-smoke` installs locked dependencies and runs `pytest --collect-only`
for the unit selection under the Linux loopback-only wrapper. It catches import,
collection and shared-schema combinations without executing the full unit suite.

Required queue API reads use the existing `release_asset_download.py` transport
policy: at most three attempts, 1/2-second exponential backoff with up to 0.5s
jitter, and one 30-second retry window. The predecessor's `gh api` listings
(including the final freshness check) and wheel download share this policy;
each CLI call also has a hard remaining-wall-time timeout. A failed paginated
response is discarded, and wheel retries overwrite only the new temporary
probe directory's partial file before mandatory digest validation.

The other required-queue API consumer, Show/runtime metadata and asset lookup
in `build-linux-artifacts`, already uses this same transport. There is no extra
cache, nested retry loop, whole-job retry, or fallback to an older predecessor.
Only transient network errors, 5xx, 429 and identified secondary limits qualify.
Ordinary 4xx, unknown CLI errors, malformed successful JSON, digest failures and
SQLite contract/freshness failures do not retry. Server `Retry-After` hints take
precedence; a direct GitHub rate limit without a hint requires at least a minute,
so it fails clearly outside this short budget rather than retrying too early.
Existing unauthenticated public-asset fallback is retained, but cannot replay
a throttled API during its own retries. For streaming assets the window limits
retries, not a healthy large transfer;
individual socket inactivity timeouts remain bounded by the remaining window.

## Full master backstop and releases

`lint.yml` triggers on `push: master` and supports a manual full run on master.
Its concurrency group is shared by those two event types, with
`cancel-in-progress: false` for master. This coalesces the **whole pass**,
including iOS/desktop/web-fetch called workflows; none has an independent master
or merge-group trigger. The advisory iOS PR trigger has its own PR cancellation
group; full master calls use a unique run ID and are never independently
cancelled. PR supersession cancels only that PR. Immutable merge groups do not
cancel one another.

`master-full-backstop` checks every producer and all three required aggregates
in `MASTER_FULL`. `release_ai.yml` and `publish.yml` resolve the requested tag
(including annotated tags and dispatch overrides) to its exact source SHA and
refuse all build/sign/publish paths without a successful full master `lint.yml`
run plus that terminal certificate. PR, merge-group, failed, cancelled, missing,
or another SHA's runs do not qualify. Historical full runs without this explicit
certificate are not assumed equivalent.

A SHA coalesced out of the pending queue is **not releaseable** merely because a
later master is green. Release a full-green SHA; wait for its in-progress full
run or rerun its existing master backstop. No tag/publish operation is performed
by this CI change.

**Fix-forward owner:** the author of the landing that first made master red owns
triage and the fix (release owner `cyhhao` assigns an owner if attribution is unclear).
Pause release/promotion at the last full-green master; for a confirmed product
regression, pause ordinary auto-enqueue until the fix is ready. A moved failure
is later feedback, not a repaired flake or permission for unbounded reruns.

## Representative dry run

Executed by `tests/test_ci_scope.py`. Cheap Linux checks run for every row
**except narrative docs**, which retain the fast path. The iOS column describes
the separate advisory PR run; desktop and all other selected jobs are required.
`W` means Windows agent/Model Hub/IPC; installation is a separate column.

| Representative diff | Browser | iOS | Desktop | Installer + Win install | W | Linux/Win fetch | Mac fetch |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ui/src/App.tsx` | Run | Skip | Skip | Skip | Skip | Skip | Skip |
| `core/controller.py` | Skip | Skip | Skip | Skip | Run | Run | Skip |
| `ios/Avibe/NavigationPolicy.swift` | Run | Run | Skip | Skip | Skip | Run | Skip |
| `desktop/src-tauri/src/main.rs` | Skip | Skip | Run | Run | Skip | Run | Run |
| `pyproject.toml` / `install.ps1` | Run | Run | Run | Run | Run | Run | Run |
| `docs/guide.md` | Skip | Skip | Skip | Skip | Skip | Skip | Skip |
| `.github/workflows/lint.yml` | Run | Run | Run | Run | Run | Run | Run |
| Unknown new root or unclassifiable diff | Run | Run | Run | Run | Run | Run | Run |

Every row uses the same thin queue profile and the same full master profile.
The iOS row retains conservative existing browser/platform routing; no extra
path-independence claim is introduced just to make the table cheaper.

## Timing evidence and rollout

November 6–9 sample: 109 queue trees, 89 completed enqueue-to-merge histories.
Measured MG unit shards were **6.60/8.31 minutes p50/p90** and Vitest
**8.13/9.14**. Wider sharding has not demonstrated a five-minute combined queue;
therefore both stay on PR/master, not MG. Retained UI static is **4.67/5.12**,
Linux build **1.32/1.50**, migration guard **2.03/2.42**.

Historical DAG replay, retaining per-job scheduling delays and terminal gate
overhead, budgets one parallel minute each for the new guard and collection:

| Measurement | Before p50/p90 min | Projected p50/p90 min |
| --- | --- | --- |
| Thin MG required CI, 109 trees | — | **5.18 / 5.85** |
| Paired final successful-attempt CI, 89 landings | 15.37 / 19.69 | **5.22 / 5.84** |
| Same final enqueue → MERGED, keeping observed waits | 16.55 / 27.31 | **5.95 / 14.77** |
| First enqueue bounded replay, preserving remaining recovery gaps | 17.23 / 34.47 | **5.83 / 11.25** |

Two minutes for each new parallel job leaves the replay's wall percentiles
unchanged because static UI still dominates. New mobile-bundle work, checkout
steps in gates, cache misses and changed runner contention are not measurements
in that replay. Do not add percentile components or present these estimates as
post-change results. Eleven of thirteen historical failed-check evictions lose
their blocking cause; two artifact-build failures remain. This does not prove
that no cross-PR interaction can escape the thin profile.

The rollout raised merge-queue **build concurrency from 2 to 4**. Keep the nine
contexts and current merge-size rules unchanged. With the UI static split, the
thin profile requests about nine hosted Linux jobs plus one self-hosted npm job
after classification: four active groups can require about 36 hosted Linux
slots, in addition to PRs/full master; aggregators add short dispatches. Effective
runner limits must be observed, not inferred from a published default quota.

Watch the first real enqueue → MERGED samples, MG CI p50/p90, per-job
`created_at → started_at` runner wait (especially Linux p90), PR/full-master
wait and full-backstop completion latency. Compare against the historical
Linux MG wait p90 of 1.15 minutes. If four groups increase waits enough to erase
the latency gain, reduce concurrency rather than weaken checks. The owner
measures actual queue latency and owns the ruleset change; this PR changes no
ruleset, queue admission, tag, release or deployment.
