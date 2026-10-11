# Managed cloudflared Runtime

## Background

Remote access runs cloudflared as its tunnel connector. Released versions
downloaded `releases/latest` into `~/.avibe/bin/cloudflared` the first time
remote access was enabled, without verifying it, and never refreshed it, or
used a cloudflared found on PATH. An install therefore froze on whatever was
latest that day. 2026.8.0 and 2026.8.1 both carry "Do not use this version"
warnings, and installs from that window stayed on them.

## Design

- `vibe/cloudflared_runtime_manifest.json` pins one cloudflared release
  (`release_tag`) for every platform: version, URL, size, archive sha256, and
  the sha256 of the binary. Archive digests come from the GitHub asset
  `digest`. For the darwin `.tgz` assets, the release notes' checksum is the
  extracted binary's, not the archive's.
- `core.cloudflared_runtime.CloudflaredRuntimeManager` installs it through the
  shared managed-runtime core into
  `~/.avibe/runtime/cloudflared/versions/<version>/<platform>/<fingerprint>/`,
  mirror first, verified. Linux and Windows assets are the executable itself:
  a manifest archive's optional `format` is `tar.gz` (the default) or
  `binary`, which `stage_archive()` dispatches on. An unknown format fails the
  manifest instead of being treated as `tar.gz`.
- Platforms: darwin and linux on arm64 and x64, and Windows x64. Cloudflare
  ships no windows-arm64 build, so win32-arm64 installs the x64 executable,
  which Windows 11 on ARM runs under emulation. Windows 10 on ARM emulates
  only 32-bit x86, so there the connector fails to start and remote access
  reports the start error.
- The runtime never resolves `releases/latest`; the pin moves with Avibe
  releases.
- `vibe runtime prepare` installs it while remote access is enabled and no
  explicit `cloudflared_path` resolves, and
  `vibe runtime clean` reclaims superseded versions.

## Binary precedence

1. An explicit `remote_access.vibe_cloud.cloudflared_path`.
2. A verified managed pin: the packaged one, or an earlier one until the
   packaged one installs.
3. The binary recorded for the tracked connector, while it is still an
   executable file.

Nothing searches PATH, so a Homebrew cloudflared is replaced by the pinned
copy. One ensure-pinned step runs before the connector lifecycle lock on every
start and every automatic restart: unless an explicit path is set, it installs
the packaged pin when that pin is missing. The lifecycle lock never waits on a
download.

A failed download never breaks a running tunnel. That is a property of the
tracked connector, not of a location: the connector's state record holds the
binary it runs, so status, settings changes, route recovery, and restarts keep
resolving it, whether it came from the unpinned `~/.avibe/bin/cloudflared`,
Homebrew, or an explicit path since cleared. `stop()` and the cleanup of an
exited connector's records clear that record, and from then on only a pin
serves. A stopped install whose download fails therefore gets the install
error, exactly like a fresh install, and the monitor's restart backoff keeps
retrying the download for a requested connector. The unpinned
`~/.avibe/bin/cloudflared` is removed once the connector runs from another
binary.

## Upgrade

The binary path is part of the connector's runtime signature, so after an
upgrade the connector moves to the pinned binary once, during the reconcile at
UI start: `start()` stops the old connector and starts the pinned one. The
restart supervisor keeps the connector alive across a UI restart, so this is
one short disconnect per pin change (up to `stop_pid`'s 8 s plus
registration). Make-before-break through a candidate is not used for it: the
candidate path needs readiness waits and promotion inside start(), which holds
the connector lock throughout, for a change that happens once per release.

## Availability

Avibe cannot republish a third-party release, so the download mirror is the
backup, as for ripgrep: `scripts/release_mirror.py` mirrors only releases a
manifest pins under `cloudflared/`, fails the run when GitHub's assets diverge
from the manifest, and keeps earlier pinned releases.

## Maintenance

- A maintainer checks upstream monthly and opens a bump PR for a security
  fix, a bug on a path Avibe uses, or when the pin is six months old.
  Cloudflare supports a release for one year.
- Cloudflare stops publishing darwin-amd64 builds in 2027. From then on
  darwin-x64 stays on its last build while the other platforms move on, which
  needs per-platform release tags in the manifest schema.
- Do not add `--no-prechecks`: with DNS unavailable at start, 2026.3.0 and
  2026.10.0 both exit in about a second, and the pre-checks only add an early
  exit when DNS works but port 7844 is blocked for UDP and TCP.
