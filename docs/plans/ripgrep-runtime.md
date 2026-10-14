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
- Vibey's command guidance names `rg` only when the Turn's composed PATH finds
  it. With a Bash or PowerShell tool available, one shared sentence explains that
  ripgrep is on PATH for text search and can be invoked directly without fixing
  its installation path. Without `rg`, the existing `grep` / `Select-String`
  guidance remains; without a command tool, no PATH capability is advertised.
  This is an availability fact, not a file-discovery tutorial: it introduces no
  CLI option examples or claim to include hidden/ignored files. Existing file
  discovery guidance remains unchanged. That PATH changes only when the service's
  PATH or installed tools change, so the system prompt stays byte-stable between
  Turns. No per-Session state is kept.

The prompt contract is tested at both the rendered rule and real Turn/job-host
boundaries: available and missing ripgrep, POSIX and Windows wording, no shell,
and no duplicate hint with multiple command tools. Runtime installation, PATH
precedence and dependency failure behavior are unchanged.

## Availability

Avibe cannot republish into or restore a third-party release, so the download
mirror is the backup. Under the `ripgrep/` root, `scripts/release_mirror.py`
mirrors only releases a manifest pins. It fails the hourly run if GitHub no
longer publishes a pinned archive with the manifest's size and digest. Clients
try the mirror first, so they still get the verified copy. The root only grows:
when the manifest moves to a newer ripgrep, the earlier pinned release stays
mirrored, because released Avibe versions still install from it. Mirroring all
of ripgrep's releases would not work: releases up to 14.x have no GitHub
digests, which the mirror requires.

## Scope

- The platforms are darwin, linux, and Windows on arm64 and x64. On Windows the
  manifest pins upstream's `.zip` (`"format": "zip"`, extracted by the
  managed-runtime core with the same path and link checks as a `.tar.gz`), and
  the binary is `rg.exe`. ripgrep matters more there than elsewhere: Windows
  has no `grep`, `find` is a different program, and `Select-String` is slow, so
  with `rg` on the Agent PATH Vibey's prompt names it. The Codex, Claude Code,
  and OpenCode CLIs bundle their own ripgrep.
- ripgrep is not a `vibe doctor` dependency or repair target. Its absence only
  changes the prompt line, and `vibe runtime prepare` installs it again.
- To move to a newer ripgrep, update the manifest. The mirror then adds the new
  pinned release and keeps the earlier one.
- The upstream 15.2.0 `x86_64-unknown-linux-musl` binary is the artifact named
  in ripgrep issue #3494. That issue is an occasional SIGSEGV during very large,
  highly concurrent searches, which its analysis traces to a Linux kernel
  page-table bug, and the same bytes ship inside Codex. The release has no
  x86_64 glibc archive, and a glibc build would also tie the binary to the
  host's glibc, so the static musl build stays. A crash fails that one search
  command, not the Turn.
