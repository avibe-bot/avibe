# Memory removal

## Decision

Remove the Memory product from the Avibe codebase and distribution. This is a
forward removal with compatibility for configurations written by released
versions.

## Invariants

- Remove Memory capture, recall, processing, runtime, sidecar, API, RPC, CLI,
  Web UI, prompt injection, dependency management, packaging, release scripts,
  feature documentation, and feature tests.
- Preserve generic uses of the word memory, such as in-memory data structures
  and cgroup resource limits.
- Never read, write, migrate, delete, or clean existing Memory data. Leave both
  `~/.avibe/memory` and the legacy `~/.vibe_remote/memory` path untouched,
  including any non-Markdown files and symlink state. Do not add a cleanup
  command or stop/inspect a legacy sidecar.
- Remove the `avibe-memory` package, the `memory` extra, lockfile entries, and
  runtime build/release integration. Do not uninstall a package already present
  in a user's environment.
- Treat the persisted `memory` config key as an obsolete unknown field. Loading
  an old config, including one with malformed Memory data or `enabled: true`,
  must succeed without warning, runtime activation, or data access. A later
  successful config save omits the key. API config payloads silently discard it.
- Removed HTTP Memory routes return 404 and the removed CLI command is an
  unknown command.
- Add one short general upgrade note stating that Memory was removed and its
  existing data was untouched.

## Installed-shape and scenario audit (review 5281184620)

Before edits at `d84f7052bdf3af4f128083ba177fd40100d4159a`, the
orchestrator authorized a bounded whole-consumer audit of retained installed
compatibility and restored scenario evidence. Inventory: `k1GlY` incorrectly
counts the retained companion's namespace metadata as core integrity evidence;
`k1Glk` points MH-RUNTIME-008 at disabled reconciliation rather than convergence.
Exclude only the canonical companion distribution at the shared provider
boundary; preserve core/legacy-core disagreement, unknown/editable behavior,
and repair provenance checks. Inspect historical wheel metadata without import,
activation, installation or mutation. Verify normal versus forced upgrade plans
through real metadata discovery in test-owned directories. Audit affected
catalog pointers against executable behavior, preserving disabled-mode tests.
The two historical release-policy findings remain unresolved pending owner
decision. No publication, guard restoration, merge, or premature closure.

Audit/evidence: downloaded the immutable v3.1.0 wheel into ignored test-owned
`.runtime/legacy-provider-evidence`; SHA256
`ce5cfb1473442642d7d19863b6f9131c0a17a1fb6e828d5e0aec04f2deea06b0`.
Read-only zip/real importlib metadata discovery confirms no `top_level.txt` and
a RECORD manifest entry causing both `avibe_memory` and `vibe` namespace
ownership. No artifact code was imported or installed. Regression fixtures
exercise actual dist-info discovery, four canonical name spellings, current and
legacy core, unrelated vendor providers, real mismatches, companion-only and
unpublished editable shapes, immutable companion bytes/mtime and explicit
core-source exact repair/preflight. CLI and API forward upgrades both consume
the shared planner. The historical automatic origin-pair resolver's only caller
was the removed Memory-package repair; it is not restored. Explicit core repair
sources and availability checks remain unchanged, without companion fallback.

Focused upgrade/provider/local-dependency/catalog run: **319 passed**.
Catalog audit compared both changed retained catalogs and the previously
restored SCT-038/AUTH-SETUP-907 pointers to test bodies. MESSAGE-DELIVERY-316
executes hydrated WeChat addressing; 317 verifies canonical text and sender;
SCT-038 checks localized refusal copy; AUTH-SETUP-907 tests actual disposable
IPv4/IPv6 origin listeners. Only MH-RUNTIME-008 was mismatched. Its convergence
test is restored as the catalog target; disabled coverage stays without that ID.
The four audited scenario targets reran: **7 passed, 2 subtests passed**.
Changed-Python Ruff and diff whitespace checks passed. No UI source changes.
Current-head automated review/CI and the two owner release decisions remain
required; no completion/closure exception is inferred.

## Acceptance evidence

- Load and save fixtures for old configurations prove the obsolete subtree is
  ignored and omitted while unrelated fields round-trip unchanged.
- A sentinel under each legacy data path remains byte-for-byte unchanged after
  service/config probes.
