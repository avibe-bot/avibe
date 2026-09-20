# Desktop / master integration contract

## Goal and authorization

The owner requested resolving the merge conflicts in avibe-bot/avibe PR #2062 (`desktop` into `master`) on 2026-09-20. Integrate the current mainline into the existing desktop lineage, preserve both sets of intended behavior, and update that PR. Merge into master, production operations, signing setup, and desktop publication are not authorized by this task.

The orchestrator is Avibe Session `sessjyd34p9rp`. One implementation lane owns the integration; do not split shared Runtime contracts across concurrent writers.

## Source snapshots and workspace

- Initial desktop head: `92c600aa78f51e3b9ec48f185e5150a4f2ef3163`.
- Initial master head: `4de519cce2f7254325a88f652b31f510a7a8ff70`.
- Branch: `chore/desktop-master-integration-20260920`, based on origin/desktop.
- Worktree: `/Users/max/workspace/ai/avibe/.worktrees/avibe/desktop-master-integration-20260920`.
- Existing PR: https://github.com/avibe-bot/avibe/pull/2062.
- The primary checkout and other desktop worktrees belong to other tasks and must remain untouched.

Use a real merge of origin/master into this desktop-based integration branch. Preserve the desktop ancestry and push the resulting descendant to origin/desktop with a normal non-force push. Do not cherry-pick desktop onto a new mainline branch or replace the assigned PR. Recheck the remote desktop head before pushing; reconcile unexpected movement instead of overwriting it.

## Behavior invariants

1. Mainline Web/IM orchestration, async UI/API handling, authorization, persistent state compatibility, and current backend/Model Hub behavior remain effective after integration.
2. Desktop retains its local native shell, self-contained Runtime installation, lazy backend installation, startup/ownership receipts, Windows IPC, tray lifecycle, native links, window restoration, and native notifications.
3. The shell remains the native capability owner; remote/Workbench pages gain no native privilege. User state stays separate from immutable packaged Runtime contents.
4. Shared behavior has one owner. Adapt desktop-specific callers to current shared interfaces when possible; do not restore obsolete mainline paths solely to satisfy stale desktop tests.
5. Successful backend config persistence uses the mainline rolling refresh path; UI request paths stay async and do not add per-request event-loop bridges.
6. On-disk released shapes retain migration or safe-degradation behavior. No destructive state changes, speculative architecture rewrite, or removal of a capability to make the merge clean.
7. Conflict resolution is semantic: automatically merged files and tests must be checked for interface drift too. No blanket ours/theirs strategy.

## Initial conflict inventory

The initial non-checkout merge probe reported 27 paths:

- AGENTS.md
- core/internal_server.py
- core/show_runtime.py
- modules/agents/codex/agent.py
- tests/test_claude_cli_path.py
- tests/test_cli_setup_status.py
- tests/test_internal_server.py
- tests/test_local_deps.py
- tests/test_restart_supervisor.py
- tests/test_ui_api.py
- tests/test_ui_server_install.py
- tests/test_ui_show_pages.py
- tests/test_upgrade_flow.py
- tests/test_vibe_cli.py
- ui/src/components/VersionBadge.tsx
- ui/src/components/settings/SettingsDependenciesPage.logic.ts
- ui/src/components/settings/SettingsDependenciesPage.test.ts
- ui/src/components/settings/SettingsDependenciesPage.tsx
- ui/src/components/settings/shared/useBackendRuntime.ts
- ui/src/context/ApiContext.tsx
- vibe/api.py
- vibe/cli.py
- vibe/internal_client.py
- vibe/model_hub_client.py
- vibe/restart_supervisor.py
- vibe/runtime.py
- vibe/ui_server.py

The lane may edit these files, existing desktop-delta paths, and directly consuming callers/tests required to preserve the invariants. It may update this plan and the PR description. Any broader architecture change or unrelated feature is outside scope and goes to the orchestrator with evidence.

## Validation and safety

