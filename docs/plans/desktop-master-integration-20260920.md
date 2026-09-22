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
  `reset_after_confirmed_runtime_loss` is called by the readiness-only recovery
  monitor. It releases retry and stop ownership after the monitor's recovery
  threshold, but does not itself prove process absence or clear the removal
  fence. Tests cover timeout -> retry eligibility -> uninstall,
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

### Final mainline synchronization candidate

- 2026-09-20: The restricted tracking refs were refreshed with explicit
  refspecs, and `origin/master` was verified at
  `a32cd9df9be96ae9b86e1cdf81c86e9fd6bdbfc3`; `origin/desktop` remained
  `007f631613be43b3d00918c9e1e3cde58235099d`. A real normal merge produced
  candidate `f07634ac61ba41d6f05e26c1a1f6b6feff2175a1`, with parents
  `e2c92afc19c9f95b2581ea8bc46b8ad40ce98896` and
  `a32cd9df9be96ae9b86e1cdf81c86e9fd6bdbfc3`. The expected merged tree was
  independently checked as `5dea0c52f75b6eedb5c952bd9f7f4ca56f462ebf` before
  this plan append. The only overlapping files are the i18n catalogs; the
  upstream Workbench/AppShell/ChatPage/Composer/Sidebar behavior and the two
  migration test fixtures are retained.
- Independent pre-push spot-checks covered the three CI compatibility classes
  and exhaustive dependency selection (`270` tests), 13 RuntimeHost/bootstrap
  consumers, and the native credential fixture boundary. On the merged tree,
  the focused Python CI compatibility, dependency, and observability command
  passed `314` tests; Runtime-host passed `94` library, `25` bootstrap, and `5`
  notification HTTP tests; the merged Workbench queue tests passed `106` UI
  tests. Rust formatting and workspace Clippy passed. The opt-in hermetic
  mock-upstream Model Hub suite passed `69` tests with `20` expected skips.
  The migration E2E command requires the separately supplied offline engine
  manifest; this workspace did not have that fixture. Its local attempt failed
  closed at `engine_down`/`migration_native_busy`, before credential mutation,
  confirming the fixture prerequisite rather than a production catalog or
  native-auth bypass. The command was not retried against a network asset or
  installed service.

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

### H3 circuit-breaker ruling: untagged Controller adoption

Review `5259674396` reviewed `c7b749838181d443b19af4faa270f829f13c04fc` and
added `PRRT_kwDOPbFPYs6kHQt4` / 4056230990 at `vibe/ui_server.py:3304`.
The finding repeats the original predecessor-identity scenario on a later
head: `vibe start` correctly reuses a healthy user-managed Controller without a
desktop identity, but the newly started UI inherits the bundled identity and
`/ready` reports a mismatch forever. The predecessor-ID boundary remains a
hard authority boundary; the missing behavior is positive external adoption.

The bounded closure keeps `/ready` as the authoritative service-lock and IPC
chokepoint. After those checks pass, a healthy Controller with no
`desktop_runtime_id` is an affirmative versioned external readiness response,
even if the serving UI inherited a bundled tag. When that UI tag is valid, the
response carries it separately as optional `desktop_ui_runtime_id`; this field
records which UI artifact is serving and never identifies or authorizes the
Controller. A truly external Controller/UI pair with no UI tag keeps the
existing three-field response. A tagged Controller whose ID differs from the
UI remains a five-field mismatch and can never authorize handover without a
validated predecessor identity. Invalid identity, owner change, unavailable
Controller, and not-ready responses remain rejected. No spawn-time environment
rewrite, persistent adoption model, or new receipt protocol is introduced.

The desktop consumers apply the same rule in both adoption paths. A bundled
launcher receiving affirmative readiness without a Controller ID adopts it
without handover, pruning, or scoped stop authority. Polling after the helper
starts also accepts that external readiness; a tagged different Controller ID
still follows the existing handover path. During uninstall, affirmative
readiness with `desktop_ui_runtime_id` is always `Unknown`, including in a new
host after the shell has been reopened, because the private UI may still be
serving beside the external Controller. Older three-field no-ID peers remain
`Unknown` whenever the host has local helper attempt/liveness evidence. A
truly external three-field Controller/UI pair with no helper evidence retains
`External` cleanup behavior.

