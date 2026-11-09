# CI scope: routed PRs, thin merge groups, full master

Owner decision: November 9, 2026. This replaces the earlier docs-only and
PR/queue affected-routing policy. The merge queue still validates its exact
combined tree, but it does not repeat every test already required on a PR.

## Change contract

- Keep the same nine required contexts; no transitional ruleset change is needed.
- Every PR, **including documentation-only changes**, runs the cheap Linux checks.
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
emits both the mode and a JSON plan. A job's condition reads its own disposition.
The gates also verify the mode against the actual event, not just an output string.

For PR routing, `scripts/ci_docs_only.py` still owns the complete Git diff:
checked-out synthetic merge and both event parents, no rename detection, NUL
framing, no API file-list truncation. Deleted and added sides of a move both
count. Missing parents, unreadable Git objects, an empty/incomplete diff,
non-regular modes, non-default targets, unknown paths, and newly added
non-portable names fail closed. Its standalone CLI remains a diff diagnostic,
not the workflow's authority to skip cheap checks.

| Producer family | PR | MG_THIN | MASTER_FULL |
| --- | --- | --- | --- |
| `changes`, Ruff, UI static/types, Linux artifact build, migration guard | Always | Always | Always |
| `memory-insight-contract`, Show router integration, npm wrapper | Always | Always | Always |
| Lock/manifest/workflow guard, Python collect smoke | Always | Always | Always |
| Eight Python unit shards, Vitest | Always | Out of scope | Always |
| Hosted-wire PR decision job | Always; compare only for an engine-changing PR | Out of scope | Successful no-op; the label decision belongs to the PR |
| Scenario execution evidence | Validate selected producers | Out of scope; no scenario rows attested | Validate every producer |
| Four browser lanes | Existing conservative browser routing | Out of scope | All |
| iOS simulator, desktop shell | Native/contract paths, unknown paths | Out of scope | Both shells, all OS legs |
| Installer/distribution shards, Windows x64 + ARM install smoke | Packaging/install inputs, unknown paths | Out of scope | All |
| Windows agent, Model Hub, control IPC | Python/runtime/test inputs, unknown paths | Out of scope | x64 + ARM where supported |
| Linux/Windows web-fetch | Existing installed-package routing | Out of scope | All |
| Both macOS web-fetch legs | Fetch/runtime/build/dependency inputs; otherwise skip | Out of scope | Both |

The Linux artifact job also builds and verifies the bundled mobile UI. Ordinary
UI work therefore checks that bundle without allocating a simulator. Native
bridge inputs (`ui/src/lib/native*`, `ui/src/lib/useNative*`, `ui/src/mobile/`),
iOS sources, message/catalog contracts, and the bundled mascot select iOS.
Desktop sources, native bridge inputs and i18n contracts select desktop.

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

## Required contexts and fail-closed aggregation

The six required leaf names remain substantive checks:

`ruff`, `show-router-integration`, `memory-insight-contract`,
`migration-release-guard`, `npm-wrapper`, `build-linux-artifacts`.

The three existing required aggregate names remain:

- `unit-tests`: unit shards, scenario evidence, UI static/types, Vitest, browser,
  hosted-wire, manifest guard, Python smoke, iOS and desktop.
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
- Tracked JSON/YAML syntax, JSON Schema dialect validation, pinned actionlint
  for the CI/release workflow boundary, and repository workflow-contract tests.

The dependency-free npm launcher has an explicit no-lock contract. Two existing
TypeScript JSONC configs remain owned by `tsc`. Actionlint 1.7.12 has one exact
`publish.yml` compatibility exception for GitHub's newer `concurrency.queue`;
an independent guard validates that field. Other unknown keys still fail.
Caches contain downloaded dependencies, never permission to rewrite a lock.

`python-smoke` installs locked dependencies and runs `pytest --collect-only`
for the unit selection under the Linux loopback-only wrapper. It catches import,
collection and shared-schema combinations without executing the full unit suite.

## Full master backstop and releases

`lint.yml` triggers on `push: master` and supports a manual full run on master.
Its concurrency group is shared by those two event types, with
`cancel-in-progress: false` for master. This coalesces the **whole pass**,
including iOS/desktop/web-fetch called workflows; they have no separate event
triggers or concurrency groups. PR supersession still cancels only that PR.
Immutable merge groups do not cancel one another.

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

Executed by `tests/test_ci_scope.py`. Cheap Linux checks run for every row.
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

The owner will change merge-queue **build concurrency from 2 to 4 after this PR
merges**. Keep the nine contexts and current merge-size rules unchanged. The
thin profile requests about eight hosted Linux jobs plus one self-hosted npm
job after classification: four active groups can require about 32 hosted Linux
slots, in addition to PRs/full master; aggregators add short dispatches. Effective
runner limits must be observed, not inferred from a published default quota.

Watch the first real enqueue → MERGED samples, MG CI p50/p90, per-job
`created_at → started_at` runner wait (especially Linux p90), PR/full-master
wait and full-backstop completion latency. Compare against the historical
Linux MG wait p90 of 1.15 minutes. If four groups increase waits enough to erase
the latency gain, reduce concurrency rather than weaken checks. The owner
measures actual queue latency and owns the ruleset change; this PR changes no
ruleset, queue admission, tag, release or deployment.