Read the current master AGENTS.md, the workspace AGENTS.md, and the canonical pr-delivery-loop skill; the desktop copy may be old. The owner's current acceptance target is the designated cloud test instance per workspace conventions, but this task does not authorize deploying to it. Use hermetic local tests by default. Do not restart the installed local service, touch real user config/token stores, create the retired local Incus environment, or run write-capable production tests.

- Compare desktop and mainline contracts before choosing each conflict resolution; record the significant choices below.
- Run focused Python tests for Runtime, desktop lifecycle/backends, IPC, readiness, API/config reconciliation, and any changed shared subsystem; adapt existing scenario coverage where its contract is affected.
- Run focused frontend tests for merged backend settings/API context/version behavior and `npm run build` in ui/.
- Run desktop bootstrap localization/build and relevant Rust Runtime/shell checks as supported locally. Let GitHub desktop-shell cover its macOS and Windows matrix; report any local limitation accurately.
- Run Ruff on changed Python files before each push and inspect the full diff for conflict markers, accidental mainline reversions, debug files, or obsolete paths.
- Required PR workflow families: lint and desktop-shell. Verify expected individual checks, current-head Codex pass, and zero unresolved threads before handing back.

## Review and delivery

Load pr-delivery-loop and background-watch-hook. One durable lane-owned combined PR/CI Watch must cover PR #2062 using a separate cursor from the orchestrator's existing Watch. Seed only once before the first pushed head or explicit review trigger, use --forever and --timeout 0 at both layers, and do not reseed between rounds.

A bot review of the original head was pending when this task began. Do not push while a prior-head review is still in flight. Resolve/reconcile its findings against the integrated candidate. Follow the circuit breaker: repeated root-cause class on two reviewed heads, or three findings-bearing heads after a model rewrite, pauses editing for orchestrator diagnosis. Fetch all paginated threads and report the full inventory. Existing review authority is not replaced by a local reviewer.

Read back every PR-body/comment change. Hand back PR/head, source parent SHAs, significant resolution decisions, test evidence, CI/review status, residual acceptance checks, and any active Watch ID. Do not merge into master or publish.

## Decisions and evidence

- 2026-09-20: Use one implementation lane and one merge commit lineage because the conflicts cross shared Runtime/UI boundaries. Preserve current master architecture and adapt desktop contracts within it.
- 2026-09-20: Resolve shared Python/API/UI conflicts by keeping the current
  `master` async FastAPI, rolling backend refresh, dependency reconciliation,
  Model Hub/Memory projections, CLI authority, and Show Runtime provider/file
  lock contracts. Adapt desktop callers and fixtures to those interfaces rather
  than restoring retired Flask, per-request event-loop, GitHub Show Runtime, or
  in-process install-lock paths.
- 2026-09-20: Preserve desktop Runtime-host ownership as two facts: launch
  attempts continue to deduplicate retries, while a completed startup receipt
  remains scoped stop authority until receipt refusal or confirmed Runtime loss.
  Readiness timeout releases only the completed helper retry slot. Handover is
  authorized only by the exact five-field identity-mismatch payload containing a
  valid 64-character lowercase hexadecimal desktop predecessor identity; both
  initial and polling consumers enforce the same boundary.
- 2026-09-20: Keep the master's `doctor` validation and bare `doctor` behavior
  by validating individual repair targets with a custom argparse type. This
  avoids argparse's empty-list `choices` failure without accepting unknown
  targets. The live-service receipt fixture now supplies controlled process
  identities and creation times rather than reading local PIDs or state.

### Validation evidence before the integration commit

- Python integration boundary and desktop/runtime/API/UI tests:
  `2123 passed, 5 skipped` across the focused Runtime, desktop, IPC, API,
  backend, CLI, upgrade, internal-server, dependency, restart, and Show Runtime
  suites.
- Rust Runtime-host: `cargo test --lib` (`92 passed`), bootstrap integration
  (`25 passed`), notification HTTP (`5 passed`), and `cargo fmt --check` passed.
  The desktop workspace `cargo fmt --all -- --check` and
  `cargo clippy --workspace --all-targets -- -D warnings` also passed.
