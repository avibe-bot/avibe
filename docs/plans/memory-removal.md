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

## Scope safety

Do not use `git clean`, remove untracked user artifacts, touch `$HOME`, restart
the local `vibe` service, or modify files outside this repository. The checkout
contains pre-existing uncommitted files; preserve unrelated changes.
