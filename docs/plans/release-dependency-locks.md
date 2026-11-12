# Release dependency locks

## Change contract

Release Python environments must use the runtime graph selected by `uv.lock`,
including its Python/platform marker forks. A release carries the same exported
constraints in its wheel, sdist and `dependency-constraints.txt` GitHub asset.
A new installer/upgrade reads **the target wheel's** constraints before running
its resolver; it never applies the currently running release's lock to a newer
one. Direct source/editable/custom development specifications remain explicit
escape hatches, not locked-release installs.

The release build adds the exact runtime graph to distribution metadata while
preserving direct requirements and extras. This is necessary for an unmodified
3.2.5 client: it does not know the new constraints protocol, but its resolver
already obeys `Requires-Dist`. Source/editable developer dependencies retain
their ranges. Rebuilding a released sdist must preserve the same pins.

The lock's native extensions do not support CPython 3.15 (watchfiles' PyO3
refuses it). This release bounds Python to >=3.10,<3.15 rather than publishing
pins that select an unsupported default interpreter. Supported platform forks
include Windows Claude SDK, Intel Mac cryptography and Python 3.10 SQLAlchemy.

## Trust and limitations

Exported dependency hashes are retained for independent verification, but **uv
tool install does not enforce requirement-file hashes**. A hermetic probe with
an intentionally incorrect hash confirmed this. Constraints fix versions; they
are not an artifact-authentication mechanism. The official core wheel is checked
against the SHA-256 in PyPI or the first-party release index, and explicit URLs
with a SHA-256 fragment are checked before its constraints are read. Hashes
served by the same trusted distributor are not independent signatures.

Dependency artifact trust remains the chosen package index. Source distributions
may also invoke build backends whose dependencies are outside the runtime lock.
This change is not a claim of end-to-end hash-verified Python installation.

New official releases without constraints fail before installation; explicitly
selected versions through 3.2.5 (including their prereleases) retain their legacy behavior for repair or
downgrade. Existing generation activation and retirement rules are unchanged.
No live service or user configuration is modified by development verification.

## Mainland access: owner decision required

The proposed patch removes the **automatic** Tsinghua-index fallback and moving
Git HEAD fallback. It does not erase user-supplied package-manager configuration.

A mainland user without a proxy follows these steps:

1. Bootstrap uv and a compatible Python; the existing Astral Python CDN fallback
   remains. If neither bootstrap route is reachable, installation stops there.
2. Read PyPI release metadata and fetch its exact wheel. If metadata is unavailable,
   use the first-party `dl.avibe.bot` release index and its hashed wheel.
3. Resolve only the target release's exact dependency versions on the configured
   index (PyPI by default).
4. If PyPI dependency artifacts are unreachable, a fresh installation stops before
   launcher activation. An upgrade retains the old active environment and reports
   failure. The first-party core-wheel mirror does not mirror all Python dependencies.

**This does not preserve zero-configuration installation for users who currently
need the third-party dependency mirror.** A working alternative is a trusted HTTPS
proxy (set `HTTPS_PROXY` for both the installer and package-manager subprocess),
or a deliberately configured trusted index. Configuring an index is a trust
decision; exact pins alone do not make it equivalent to authenticated artifacts.

Keeping the automatic Tsinghua fallback with exact constraints would retain that
convenience and prevent version drift, but it would still trust same-version
replacement bytes and build-time dependencies from that mirror, because uv tool
ignores the exported hashes. This does not satisfy a hash-pinned-mirror policy.

Recommendation: remove automatic trust switching, explicitly disclose the access
regression, and obtain owner approval before merge. If no-proxy zero-configuration
continuity is mandatory, this PR needs a first-party dependency mirror/wheelhouse
or a genuinely hash-verifying install path; do not call mainland acceptance passed.

## Show Runtime

The normal provider remains its SHA-verified release archive, unchanged for old
users. The explicit npm provider uses a pre-reviewed consumer package/lock project,
`npm ci` and `--ignore-scripts`; it never synthesizes a fresh lock during installation.
`@avibe/show-runtime` is not published on the public registry, so inventing a default
registry lock would not work. See `show-runtime-npm-lock.md` for the opt-in contract.

The separate `vibe-show-runtime` repository's archive bundler currently resolves a
fresh production tree with `npm install`. Fixing that producer is being delivered in a separate
owner-held `vibe-show-runtime` PR with a committed distribution lock and scripts-off
installation. This consumer PR alone does not fix the producer.

## Validation invariants

- Deterministic runtime-only export with hashes; stale input refuses packaging.
- Built wheel, sdist metadata and a wheel rebuilt from that sdist all exact-pin
  the same transitive graph, including marker forks and extras.
- Target constraints are alive throughout the installer process and removed only
  afterward; missing/corrupt releases never reach activation.
- Legacy 3.2.5 artifacts remain explicitly installable; newer missing-lock artifacts
  fail closed. An unmodified 3.2.5 resolver sees pins in the new artifact metadata.
- No automatic third-party dependency index or moving Git source retry.
- npm lifecycle scripts do not execute, package/lock mismatch fails and retained
  runtime remains available on a failed reinstall.
- Owner approves the mainland-access trade-off and supply-chain boundary before merge.