- Frontend: merged settings/API-context focused tests `81 passed`,
  `npm run typecheck:tests`, and `npm run build` passed. Desktop bootstrap
  `npm run test:i18n` and `npm run build` passed.
- Changed Python Ruff check passed with `uvx ruff check`; Python compile and
  whitespace checks were run during reconciliation. The repository's existing
  `patches/cliproxyapi/native-intent.patch` carries intentional patch-format
  whitespace diagnostics under a full `git diff --check`; no conflict markers
  remain in source files.
- The original-head Codex review inventory remains two distinct findings on
  `92c600aa78`: predecessor identity authorization and receipt ownership across
  readiness timeout. Both have consuming regression tests and no repeated class
  or circuit-breaker trigger at this point. The exact-head review and required
  GitHub CI remain post-push gates.

### Post-push mainline synchronization note

- 2026-09-20: After pushing `007f631613be43b3d00918c9e1e3cde58235099d`, the
  latest remote `master` was independently verified with `git ls-remote` as
  `162fda5942461941942536355130477372739c36`. It includes the native
  credential takeover integration from PR #2060, which intersects the shared
  Model Hub/auth/backend lifecycle boundaries in this merge.
- The repository's `remote.origin.fetch` contains only unrelated branch-specific
  refspecs. A plain `git fetch origin master desktop` updates `FETCH_HEAD` but
  does not refresh `origin/master` or `origin/desktop`. Subsequent source refresh
  must use explicit refspecs such as
  `refs/heads/master:refs/remotes/origin/master` and
  `refs/heads/desktop:refs/remotes/origin/desktop`, or pin the SHA returned by
  `git ls-remote`. The shared fetch configuration must remain unchanged.
- A non-checkout merge-tree probe of `162fda5942` with the current candidate
  returned a clean tree, but textual cleanliness does not establish semantic
  compatibility. After the current-head review is terminal, merge this exact
  master SHA normally, inspect native credential takeover against desktop IPC,
  API, auth, and backend rolling-refresh ownership, and rerun focused consumers,
  frontend build, and changed-Python Ruff before the one subsequent push.

### Circuit-breaker ruling and bounded follow-up

- 2026-09-20: The Codex review inventory contains two findings-bearing heads:
  `92c600aa78` (review `5259095413`) and `007f631613` (review `5259460941`).
  The predecessor-identity finding was fixed on the first candidate. The
  launch-provenance class repeated on the second candidate as
  `4056057500`/`PRRT_kwDOPbFPYs6kG0NV`, so the review-loop circuit breaker
  paused edits until the orchestrator diagnosed the complete class.
- The diagnosis is that retry deduplication, scoped stop authority, and
  evidence that a Runtime may still be alive have different lifetimes. The
  bounded fix keeps one `LaunchState` owner and adds only minimal in-memory
  liveness evidence. A helper timeout, successful reused outcome, missing
  receipt, helper failure, receipt refusal, or recovery reset is not proof that
  a previously launched or reused Runtime is absent. Receipt refusal revokes
  stop authority; it does not turn uncertain liveness into `Inactive` removal.
  Unknown liveness must continue to reach `RuntimeRemovalState::Unknown`, where
  the bundled launcher refuses destructive deletion. A definitive safe stop or
  completed removal may clear the evidence.
