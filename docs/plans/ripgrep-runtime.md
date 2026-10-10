# Managed ripgrep Runtime

## Background

Vibey's system prompt, ported from Pi, told the model to "use bash for file
operations like ls, rg, find". Vibey's commands run in a non-interactive bash
with the service's PATH, which usually has no `rg`. A Claude Code shell snapshot
may define `rg` as a zsh function, but that function never reaches Avibe's
commands. In issue #2425 Vibey fell back to `git grep` after `rg` was not found.
The prompt had stated a tool that did not exist.

## Design

- `vibe runtime prepare` installs the official ripgrep release through the
  shared managed-runtime core, under
  `~/.avibe/runtime/ripgrep/versions/<version>/<platform>/<fingerprint>/`. A
  missing or failed ripgrep never fails `prepare --strict`.
- `vibe/ripgrep_runtime_manifest.json` pins the upstream `BurntSushi/ripgrep`
  release archives directly, as the tmux manifest does. Each archive carries its
  size, its sha256 (matching both upstream's `.sha256` sidecar and GitHub's asset
  digest), and the sha256 of the extracted `rg`. Linux uses the static musl
  builds.
- `core.agent_path.prepend_managed_tools_to_path` is the one PATH composition
  that the Claude, Codex, OpenCode, and Vibey command environments use. It adds
  verified Git under its existing rules, then the managed ripgrep only when the
  effective PATH has no `rg`, so a user's own ripgrep keeps precedence, as for
  Git. An absent or unverifiable install leaves PATH unchanged.
- Vibey's bash rule names `rg` only when the Turn's composed PATH finds it, and
  `grep` otherwise. That PATH changes only when the service's PATH or the
  installed tools change, so a Session's system prompt stays byte-stable and
  cacheable between Turns. No per-Session state is kept.

## Availability

Avibe cannot republish into or restore a third-party release, so the download
mirror is the backup. `scripts/release_mirror.py` mirrors only the release the
manifest pins under the `ripgrep/` root. It fails the hourly run if GitHub no
longer publishes a pinned archive with the manifest's size and digest. A failed
run deletes nothing, so clients, which try the mirror first, still get the
verified copy. Mirroring all of ripgrep's releases would not work: releases up
to 14.x have no GitHub digests, which the mirror requires.

## Scope

- The platforms are darwin and linux on arm64 and x64, as for the Git runtime.
  Windows reports `ripgrep_platform_unsupported` for these reasons:
  - The managed-runtime core extracts only `.tar.gz`, and upstream ships a
    Windows `.zip`.
  - Vibey's job host does not run commands on Windows.
  - The Codex, Claude Code, and OpenCode CLIs bundle their own ripgrep.
- ripgrep is not a `vibe doctor` dependency or repair target. Its absence only
  changes the prompt line, and `vibe runtime prepare` installs it again.
- To move to a newer ripgrep, update the manifest. The mirror then selects the
  new pinned release, and older Avibe builds fall back to GitHub for the
  previous one.
