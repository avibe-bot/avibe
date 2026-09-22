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

## Scope safety

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