- Route and CLI contract tests prove removed entry points have disappeared.
- Packaging/import checks prove no new distribution imports or includes Memory.
- Focused Python tests, `ruff check` on changed Python files, and the UI build
  (if UI files change) pass.

## Shared-consumer repair contract (2026-09-22)

The owner and orchestrator authorized one coherent repair round after reviews
of 4a91228ab and e5bf5fbe found the same structural defect: deleting by Memory
symbol, line, or file also removed co-located generic behavior and coverage.
This is the circuit-breaker scope decision, not authorization to delete more
generic tests or restore the removed product.

- Merge current master, preserving upstream generic behavior. The only textual
  conflict against master 4b964fef2 is the independently added async-plugin guard;
  retain upstream coverage and use the retained synchronous release suite.
- Restore release automation checkout and asset shell dispatch; exercise paths,
  workflow SHA selection, shell syntax, upload order, and asset integrity.
- Preserve legacy author hydration and recursive privacy filtering of GENERAL
  delivery metadata. Never traverse or migrate Memory data.
- Remove unused Memory proof plumbing and companion-only upgrade preflight,
  retaining caller ACL/origin binding and generic upgrade failure behavior.
- Prove removal using actual wheel/sdist members and metadata, an installed
  import/startup probe outside the source tree, route/CLI behavior, old Editor
  payloads, and byte/symlink identity sentinels in isolated test homes.
- Restore generic browser fallbacks and i18n/scenario coverage; remove obsolete
  feature documentation. Audit mixed-purpose deletions against latest master.
- Diagnose packaged startup failures from test-owned logs before changing any
  timing policy. Verify Linux startup in the sanctioned isolated environment.

Review inventory: 4a91228ab has six inline findings plus one review-body finding;
e5bf5fbe has two inline findings plus one review-body finding. All belong to the
shared-consumer removal class; prior fixes must be verified against current code.
No merge, release, installed-service restart, or production-data operation is
authorized. Acceptance requires behavioral and artifact evidence, not grep or
collection alone. The existing orchestrator watch remains authoritative.

### Repair evidence (2026-09-22)

- Integrated master `4b964fef2` in merge `3d4aa9763`. Conflict inventory: one
  add/add file, `tests/test_conftest_async_plugin_guard.py`; preserved upstream
  generic guards and changed only the retired suite target. Thirteen guard
  tests passed. A final fetch confirmed master had not moved.
- Read all three review bodies and all thirteen threads, including the f6 head
  review. Restored generic tests for CI resource metrics, remote authorization
  cache races, OpenCode restored polling/prompt/steering, dependency UI refresh,
  upgrade failure/atomicity, and config/delivery/runtime consumers. Retained
  SCT-038, AUTH-SETUP-907, MESSAGE-DELIVERY-316 and MESSAGE-DELIVERY-317 coverage.
- Concentrated behavioral run: **2,259 passed, 14 subtests passed** across 31
  suites. Includes the real released HTTP method/path pairs, generic unknown
  API 404, existing API 405, SPA navigation, Editor legacy filtering, opaque
  byte/inode/symlink sentinels, legacy author/message-kind hydration, recursive
  privacy projection, and both release upload scripts with mocked GitHub.
  Extracted workflow shell passes `bash -n`; sparse-checkout path/ref behavior
  is replayed using a test-owned Git repository.
- Built a wheel from the actual sdist with `uv build` in isolated HOME/XDG/uv
  cache. `tests/test_distribution_artifacts.py`: **2 passed**. Inspected both
  archives and metadata; retained Show manifest/router/UI contracts; installed
  the wheel and dependencies in a fresh venv and imported with `-I` outside
  the source tree while the Memory package was unavailable. This is import
  evidence, not running-service evidence.
- Separate Linux packaged user flow: **1 passed** in disposable Debian Docker,
  installing that wheel as a new user without `.local/bin` on PATH; service
  reached `running` with internal server ready. Startup's root cause was orphan
  decorators left on `_init_modules` and `_run_im_runtime`, not a short timeout.
  Bound-method regression tests now exercise both. Diagnostic EXIT handling
  also avoids Debian login-shell logout masking a successful probe. Node/Show
  preparation is explicitly skipped by this existing installer smoke; it does
  not claim live IM or Show-runtime end-to-end coverage.