- The lifecycle contract to preserve is:

  | Launch/watch outcome | Readiness/retry state | Stop authority | Removal decision |
  | --- | --- | --- | --- |
  | pending | retain attempt; no overlapping helper | none | `Unknown` if no external readiness proves otherwise |
  | started receipt | timeout releases only completed retry slot; late readiness may be adopted | scoped stop remains valid until refusal or confirmed loss | `Managed` while the owned Runtime is active; unknown liveness remains fail-closed |
  | reused receipt | retry may proceed after a completed helper; no ownership | none | `Unknown` until liveness is disproved or removal completes |
  | successful without receipt | retry may proceed after a completed helper; no ownership | none | `Unknown` rather than `Inactive` |
  | failed helper | release failed attempt; do not infer prior Runtime absence | none | `Unknown` when prior liveness evidence remains; fresh never-launched state may be `Inactive` |

  Readiness timeout alone releases retry eligibility, not liveness evidence.
  Receipt refusal removes scoped stop authority but preserves uncertainty.
  `reset_after_confirmed_runtime_loss` releases retry and stop ownership after
  the readiness monitor's recovery threshold, but does not itself prove process
  absence or clear the removal fence. Tests cover timeout -> retry eligibility -> uninstall,
  late-ready scoped stop, refusal, no-overlap, valid handover, fresh inactive
  removal, and safe successful cleanup.
- The Windows notification review finding `4056057501` is a verified false
  positive and remains a deliberate non-change. `std::fs::rename` replaces an
  existing destination on Windows according to the Rust standard-library
  documentation; desktop-shell job `106015889235` in run `35487284891` passed
  `preference_defaults_on_and_persists_the_tray_choice_across_restarts` and the
  failure-preservation test on the reviewed head. No notification code or
  dependency change is warranted. This evidence will be linked in the thread
  reply before the thread is resolved.

### Latest-master candidate evidence

- 2026-09-20: Explicit refspec refresh confirmed `origin/master` at
  `162fda5942461941942536355130477372739c36` and `origin/desktop` at
  `007f631613be43b3d00918c9e1e3cde58235099d`. The local candidate is a real
  merge commit with parents `a323abc0fb300baca829a3df0d986cabc49445cf` and
  `162fda5942461941942536355130477372739c36`; the first parent preserves the
  desktop integration candidate and the second is the native credential
  takeover mainline. The restricted fetch configuration was not changed.
- The three exact CI compatibility classes from the synthetic latest-master
  merge ref were fixed at their current contracts: the Codex Hub transport
  test supplies a hermetic catalog-preparation result; dependency selective
  checks assert `reconciling` and `reconciling_dependencies` metadata while
  retaining row/probe ownership assertions; internal-server observability
  tests use the current cross-platform `ControlIpcHost` bind/publish/cleanup
  owner and retain all five recorder-close paths.
- Focused evidence on the candidate: the three CI classes plus local dependency
  coverage passed (`483 passed`); the native auth/Model Hub/backend consumer
  set passed (`1303 passed, 33 warnings, 125 subtests`); Runtime-host passed
  `94` library tests, `25` bootstrap integration tests, and `5` notification
  HTTP tests, with `cargo fmt --check`; changed-Python Ruff passed; desktop
  localization and bootstrap build passed; UI test typechecks and production
  build passed.
- The first native consumer run reported 17 Claude direct-observation failures
  only because this agent process exports `ANTHROPIC_*` and `OPENAI_*` launch
  credentials. The new-master direct projection intentionally honors those
  launch sources. The connection-test safety fixture now removes those five
  variables before each test, and the same consumer set passes with hermetic
  source selection. Production credential precedence is unchanged.

### Original-head review inventory and scope decision

Review 5259095413 reviewed `92c600aa78f51e3b9ec48f185e5150a4f2ef3163`.
The lane and orchestrator independently paginated all threads: two findings on
one reviewed head, with no repeated class and no circuit breaker.

- `PRRT_kwDOPbFPYs6kGKft` / 4055803241: handover authority accepted without a
  validated desktop predecessor identity. Require that identity at the response
  parser and test the bootstrap consumer; preserve valid desktop replacement.
- `PRRT_kwDOPbFPYs6kGKfv` / 4055803243: readiness timeout discards successful
  launch provenance. Keep receipt ownership independent of retry deduplication;
  test timeout, late readiness, retry and scoped stop together.

The orchestrator ratified both bounded fixes within the existing desktop delta.
Neither requires a new lane, product direction, or lifecycle rewrite. A repeated
class on the next reviewed head requires another orchestrator diagnosis before
editing or pushing.
