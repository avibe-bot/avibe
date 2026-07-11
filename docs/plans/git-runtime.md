# Vendored Git Runtime

## Background

Show Page checkpointing needs native Git on machines that may not have a safe
system installation. macOS `/usr/bin/git` is also a Command Line Tools shim and
must not be executed before `xcode-select -p` confirms the tools are installed.

## Design

- Add a reusable managed-runtime core for manifest loading, archive download,
  archive and binary SHA-256 verification, safe extraction, versioned
  installation, cross-process mutation locking, and cleanup.
- Keep tmux and Show Runtime unchanged in this PR; Git is the first consumer of
  the extracted core, which limits migration risk while establishing the common
  boundary for a follow-up.
- Install Git under
  `~/.avibe/runtime/git/versions/<version>/<platform>/<fingerprint>/bin/git`.
- Resolve an installed binary against the immutable binary hash in the active
  manifest without network access or execution. Status classifies system Git
  paths without executing PATH-selected binaries and reports both the platform
  resolution (vendored first) and Agent resolution (system first).
- Support `VIBE_GIT_MANIFEST_PATH`, `VIBE_GIT_MANIFEST_URL`, and
  `VIBE_GIT_OFFLINE` for development, out-of-band updates, and offline use.
- Agent child environments preserve an effective system Git and prepend the
  verified runtime only when none is available. PATH composition respects an
  explicit key, including an empty value, and uses the service environment only
  where each backend passes it as an explicit fallback. Relative, empty, and
  workspace-owned prefixes are not trusted ahead of system Git. Claude injects
  at SDK client creation, Codex at its per-thread shell policy, and OpenCode at
  its per-session `shell.env` binding. Cached clients, thread policies, and
  binding rows refresh or clear when the effective Git PATH changes.

## Build Boundary

The workflow verifies the SHA-256-pinned upstream Git 2.55.0 tarball, then
builds one stripped multicall binary per supported platform. Linux uses musl
static linking; macOS links only Apple system libraries. Build flags remove
curl, expat, gettext, Perl, Python, Tcl/Tk, OpenSSL, and Rust surfaces.

The pinned source is additionally constrained so remote-capable commands and
all non-Git child processes fail closed. Signing, pagers, fsmonitor helpers,
external diff/textconv, external Git subcommands, shell aliases, hooks, and
configured content filters are disabled or ignored so the retained operations
stay deterministic. The workflow exercises `init`, `add`, `commit`, `status`,
`log`, `diff`, `restore`, and `gc`; proves hostile helper markers do not run;
and proves that `push` is rejected.

## Publication

The four-platform workflow completed in run `29165285903`. Release
`git-runtime-v2.55.0-1` contains the four archives and checksum files, and the
packaged manifest carries the published release URLs, sizes, archive hashes,
and final-binary hashes.

Offline resolution deliberately fails closed across manifest version changes:
an install is reusable only when it matches the active trusted manifest. Avibe
does not trust mutable local metadata as a cross-version fallback root; a
multi-version manifest schema can be added later if real usage justifies it.

## Deferred Work

- Migrate tmux and Show Runtime to the common managed-runtime core in a separate
  change after this interface has production evidence.
- Verify vendored resolution and Show Page checkpointing end to end on a
  gitless Incus machine as part of #669's integration pass.