- Artifact directory (ignored, retained locally): `.runtime/removal-dist-reviewed`.
  Wheel SHA256: `3fb24491e04f81db1a3c4586e2f5c56c5ee22f828ad1e0d1e9646ab443be7acc`.
  Sdist SHA256: `1d8daca30826ccabd016fc8870a42c38c2921f4521af11b8ff3476bf60d28fec`.
- Node 24.12: UI build, lint-baseline and test typechecks passed; restored
  dependency UI tests **15 passed**; affected sidebar/geometry Playwright
  suites **27 passed** with test-owned Chromium/HOME. Unknown-request denial
  stays strict; inbox/version/events fallbacks are restored.
- Ruff passed on all existing changed Python files against master and new
  Python suites; `git diff --check` passed. Collection was also successful
  (23,029 nodes before the final seven regression cases), but is not acceptance.
- Remaining delivery gate: pushed-head Codex review and CI. No manual review
  trigger, merge, release, deployment, local reinstall or service restart.

## Legacy GENERAL boundary completion (review 5280069619)

Before edits, orchestrator `sesk8rfbcfr46` authorized a coherent continuation:
the repeated class is incomplete legacy GENERAL persisted/IPC compatibility.
Identity must remain distinct through queue collection and merge, before
hydration. Old scheduler argv must remain consumable by the new parser without
restoring retired behavior. Audit the complete consumers, not only the failing
line; preserve modern author/resource authority and public privacy projection.

Inventory at exact reviewed head `29c6de3339e10be08c652df2817b0e96033faeca`
(19 threads, six unresolved; CI run 35743858636 succeeded):

1. `PRRT_kwDOPbFPYs6kypQQ`: legacy effective-author queue merge identity.
2. `PRRT_kwDOPbFPYs6kypQa`: suppressed, ignored legacy rollback argv.
3. `PRRT_kwDOPbFPYs6kypQm`: runtime-clean localized help matches retained cleaners.
4. `PRRT_kwDOPbFPYs6kypQx`: retired settings bookmarks redirect to General;
   retired API paths still return 404, with no feature page or PWA state restored.
5. `PRRT_kwDOPbFPYs6kypQL`: investigate released updater companion preflight
   and supported transition options. Read-only evidence first; implementation
   needs an explicit orchestrator/owner decision.
6. `PRRT_kwDOPbFPYs6kypQq`: investigate historical published manifest assets,
   backups and remaining availability guards. Read-only evidence first; no
   Memory-only workflow/verifier restoration without a scope decision.

The two release findings remain explicit and unresolved pending that decision.
No tombstone/bridge publication, old-release mutation, data traversal, sidecar
logic, local service operation, merge or premature PR closure is authorized.
The sole active Watch is `d18dce76dd9f`, owned by `sesk8rfbcfr46`; do not reseed.

Read-only release evidence: published `v3.1.0` and `gh-v3.1.1rc1` through
`gh-v3.1.1rc4` build and preflight a same-tag `avibe_memory` wheel when the
companion is installed/enabled. A future companionless release blocks that
automatic upgrade route. The existing documented core-wheel-only manual
install is an alternative, not an automatic bridge. No release is authorized
for this local-test-only PR. The deleted guard exclusively protected historical
Memory Runtime manifests/assets; Git Runtime and Model Hub guards do not cover
them. Nonexpired backup artifacts exist (for example `10677598133`, expiring
2026-12-21), but are not a permanent availability guarantee. Both decisions
were escalated to the orchestrator; neither thread is resolved by these facts.

Supported repair evidence:

- The shared merge key now preserves legacy effective authors both in normalized
  queue segments and raw snapshot merges. Explicit modern authors and delegated
  resource authority retain precedence/separation. Regression cases exercise
  distinct/same authors, non-ASCII text, hydration of both separated deliveries,
  public privacy projection, and byte-identical persisted snapshots.
- Restored the suppressed, ignored flag/value pair accepted by released
  `v3.1.0`; neither reaches the restart job. A read-only execution of the
  historical `d87a39415` scheduler's argv-building AST against the new parser
  also passed with a job sink. A published emitter for that historical commit
  was not established; this is not claimed as a released-client runtime test.
