# Vendored Git Runtime

## Background

Show Page checkpointing needs native Git on machines that may not have a safe
system installation. macOS `/usr/bin/git` is also a Command Line Tools shim and
must not be executed before `xcode-select -p` confirms the tools are installed.

## Design

- Add a reusable managed-runtime core for manifest loading, archive download,
  SHA-256 verification, safe extraction, versioned installation, and cleanup.
- Keep tmux and Show Runtime unchanged in this PR; Git is the first consumer of
  the extracted core, which limits migration risk while establishing the common
  boundary for a follow-up.
- Install Git under
  `~/.avibe/runtime/git/versions/<version>/<platform>/<fingerprint>/bin/git`.
- Resolve an installed and metadata-verified vendored binary without network
  access. Status reports both the platform resolution (vendored first) and the
  Agent PATH resolution (system first), so the two contracts stay explicit.
- Support `VIBE_GIT_MANIFEST_PATH`, `VIBE_GIT_MANIFEST_URL`, and
  `VIBE_GIT_OFFLINE` for development, out-of-band updates, and offline use.
- Add the vendored `bin` directory to Agent shell environments only when no
  safe system Git is present. The caller-context environment builder is shared
  by Claude, Codex, and OpenCode, avoiding backend-specific wiring.

## Build Boundary

The workflow verifies the SHA-256-pinned upstream Git 2.55.0 tarball, then
builds one stripped multicall binary per supported platform. Linux uses musl
static linking; macOS links only Apple system libraries. Build flags remove
curl, expat, gettext, Perl, Python, Tcl/Tk, OpenSSL, and Rust surfaces.

The pinned source is additionally constrained so remote-capable commands,
external Git subcommands, shell aliases, hooks, and configured content filters
fail closed or are ignored. The workflow exercises `init`, `add`, `commit`,
`status`, `log`, `diff`, `restore`, and `gc`; proves hook/filter helpers do not
run; and proves that `push` is rejected.

## Publication Gate

The packaged manifest remains `release_state: pending` with zero placeholders
until the orchestrator runs the workflow with tag `git-runtime-v2.55.0-1`.
Pending manifests never download or install. The published workflow artifact
must replace `vibe/git_runtime_manifest.json` so real archive sizes and SHA-256
digests ship in the final integration commit.

## Deferred Work

- Migrate tmux and Show Runtime to the common managed-runtime core in a separate
  change after this interface has production evidence.
- Wire `core/git_binary.py` to `GitRuntimeManager` in the orchestrator-owned
  post-merge integration for #669.
