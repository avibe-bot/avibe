# Show Runtime Manifest Cache Plan

## Summary

Vibe Remote should stop embedding every platform's Show Runtime archive in the
Python wheel. The wheel should stay small and carry only the runtime selection
metadata. The actual Show Runtime should be prepared per machine:

- resolve the current platform
- download exactly one matching runtime archive
- verify it against a pinned manifest
- install it into a global cache
- atomically switch new Show Page sessions to the prepared runtime

Official install and upgrade flows should run this preparation step up front, so
normal users do not wait for the first Show Page request. If runtime preparation
fails, Vibe Remote itself must still install and start; only Show Page runtime
availability is degraded.

## Goals

- Shrink Vibe Remote wheel size by removing the six embedded runtime archives.
- Keep Show Page ready after normal official install or upgrade.
- Share one prepared runtime across all sessions on the machine.
- Verify every downloaded runtime archive by version, platform, and sha256.
- Make runtime upgrades deterministic when Vibe Remote releases.
- Preserve offline behavior when a verified cached runtime already exists.
- Keep runtime install failures isolated from core Vibe Remote installation.

## Non-Goals

- Do not require npm install in user session directories.
- Do not use one Node process or dev server per Show Page session.
- Do not expose live Show Runtime handlers on public `/p/<share-id>` links.
- Do not make the Python wheel platform-specific in the first iteration.
- Do not force-kill active runtime processes during normal upgrades.

## Current Problem

The current release workflow builds six platform archives from
`avibe-bot/vibe-show-runtime` and copies all of them into `vibe/show_runtime/`
before building the Vibe Remote wheel. This makes the `py3-none-any` wheel
roughly 100 MB even though each installed machine needs only one archive.

That approach is reliable for offline first launch, but it scales poorly:

- every platform pays for every other platform
- GitHub-only prereleases become large
- future UI/runtime dependency growth directly inflates the Python package
- PyPI publishing and local upgrades move more data than necessary

## Target Architecture

```text
Vibe Remote release
  -> small Python wheel
       - UI dist
       - runtime manifest lock
       - no platform runtime archive by default
  -> release assets
       - vibe-show-runtime-node-darwin-arm64.tgz
       - vibe-show-runtime-node-darwin-x64.tgz
       - vibe-show-runtime-node-linux-arm64.tgz
       - vibe-show-runtime-node-linux-x64.tgz
       - vibe-show-runtime-node-win32-arm64.tgz
       - vibe-show-runtime-node-win32-x64.tgz
       - show-runtime-manifest.json

Official installer / upgrade
  -> install Vibe Remote
  -> vibe runtime prepare
       - read pinned manifest from installed package
       - select current platform archive
       - download archive if cache miss
       - verify sha256 and size
       - safe extract into versioned cache
       - atomically update current pointer
```

Runtime state:

```text
~/.vibe_remote/runtime/show-runtime/
  downloads/
    <sha256>.tgz
  versions/
    <runtime-version>/
      darwin-arm64/
        package.json
        package-lock.json
        packages/
        node_modules/
        .vibe-show-runtime.json
  current.json
  install.log
  stdout.log
  stderr.log
```

The download cache key is the manifest archive digest, not only the version
string. Installed runtimes also record the manifest digest and archive digest;
an installed runtime is reused only when those values still match the active
manifest.

## Manifest Contract

The installed Vibe Remote package includes a pinned manifest lock, for example:

```json
{
  "schema_version": 1,
  "runtime_version": "0.0.12",
  "runtime_source": {
    "repo": "avibe-bot/vibe-show-runtime",
    "ref": "8ea539d104e3c9d9e1873924a2be302d35465fcf"
  },
  "minimum_node": "^20.19.0 || >=22.12.0",
  "archives": {
    "darwin-arm64": {
      "name": "vibe-show-runtime-node-darwin-arm64.tgz",
      "url": "https://github.com/avibe-bot/avibe/releases/download/gh-v2.3.7rc1/vibe-show-runtime-node-darwin-arm64.tgz",
      "sha256": "…",
      "size": 18300000
    }
  }
}
```

Required validation:

- `schema_version` is supported
- current platform exists in `archives`
- archive URL uses `https` or an explicit local `file` URL for development
- downloaded file size matches when present
- downloaded file sha256 exactly matches
- archive extraction passes the existing safe tar checks
- extracted archive exposes the expected runtime CLI
- extracted archive metadata records the same manifest digest

## Runtime Provider Model

Initial providers:

- `manifest-cache`: default for packaged Vibe Remote releases
- `local-bin`: explicit `VIBE_SHOW_RUNTIME_BIN` override for development
- `archive-path`: explicit local archive override for testing and emergency use
- `npm`: future stable-channel provider, not the first default

Resolution order:

1. `VIBE_SHOW_RUNTIME_BIN`
2. explicit local archive path
3. manifest cache
4. development provider override, if configured

The first implementation keeps the provider logic inside `ShowRuntimeManager`
instead of introducing a new class hierarchy. That keeps the change smaller
while preserving the provider boundary in the CLI and environment contract.
Show Page request-time install remains available as a recovery fallback, but
official install and `vibe upgrade` now run strict preparation first and treat
failure as a warning at the install/upgrade layer.

## Install And Upgrade Behavior

Official install script:

```text
install Vibe Remote
run vibe runtime prepare --strict
if prepare succeeds:
  print Show Runtime ready
else:
  print warning and keep Vibe Remote installed
```

Official upgrade flow:

```text
upgrade Vibe Remote
read newly installed runtime manifest
run vibe runtime prepare --strict
if same manifest digest already installed:
  reuse cache
else:
  download and verify current platform archive
  extract to new version directory
  atomically switch current
```

Direct `pip install` or `uv tool install`:

- should not depend on Python package postinstall hooks
- may leave runtime unprepared
- `vibe start` or first `vibe show` status can report a clear preparation
  warning
- first Show Page request can still attempt one controlled prepare if enabled

This split keeps official UX fast while preserving Python packaging norms.

## Runtime Update Semantics

Vibe Remote releases pin a specific runtime manifest. A Vibe Remote upgrade is
therefore the default runtime update trigger.

Rules:

- New Vibe Remote version sees a new manifest.
- If the current cache matches the manifest digest, no work is needed.
- If the manifest differs, prepare the new archive in a temporary directory.
- Only after verification succeeds, atomically update `current`.
- Existing sidecar processes keep running until idle TTL, explicit stop, or
  Vibe Remote restart.
- New sidecar starts use the new `current`.
- Keep the previous version for rollback until a cleanup policy removes it.

This avoids breaking active Show Pages mid-session while still making new
sessions pick up the updated runtime.

## CLI Surface

Add a small CLI group:

```bash
vibe runtime status
vibe runtime prepare
vibe runtime clean
```

`status` should show:

- provider
- current platform
- pinned runtime version
- installed runtime version
- archive digest
- cache path
- last prepare result
- whether Node satisfies the manifest engine

`prepare` options:

- `--force` redownload/reinstall even if the digest matches
- `--offline` use only verified cache, no network
- `--manifest <path>` or `--manifest-url <url>` for development and regression
  tests
- `--strict` returns non-zero when preparation fails

`clean` options:

- keep current
- keep previous N versions
- remove failed temporary directories and stale downloads

## Failure Policy

Runtime prepare failure must not fail core Vibe Remote install.

Failure modes:

- Node missing or unsupported
- network unavailable
- checksum mismatch
- unsupported platform
- unsafe archive member
- missing runtime CLI after extraction
- permission or disk-space error

Behavior:

- official install/upgrade prints a warning and exits successfully unless the
  user explicitly requested strict runtime preparation
- `vibe runtime prepare --strict` can return non-zero for CI
- Show Page recovery page should explain that the runtime package is missing or
  invalid and suggest `vibe runtime prepare`
- if a previously verified runtime exists, reuse it when safe
- do not reuse a stale installed runtime when the active manifest changed and
  the new archive fails integrity checks

## Release Workflow Changes

GitHub prerelease workflow:

1. Build six runtime archives.
2. Compute sha256 and size for each archive.
3. Generate `show-runtime-manifest.json` pinned to the runtime repo commit.
4. Attach the archives and manifest as GitHub release assets.
5. Copy only the manifest lock into the Vibe Remote package.
6. Verify the wheel does not contain `vibe/show_runtime/*.tgz`.
7. Verify the wheel does contain the manifest lock.

PyPI workflow:

- Generate the same manifest and verify the wheel contains the manifest but no
  runtime archives.
- Do not copy runtime archives into `dist/` for PyPI upload; PyPI should receive
  only Python package artifacts.
- The manifest URLs still point at GitHub release assets for the same tag, so
  the matching GitHub release assets must exist before users run
  `vibe runtime prepare` from a PyPI-installed package.

## Security And Integrity

- Use sha256 verification before extraction.
- Keep safe tar extraction checks for files, directories, symlinks, and hard
  links.
- Extract into a temporary directory under the runtime cache and rename after
  validation.
- Never execute code from a newly downloaded archive before integrity checks.
- Treat manifest URL overrides as advanced/development mode.
- Avoid forwarding user credentials or UI cookies to the runtime sidecar.

## Tests

Unit tests:

- platform tag selection
- manifest parsing and validation
- checksum mismatch handling
- offline cache hit and cache miss
- atomic install success path
- safe tar rejection still works
- old verified runtime fallback after failed new download

CLI tests:

- `vibe runtime status` with no runtime
- `vibe runtime prepare --offline` cache hit
- `vibe runtime prepare --offline` cache miss
- `vibe runtime prepare` is warning-only by default
- `vibe runtime prepare --strict` returns non-zero on failure
- `vibe runtime clean` keeps current

Release tests:

- wheel has no runtime archives
- wheel includes manifest lock
- manifest contains all six platforms
- manifest sha256 matches uploaded artifacts
- install/upgrade script treats prepare failure as warning by default

Regression:

- clean install through official script prepares runtime
- new Show Page opens without runtime download wait
- upgrade prepares newer runtime and preserves existing session behavior
- offline start reuses verified cache

## Implementation Steps

1. Add manifest schema and test fixtures.
2. Add manifest-cache support to `ShowRuntimeManager`.
3. Keep archive/github/npm as explicit provider overrides.
4. Add `vibe runtime status/prepare/clean`.
5. Update install and upgrade scripts to call `vibe runtime prepare --strict` with
   warning-only behavior by default.
6. Update release workflows to generate and attach runtime manifest/assets.
7. Remove `vibe/show_runtime/*.tgz` from wheel artifacts.
8. Run a full clean-install regression.

## Open Questions

- Should prerelease manifests point to the Vibe Remote release assets or to a
  dedicated `vibe-show-runtime` release?
- How many old runtime versions should `vibe runtime clean` retain by default?
- Should `vibe check-update` report runtime manifest drift separately from
  Vibe Remote package drift?