- Focused delivery/CLI/removed-feature tests: **446 passed**; restart suite:
  **28 passed**. After strengthening the second-author assertion, delivery plus
  restart suites: **249 passed**. Changed-Python Ruff and diff whitespace passed.
- Localized cleanup help matches the real retained cleaners. Route unit tests:
  **9 passed**; both old bookmarks also passed real browser navigation with
  strict unexpected-request rejection. UI build, lint and test typechecks passed.
  The full workbench-general browser suite passed **74 tests**.
- Prior-head CI was independently read back: all sixteen reported checks
  succeeded at `29c6de3339e10be08c652df2817b0e96033faeca`. This does not certify
  the next pushed head; its review and CI remain mandatory.

## Scope safety

### Independent UI lifecycle audit at b7f8c58da

Before edits, the orchestrator authorized the circuit-breaker repair for
`PRRT_kwDOPbFPYs6k0mEs`: retained CLI service replacement and repair still stopped
the UI solely to align the removed per-process Memory proof secret. Compare
complete original/current functions, not just their comments. Healthy UI,
remote access and SSE must survive service-only replacement/repair.

Caller inventory: `cmd_start` ensures both services and delegates healthy reuse
or missing/stale UI recovery to `runtime.start_ui`; its explicit UI stop is
obsolete. `_start_service_after_repair` repairs only the service; its entire UI
realignment block is obsolete (an absent UI already remained absent).
`cmd_stop` legitimately stops the full stack and remote access. Supervisor
`scope=all` explicitly stops/restarts UI plus service while preserving the
tunnel; `scope=service` never owns UI. Explicit CLI restart and upgrade schedule
the full-stack scope to activate new UI code, so those calls remain. Runtime UI
startup stops only a matching unhealthy process, never a healthy/unrelated PID.
The private live-UI helper has only the two obsolete coupling consumers and can
be removed with their probes. Preserve readiness waits, repair error mapping,
status PID reporting and missing/stale UI startup behavior. Validate with real
idempotent UI startup code and stubbed process/network edges in test-owned homes.
Release-policy threads remain unresolved pending owner decision.

The isolated Incus supervisor's remaining UI calls are also retained: initial
stack startup, recovery of a dead UI, failed-stack teardown and explicit stop.
They own the isolated stack and are not secret synchronization.

Behavioral evidence: CLI/restart-supervisor/upgrade suites **406 passed**, including a
six-case real `runtime.start_ui` matrix (new/reused service × healthy/missing/
stale UI), service-repair success/failure with healthy or absent UI, PID/status
preservation and forbidden UI/remote process operations. Existing explicit
full-stack restart, service-only restart, stale/unrelated PID handling, slow
startup/error mapping and upgrade activation coverage remains. The retired
helper's identity-probe test now targets the retained runtime probe; no generic
coverage was discarded. All process/network edges are stubs and paths are
test-owned; no real UI/SSE/tunnel or service was restarted.
Changed-Python Ruff and diff-check passed; no UI source changed.

### Shared lifecycle audit at 970facd01 (2026-09-23)

The orchestrator authorized a bounded whole-contract repair before another push.
The repeated class is generic behavior lost with co-located Memory code; green
CI/import/startup evidence did not test concurrency, mutation completion or IM
business readiness. Inventory: `PRRT_kwDOPbFPYs6kz9WY` config lock inversion;
`PRRT_kwDOPbFPYs6kz9Wm` archive reporting deadline;
`PRRT_kwDOPbFPYs6kz9Wt` missing initial adapter dependency injection. Historical
release threads `kypQL` and `kypQq` remain pending owner decision.

Contracts to audit and prove before push:

- All generic config save/transaction/migration entrants acquire CONFIG_LOCK
  before the per-file lock, with nested reentrancy and preserved concurrent
  fields. The removed Memory transaction supplied that ordering incidentally;
  restore it at the shared config boundary without any Memory lock.
- UI archive -> internal RPC -> controller lifecycle -> durable archive awaits
  accepted completion without a transport reporting deadline. Connection
  failure may remain bounded; preserve success/domain-failure mapping and do
  not retry or restore the retired endpoint.
- Initial runtime clients receive the same settings/controller dependencies as
  hot-added clients, after managers/router exist. Audit all platform descriptors
  and the auxiliary avibe contract. Exercise actual adapter authorization, not
  only injection call counts; no live SDK connections or feature runtime.

