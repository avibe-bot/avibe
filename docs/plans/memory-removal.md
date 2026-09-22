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

## Scope safety

Do not use `git clean`, remove untracked user artifacts, touch `$HOME`, restart
the local `vibe` service, or modify files outside this repository. The checkout
contains pre-existing uncommitted files; preserve unrelated changes.