The required consuming transitions are:

| Scenario | `/ready` identity | Bootstrap result | Handover/prune/stop | Uninstall decision |
| --- | --- | --- | --- | --- |
| Existing external Controller/UI | no IDs | adopt | none | `External` when the host did not launch it |
| Bundled UI beside an untagged Controller | `desktop_ui_runtime_id` | adopt | none | `Unknown`; preserve private trees, including after reopen |
| Bundled helper starts an older no-field peer | no IDs after launch | adopt during polling | none | `Unknown` while local liveness evidence remains |
| Bundled helper and Controller share the expected ID | expected ID | adopt | prune superseded installs only | `Managed` |
| Tagged Controller has a different ID | other valid ID | mismatch/handover through validated predecessor path | no authority from the body itself | existing managed/external rules |
| Missing/invalid/not-ready identity | unavailable or invalid | reject or keep polling | no handover authority | fail closed when liveness is uncertain |

Tests must cover the real Python reused-service/missing-UI path and its
untagged `/ready` response, the Rust parser's exact three-field and
`desktop_ui_runtime_id` payloads, polling adoption after a helper launch, and
uninstall reaching `Unknown` while preserving private Runtime/backend trees,
including after a fresh RuntimeHost reopen. Existing valid handover, no-ID
mismatch rejection, receipt lifetime/refusal, fresh inactive cleanup, and
safe successful cleanup remain part of this contract.

The first H3 candidate exposed a boundary the initial ruling missed: the
in-memory liveness fence disappears when the shell closes, so a later host
cannot know that its bundled UI may still be using private files. The smallest
stateless correction is to let the already authoritative `/ready` response
carry the live UI identity separately. `desktop_ui_runtime_id` is optional in
schema version 1, accepts the same validated lowercase hexadecimal identity as
the Controller field, and is never used for handover, stop authority, managed
classification, or pruning. Old valid payloads remain accepted; malformed or
ambiguous new shapes fail closed. The orchestrator reproduced the failure as
`adopt -> Unknown removal -> reopen -> External removal`, and authorized this
producer/parser/consumer correction before commit or push.

### Revised H3 candidate evidence before commit

- The revised candidate remains limited to the producer, readiness parser and
  direct RuntimeHost consumers, plus the shared contract fixture
  `tests/fixtures/desktop_ready_external_controller_bundled_ui.json`. Python's
  `/ready` producer test and `runtime.ui_server_healthy` read that same
  affirmative external-Controller/bundled-UI shape; the Rust parser and
  bootstrap test include it directly. The fixture therefore exercises the
  actual cross-language field names and values rather than two independently
  invented payloads.
- `uv run pytest -q tests/test_desktop_runtime.py tests/test_internal_client.py`
  passed `133` tests. This includes the real reused-service `cmd_start` path,
  the single missing-UI spawn and reused receipt, valid and malformed optional
  identity payloads, producer output, and `ui_server_healthy` acceptance of the
  new shape. The Python tests reject null, uppercase, short, ambiguous and
  extra-field identities.
- RuntimeHost bootstrap consumers passed `cargo test --test bootstrap` with
  `28` tests. `a_helper_adopts_untagged_readiness_after_launch_without_handover_or_pruning`
  proves polling adoption after helper launch and the no-authority/Unknown
  uninstall fence. `an_external_controller_with_a_bundled_ui_stays_unknown_after_shell_reopen`
  starts the helper on the first host, drops that host, adopts the same
  producer fixture in a fresh host, asserts one launch total, no handover or
  prune, and `Unknown` on both removal attempts. The pre-existing
  `unknown_runtime_ownership_blocks_private_file_removal` consumer still
  proves that Unknown refuses deletion of private Runtime and backend roots.
- Rust readiness unit tests passed `11` tests, full RuntimeHost library tests
  passed earlier with `94` tests, and RuntimeHost Clippy with `-D warnings`
  passed. `cargo fmt --all -- --check`, changed-Python Ruff, and
  `git diff --check` passed. Cargo emitted only its existing permission warning
  while attempting to clean a shared global cache; compilation, tests and
  lint completed successfully. No notification, process-stop, spawn-environment,
  persistence or predecessor-authority behavior was widened.