Compare complete original/current surrounding functions and retained callers,
then use deterministic concurrency and lifecycle gates in test-owned state.
Installed-service liveness remains distinct from IM authorization readiness.

Audit result: compared complete original/current `_init_modules`,
`_inject_runtime_dependencies`, `_register_client_runtime`, config transaction,
save/load/migration helpers and archive controller/RPC/UI callers. The generic
losses were CONFIG_LOCK acquisition, the final startup injection loop and the
archive client's no-reporting-deadline contract. Migration locking and hot
registration were unchanged; archive's removed post-commit observer was
Memory-only, so it stays removed. Removed the now-trivial archive observer
wrapper/unused loop variable, preserving the blocking completion helper.
Only two production entrants take the private config file lock: the shared
boundary and migration persistence; both now take CONFIG_LOCK first.

Evidence: **354 passed** across ten focused config/controller/platform/internal
RPC/UI/archive suites; **177 passed** across config-read, migration persistence,
API-save and file-lock suites. New barrier/event-driven regression proves
transaction/load-persistence overlap preserves both updates and nested
load/save transactions re-enter safely. Archive test traverses actual UI HTTP,
internal client with asserted transport timeouts, internal ASGI route, real
Controller archive method and test SQLite; a gated lifecycle proves pending
acceptance does not become an early response, then checks commit or 404 mapping.
Adapter tests execute the real `_init_modules` and shared injection, all five
actual adapter setters at startup/hot registration, and Discord allowed/denied/
unknown channel authorization against real test settings. SDK construction and
network startup are excluded; this is not a live IM end-to-end claim. The avibe
auxiliary adapter remains outside runtime injection; forbidden feature imports
and absent Memory runtime are asserted. No production state/service touched.

### PWA compatibility boundary completion (review 5280345842)

Before edits, the orchestrator verified `PRRT_kwDOPbFPYs6kzQa_` at
`074e02466d8481acae6538d3600dfe123b237d71` and authorized the circuit-breaker
repair: browser aliases reached the router, but persisted PWA paths were rejected
before routing. Replace the independently maintained legacy PWA allowlist with
normalization derived from `LEGACY_SETTINGS_REDIRECTS`, accepting only safe
restorable destinations. Audit storage read, normalization, launch selection,
rendering and canonical persistence together. Preserve explicit deep links,
query/hash stripping, dynamic routes and no mid-session bounce; reject unknown
or external destinations. Cover declaration-driven invariants and actual app
PWA launch in test-owned browser storage. No feature page/API/runtime is restored.
The two release-policy threads remain unresolved pending owner decision; no
exception to the review/CI/close-out gates has been granted.

Implementation reuses `LEGACY_SETTINGS_REDIRECTS` directly and removes the old
duplicated legacy allowlist. The destination still passes origin and canonical
restorable-path checks; query/hash are omitted. No App lifecycle change was
needed. Focused PWA/navigation/router units: **74 passed**. Actual App browser
tests with test-owned localStorage and simulated iOS standalone detection:
**7 passed**, covering both retired entries, canonical persistence, back to root
without bounce, explicit Shortcuts deep link, unknown/external rejection and
the two ordinary bookmarks. This is Chromium app-flow evidence, not physical
iOS-device verification. Prior-head CI was read back fully green; new-head gates
remain required.
Node 24.12 UI build, lint-baseline, test typechecks and diff whitespace checks
passed. No Python files changed. The latest fetched master remains `4b964fef2`.

### Owner close-out decision (2026-09-22 22:41 Asia/Shanghai)

Continue the repair, push, review-response, and exact-head review/CI loop to
completion. Do not merge or close prematurely. After the gates are satisfied,
hand the evidence to orchestrator `sesk8rfbcfr46`, who will close PR #2120
without merging and retire the existing watch. Preserve the `remove-memory`
branch, every commit, and this worktree for the owner's local testing. No
release, deployment, installed-service restart, or branch/worktree deletion.

Do not use `git clean`, remove untracked user artifacts, touch `$HOME`, restart
the local `vibe` service, or modify files outside this repository. The checkout
contains pre-existing uncommitted files; preserve unrelated changes.
