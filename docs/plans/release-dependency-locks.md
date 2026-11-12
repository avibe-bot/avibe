# Release dependency locks

## Change contract

Release Python environments must use the runtime graph selected by `uv.lock`,
including its Python/platform marker forks. A release carries the same exported
constraints in its wheel, sdist and `dependency-constraints.txt` GitHub asset.
A new installer (`install.sh` or `install.ps1`)/upgrade reads **the target wheel's** constraints before running
its resolver; it never applies the currently running release's lock to a newer
one. Direct source/editable/custom development specifications remain explicit
escape hatches, not locked-release installs.

The release build adds the exact runtime graph to distribution metadata while
preserving direct requirements and extras. This is necessary for an unmodified
3.2.5 client: it does not know the new constraints protocol, but its resolver
already obeys `Requires-Dist`. Source/editable developer dependencies retain
their ranges. Rebuilding a released sdist must preserve the same pins.

The runtime graph supports CPython 3.15 as well as 3.10–3.14. A metadata upper
bound alone did not protect old upgraders or uv's default-interpreter selection:
the previously locked pydantic-core/watchfiles native builds rejected 3.15.
Upgrade only native dependency releases needed for compatible cp315 or abi3
wheels (and their required companions); refresh the other wheel inventories
without changing their versions. Preserve Windows Claude SDK, Intel Mac
cryptography, Python 3.10 SQLAlchemy/Pillow and rpds-py compatibility forks.

Acceptance installs the built wheel and its shipped constraints on a real 3.15
interpreter and exercises the unmodified public 3.2.5 upgrader without new-client
Python flags. New installers and upgrades enforce a Python >=3.10 floor without
pinning an upper bound or a minor; Windows on Arm retains its explicit x64
Python policy. On Windows, the public 3.2.5 client is invoked through its unchanged
`python -m vibe upgrade` entry point: its `vibe.exe` path has a pre-existing
self-replacement defect and is **not** shown to pass by this acceptance. Windows
3.2.5 users can rerun the official `install.ps1`, or invoke that module with the
Python interpreter from their existing Avibe environment. Future clients normalize
Windows argv[0]'s omitted `.exe`, path case and 8.3 aliases before deciding whether
to defer activation. An inherited `VIBE_CURRENT_EXECUTABLE` never proves the current
process holds the launcher. Non-Windows and module-entry behavior stay unchanged;
the existing detached helper still owns replacement/restart after the parent exits.
When the CLI is the Python child of a verified Windows console trampoline, wait
for that trampoline's PID/create-time identity, not just the Python child: the
wrapper can remain loaded briefly after Python exits. A missing/inaccessible or
unrelated parent retains the Python-process wait, never an unrelated long-lived
process. The helper protocol and activation lock remain unchanged.

`scripts/windows_launcher_upgrade_regression.py` separately installs the candidate
wheel and invokes its actual `vibe.exe upgrade` on native Windows x64 and Windows
on Arm (the latter retains x64 Python). It advertises a future version and stages
the same CI fixture wheel into a different environment: this proves future-client
self-replacement, not a released 3.2.5 console-path success or a published future
version. It requires deferred activation, waits only on the post-replace generation
marker, then verifies changed launcher/interpreter identity and the active version.
Focused tests cover extensionless paths, native canonicalization order, module
entry points, inherited hints and the unchanged deferred activation owner. Recovery
copy lives in `docs/UPGRADING.md` and must accompany the 3.2.6 release notes.

Native dependency source builds are disallowed in the acceptance,
except PyYAML 6.0.3 (no cp315 wheel, supported source build). The existing pure
Python http-ece 1.2.1 also remains a source-only package. These exceptions are
explicit build-input trust boundaries, not hash-verified or compiler-free claims.

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

## Mainland access policy

The installers remove the **automatic** Tsinghua-index fallback and moving
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

The selected policy removes automatic trust switching and explicitly discloses
the access trade-off. Install/upgrade failures show literal, shell-appropriate
`HTTPS_PROXY` examples and `UV_DEFAULT_INDEX` (uv) or `PIP_INDEX_URL` (pip)
alternatives; the examples never print configured credentials. Web upgrade advice
is localized, and directs the user to retry from a terminal so its environment
actually reaches the installer. A custom index does not replace the official
core-wheel source, and does not supply a proxy for inaccessible release metadata.

No-proxy zero-configuration continuity would require a first-party dependency
mirror/wheelhouse or a genuinely hash-verifying install path; it is not claimed.
The standard uv path stages the new environment separately. Tests keep a
test-owned process alive and verify failed metadata/download/timeout paths never
activate, restart, or alter the old launcher and discard only the candidate.
This is not a promise of transactional rollback for arbitrary in-place pip errors.

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
- Literal proxy/trusted-index recovery instructions appear on install and upgrade
  failures on both shell families; failed uv downloads preserve the live generation.