- The earlier opt-in mock-upstream Model Hub suite passed `69` tests with `20`
  expected skips. The migration E2E attempt remains accurately recorded as
  `3 failed, 71 passed, 20 skipped` in the earlier evidence: the workspace
  lacked the separately supplied offline engine manifest, and the run closed
  at `engine_down` / `migration_native_busy` before credential mutation. This
  observes the fixture limitation; it does not prove a causal production
  authentication failure and was not retried against a network asset or the
  installed service.

The orchestrator independently reviewed all eight candidate paths at the
`c7b749838181d443b19af4faa270f829f13c04fc` base and approved this bounded fix
for commit and push. Its consuming spot-check passed `133` Python tests, `28`
bootstrap tests, `11` readiness-parser tests, and the existing
`unknown_runtime_ownership_blocks_private_file_removal` consumer (`40` Rust
checks in total). The prior destructive transition `[Unknown, External]` is
now covered by the fresh-host regression and expects `[Unknown, Unknown]`.
The remote desktop was still `c7b749838181d443b19af4faa270f829f13c04fc`,
and the three findings-bearing review heads remain H1 `92c600aa78`, H2
`007f631613`, and H3 `c7b749838`, with the H3 thread still open until the
pushed fix is evidenced.

### Resumption for the 2026-09-22 integration cycle

- 2026-09-22: The owner reopened PR #2062 for conflict resolution. The clean
  desktop head is `8440cf81737be5db46a16992ecce41fe29beb866`; the actual remote
  `master` source is `16bf1be3f93cd3cb855da2dd1b7133932acc0363`. GitHub's PR
  base ref remains stale at the earlier `3994d18d`, so this cycle uses explicit
  remote refspecs and the SHA returned by `git ls-remote`.
- The new cycle has one lane-owned combined PR/lint/desktop-shell Watch,
  `54efbc6d5cec`, with cursor
  `pr-2062-sesy9cb4ht9nq-20260922.json`. Both the supervisor and bundled waiter
  use `timeout 0`; the cursor is new for this cycle and will not be reseeded.
  The prior 2026-09-20 cursor and retired Watch remain untouched.
- The merge probe reports one textual conflict in
  `tests/test_ui_server_install.py`. Resolution must retain the desktop
  three-backend parameterization of
  `test_install_job_fails_when_runtime_refresh_fails`, master's hermetic
  `_agent_runtime_fingerprint -> None` fixture stub, and all newer measured
  runtime refresh tests. Automatically merged changes at the shared controller,
  Codex agent, API/CLI/UI server, UI API context/backend lifecycle, i18n, and
  related test boundaries require semantic audit; no redesign or unrelated
  behavior change is authorized.
- This cycle remains a real merge into the existing desktop ancestry. The first
  push is held until the orchestrator independently spot-checks a concrete
  candidate SHA/diff and a consuming test. Required evidence is focused
  hermetic Python installation/runtime/IPC/backend coverage, relevant UI tests
  and build, desktop Rust lifecycle tests/format/lint, and changed-Python Ruff.
- 2026-09-22: The owner approved the initial test distribution path: use the
  existing manually triggered package workflow with a selected SemVer and
  source ref, macOS ad-hoc app signing inside an unsigned/unnotarized DMG, and
  unsigned Windows NSIS installers. The README records Gatekeeper/SmartScreen
  prompts, `SHA256SUMS`/`SIGNATURE` evidence, 14-day artifact retention, the
  self-contained runtime/no Python or Node prerequisite, and manual replacement
  upgrades. The optional trusted-signing path remains unchanged; this decision
  does not authorize public Release publishing, updater work, new certificates,
  or OS-protection bypasses.

### Resumed merge candidate evidence

- 2026-09-22: A real non-fast-forward merge of
  `16bf1be3f93cd3cb855da2dd1b7133932acc0363` completed as
  `5589edc71292921281f95d072916628a9790fa6a`, with parents
  `45aa0208e8acda3a7d51e1e5b2052e90a824743e` and
  `16bf1be3f93cd3cb855da2dd1b7133932acc0363`. The one conflict in
  `tests/test_ui_server_install.py` keeps the desktop three-backend refresh
  failure parameterization, master's `_agent_runtime_fingerprint -> None`
  hermetic stub, and all measured-runtime refresh cases. No conflict markers or
  unresolved paths remain.
- The automatically merged shared boundaries were audited. Master changes to
  Model Hub credential migration/persistence, Codex steering reconciliation,
  caller authorization, queued delivery projections, Workbench/sidebar/API
  context, and i18n remain present. Desktop `vibe.desktop_backends`,
  `vibe.desktop_runtime`, FastAPI UI server behavior, rolling backend refresh,
  and native Runtime ownership/readiness paths remain present. No migration or
  allocated-ID namespace collision was introduced.
- Candidate validation passed: Python desktop/runtime, installation/upgrade,
  IPC, FastAPI/API, backend rolling-refresh, and observability consumers passed
  `1389` tests with `5` skips; Model Hub/native-auth/Codex consumers passed
  `1610` tests, `68` warnings, and `47` subtests. The merged UI focused suite
  passed `125` tests; UI test typechecks and `npm run build` passed. Desktop
  i18n validation and bootstrap `npm run build` passed. Runtime-host passed
  `94` library, `28` bootstrap, and `5` notification HTTP tests; `cargo fmt
  --all -- --check` and workspace Clippy with `-D warnings` passed. Changed
  Python Ruff passed. The only native warning was the existing inability to
  clean one shared global Cargo cache entry due to permissions; it did not
  affect compilation, tests, or lint.
- This candidate is ready for the orchestrator's independent diff and consuming
  test spot-check. It has not been pushed; the first push remains held. The
  latest remote desktop still needs an immediate explicit-refspec check before
  any authorized push.

### H4 readiness-loss transition correction

- 2026-09-22: The exact-head Codex review identified a real monitor boundary
  defect: three consecutive `/ready` misses discarded a valid startup receipt,
  even though readiness loss supplied no process-exit or ownership-loss
  evidence. The repeated receipt/provenance class was diagnosed at the
  orchestrator level before this bounded edit; no receipt schema, persistence,
  parser, launcher signaling policy, or lifecycle redesign was introduced.
- The shared RuntimeHost transition is now named
  `release_after_readiness_loss`. It releases only a completed helper's retry
  slot. A pending helper remains the single deduplicated launch, a valid
  `started` receipt remains paired with its original resolved launcher, and
  `runtime_may_be_running` remains set. Scoped `OwnershipLost` still revokes
  only that receipt and preserves the unknown-liveness fence; a successful
  scoped stop clears the state.
- Private removal now fails closed whenever local launch/liveness evidence is
  present but readiness is unavailable. A retained receipt therefore cannot
  make `BundledVibeLauncher::remove_private_runtime(Managed)` invoke unscoped
  handover. Affirmative managed readiness remains sufficient for managed
  cleanup, while external/UI-only readiness and unknown readiness retain their
  existing boundaries.
- The production shell monitor uses the shared transition after its existing
  generation and activity checks. RuntimeHost tests cover retained receipt,
  explicit scoped stop, pending-helper deduplication, replacement receipt
  authority, reused/receipt-less/failed no-authority outcomes, unknown removal,
  and successful cleanup. The shell crate test drives the same recovery
  transition after three misses with a real RuntimeHost and confirms that a
  pending helper is not overlapped. The former
  `confirmed_runtime_loss` expectation was corrected to distinguish readiness
  loss from confirmed process loss.

### Latest master refresh: #2118 lint-gate test-only delta

- 2026-09-22: Explicit refspec refresh moved `origin/master` from
  `335f62e6c15987d0ab72f6139c16fe49eea706e8` to
  `4b964fef223862c2cf7d5cec852ac0d3aa64e323` (#2118). The delta is one
  test-only path, `ui/scripts/lintBaseline.test.mjs`: its temporary integrity
  probe now explains its ownership and asserts that the diagnostic contains one
  inline-policy finding. It changes no desktop runtime, shell, release, or
  product behavior.
- The real merge into the desktop candidate is conflict-free and has parents
  `deedb50b4bb814b180953ebdbf14b64350e51ff3` and
  `4b964fef223862c2cf7d5cec852ac0d3aa64e323`. The existing release and H4
  implementation paths remain unchanged. Focused Vitest coverage for the
  incoming file passed with 33 tests; GitHub lint and desktop-shell remain the
  authoritative pushed-head checks.
