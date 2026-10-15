# Upstream dependency maintenance playbook

This is the operational playbook for preparing upstream dependency upgrades in
Avibe. It is designed for one weekly Harness Task and explicit on-demand runs;
it is not a new GitHub scheduler or a universal dependency registry.

## Operating contract

- Prepare one reviewed candidate per dependency at a time. Coalesce repeated
  upstream releases into the latest exact candidate instead of opening a trial
  for every upstream tag.
- Read the current owner Sessions, Runs, Watches, and open PRs before starting.
  Skip a dependency whose candidate is actively owned by another Session or PR
  (in particular, do not compete with the CPA v8.0.29 work in PR #2680). Record
  the owner-held skip in the run report and continue with independent lanes.
- Record exact upstream version/tag, immutable source commit, source URL, patch
  order and checksums before building. Never use `latest`, floating URLs, fake
  digests, or a digest copied from a previous candidate.
- Reuse the existing builder, release guard, manifests, workflows, Runs and
  Watches. A runtime candidate gets focused checks plus exactly one relevant
  local Incus regression E2E. Exploratory checks are evidence, not extra adoption
  gates, unless they expose a reproducible contract failure.
- A successful candidate may prepare or update a PR and carry it through review
  and CI. Merge, official publication, release/tag creation, packaged pin
  adoption, and host rollout remain owner-held. Publish replacement assets before
  changing a packaged manifest.
- No-change runs are silent. Report only a prepared candidate/PR, an owner-held
  conflict, a concrete exception, or a failed required check.

The Task uses the existing Avibe Harness Agent/Runs/Watches. Do not add a
scheduled GitHub workflow, daemon, or live Task in a dependency PR.

## Weekly or on-demand procedure

1. **Preflight ownership.** Inspect `vibe data query` for live Sessions and
   Runs, `vibe watch list`, and `gh pr list --state open`. Do not touch another
   owner's dirty worktree or candidate branch. If a matching owner PR is live,
   coalesce into that work only when its owner explicitly accepts the handoff;
   otherwise report and skip.
2. **Detect exact drift.** Read the owning manifest/declaration and the current
   upstream release/tag API. Resolve the tag to an immutable commit or release
   asset set. Compare version, source identity, patch applicability, license,
   platform set, archive size, and SHA-256. Detection is not completion: a
   usable result is a prepared, tested upgrade PR or a specific exception.
3. **Prepare one candidate.** Use the dependency-specific path below. Keep
   candidate manifests, receipts, offline manifests, and downloaded artifacts in
   a temporary directory outside the exact release-asset directory.
4. **Run focused validation.** Run the owning builder/guard tests and the
   narrow checks for changed boundaries. For a runtime bump, run one relevant
   local Incus E2E through `scripts/incus_regression.py`; use the repository
   runner and preserve the environment. Never hide a failure with retries or a
   larger timeout.
5. **Prepare or update one PR.** Include exact source identity and digests,
   patch decisions, focused results, the single E2E result, and known
   exceptions. Use the normal PR delivery loop: non-draft PR, exact-head Codex
   review, CI, and zero unresolved threads. Do not merge.
6. **Continue safely.** Keep one combined PR/CI Watch for the lane. A patch
   conflict, build failure, E2E failure, missing support contract, or
   non-reproducible asset is an exception report, not a reason to create a
   trial matrix. Stop at the smallest reproducible boundary and name the next
   owner action.

## Dependency-specific paths

### CPA / Model Hub engine

Source of truth and consumers:

- packaged identity: `vibe/model_hub_runtime/cliproxyapi_manifest.json`;
- compatibility patches and ordered metadata:
  `patches/model-hub-cpa-*/README.md` and the patch files;
- candidate builder: `scripts/build_model_hub_engine.py`;
- unchanged byte/asset guard:
  `scripts/model_hub_engine_release_guard.py`;
- relevant contract checks: `tests/test_model_hub_engine_build.py` and
  `tests/test_model_hub_engine_release_guard.py`;
- relevant local regression: the existing installed-service Model Hub
  acceptance in `tests/e2e/test_model_hub_service_acceptance.py`, run once with
  a candidate manifest and synthetic loopback upstreams.

Preparation:

1. Resolve the upstream CPA release tag to its exact source commit. Start from
   the current patch set, apply patches in recorded order with
   `git apply --check`, and remove a patch only when its README drop condition
   and the upstream source both prove it is no longer needed.
2. Create a temporary candidate manifest outside the output directory. It keeps
   the exact source identity, build metadata, ordered patch entries, version,
   `asset_release_tag`, and the six Avibe-owned planned URLs, but contains no
   `size_bytes`, `sha256`, or `binary_sha256` fields. Do not edit the packaged
   manifest for this step.
3. Use the first-class candidate mode, which reuses the strict build/archive
   loop and official builder flags:

   ```bash
   python3 scripts/build_model_hub_engine.py \
     --candidate \
     --manifest "$TMP/cpa-candidate-manifest.json" \
     "$TMP/cpa-candidate-assets"
   ```

   The output directory must be fresh and contain only the six archives, six
   checksum sidecars, and the generated `model-hub-engine-manifest.json`.
   Candidate mode computes all six real archive sizes, archive hashes, binary
   hashes, and owned planned URLs, then runs the same release-asset verifier.
   It does not publish, call `gh`, or modify the packaged manifest.
4. Before the installed-service E2E, materialize the target archive locally. For
   the arm64 Incus target, create an offline manifest outside the exact asset
   directory and point only `linux-arm64` at the container-local archive path:

   ```bash
   python3 - "$TMP/cpa-candidate-assets/model-hub-engine-manifest.json" \
     "$TMP/cpa-offline-manifest.json" <<'PY'
   import json
   from pathlib import Path
   import sys

   source = Path(sys.argv[1])
   destination = Path(sys.argv[2])
   manifest = json.loads(source.read_text())
   target = next(item for item in manifest["assets"] if item["platform"] == "linux-arm64")
   target["url"] = (Path("/tmp/avibe-candidate-assets") / Path(target["url"]).name).as_uri()
   destination.write_text(json.dumps(manifest, indent=2) + "\n")
   PY
   ```

   Use the existing `scripts/incus_regression.py` Runner/`tenant_exec` helpers
   to transfer that offline manifest and the matching generated archive into
   `/tmp/avibe-candidate-assets` in the test-owned Incus target. Do not publish
   the planned Avibe URL and do not touch the runner service at `/opt/avibe/venv`.
5. Verify the generated manifest again if a separate step consumes it:

   ```bash
   python3 scripts/model_hub_engine_release_guard.py \
     --manifest "$TMP/cpa-candidate-assets/model-hub-engine-manifest.json" \
     verify --asset-dir "$TMP/cpa-candidate-assets"
   ```

6. Run the focused builder/guard tests, then run the existing installed-service
   acceptance once with
   `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH=/tmp/avibe-candidate-assets/offline-manifest.json`
   (and the candidate normal wheel) against synthetic loopback upstreams. Record
   the real result and prepare the PR with the candidate manifest/patch
   metadata. The existing release workflow remains the publication/recovery
   authority; do not change the packaged pin or publish from candidate mode.

Strict pinned mode remains the recovery and publication path: it must verify
bytes against the checked-in manifest and reject mismatched archives. A
candidate's output manifest is new evidence, not permission to adopt it.

### `claude-agent-sdk`

This is a runtime dependency, not an ordinary bulk update. It bundles a Claude
Code CLI and has a separate Windows wheel constraint in `pyproject.toml`.
Dependabot excludes it from the generic `uv` lane.

When an explicit SDK candidate is requested:

1. inspect both the declarations in `pyproject.toml` and the corresponding
   `uv.lock` entries, including the Windows marker;
2. record the SDK version and bundled CLI version exactly;
3. run the existing installed-service Claude path with a fresh home,
   loopback/fake credentials, and the relevant local Incus E2E once;
4. update the declaration and lock together only after the runtime evidence is
   understood; preserve the Windows constraint or report an upstream wheel
   exception;
5. keep startup failures visible. Do not turn an SDK startup timeout into a
   passing result by increasing timeouts or adding retries.

### Git runtime

The active path is the manual build workflow and its manifest, not Dependabot:

- version/source/checksum inputs and Avibe hardening are in
  `.github/workflows/build-git.yml`;
- the consumed pinned output is `vibe/git_runtime_manifest.json`;
- release verification/recovery is `.github/workflows/git-runtime-release-guard.yml`
  and `scripts/git_runtime_release_guard.py`;
- the mirror follows published releases through
  `.github/workflows/release-mirror.yml`.

Resolve the upstream kernel.org tarball and SHA-256, update the workflow input
and manifest as one candidate, build the four supported platforms, run the Git
runtime guard and focused tests, then run one relevant local Incus runtime
check. Publication and packaged adoption remain owner-held.

### tmux runtime

The manifest is `vibe/tmux_runtime_manifest.json`; the current build entry point
is `.github/workflows/build-tmux.yml`. That workflow still packages scaffold
placeholders and explicitly refuses publication. This is a real support
exception, not a successful upgrade path:

- do not publish or adopt a tmux candidate from the scaffold;
- report the missing cross-build contract (real static/relocatable tmux with
  utf8proc, terminfo, archive and binary verification);
- only after that contract exists, update the exact manifest and run its focused
  checks plus one relevant local regression.

### ripgrep and cloudflared

These are upstream release-asset consumers, not source builds in this repo:

- `vibe/ripgrep_runtime_manifest.json` pins version, release URLs, archive
  format, size, archive SHA-256, binary SHA-256, and binary path;
- `vibe/cloudflared_runtime_manifest.json` carries the same contract for its
  tarballs or raw binaries;
- `.github/workflows/release-mirror.yml` mirrors published releases and is the
  availability guard.

For either dependency, resolve the upstream release, update every platform
entry with exact immutable asset names and digests, run the existing manifest
and runtime-consumer tests, and perform one relevant local Incus check. If an
upstream asset changes format, platform coverage, signing, or binary path,
report that contract exception instead of silently broadening the installer.

### Desktop toolchains

The active desktop inputs are explicit source manifests:

- `desktop/runtime-sources.json` pins the private Python build, Node archive,
  npm version, and the Show Runtime manifest;
- `desktop/cua-driver/sources.json` pins the CUA driver release, source commit,
  tool snapshot checksum, platform asset checksum, and patch contract;
- `desktop/scripts/build-runtime-bundle.py` downloads with SHA-256 checks,
  exports the frozen Python requirements, installs with required hashes, and
  builds the relocatable bundle;
- `desktop/cua-driver/PATCHES.md` records when the CUA patch may be dropped;
- focused checks include `tests/test_desktop_runtime_bundle_builder.py` and
  the existing desktop package/update checks.

For a Python/Node/npm candidate, update the exact URLs and hashes together,
rebuild the affected target bundle, verify the expected npm and Show Runtime
identity, and run one relevant desktop smoke/regression path. For a CUA driver
candidate, resolve the release tag and source commit, refresh the tool snapshot
and patch applicability, rebuild the helper, and verify the macOS raw-click and
parent-liveness contracts. A missing Windows or macOS build/signing contract is
an explicit exception; do not fill it with a placeholder archive.

## Ordinary dependency automation

`.github/dependabot.yml` covers:

- ordinary npm dependencies in `/ui` and `/desktop`;
- the desktop Cargo workspace in `/desktop`;
- the root `uv` ecosystem, with `versioning-strategy: increase` so the lane
  explicitly targets declaration-bound maintenance while updating `uv.lock`
  when applicable.

A uv PR is accepted only when it contains a meaningful `pyproject.toml`
requirement change together with the corresponding `uv.lock` update; a lock-only
change is an exception, not ordinary declaration maintenance. The lock is
consumed by desktop packaging and focused frozen-lock jobs, while most ordinary
lint installs use `uv pip install --system -e . --group dev` and resolve the
project declarations directly.

Minor and patch updates are grouped per ecosystem. Major updates remain
individual, reviewable PRs. Each lane has a small open-PR limit and no
auto-merge. `claude-agent-sdk` is ignored by the generic `uv` lane because its
bundled CLI and Windows marker require the runtime path above. Dependabot does
not open sibling-repository PRs in this lane.

## Canonical Harness Task message

Use this as the Task body after the playbook and a dry run have been reviewed;
the implementation PR does not create or enable the live schedule:

```text
Run the Avibe upstream dependency maintenance playbook at
 docs/maintenance/upstream-dependency-maintenance.md.

Cadence: every Monday at 09:00 Asia/Shanghai, plus on-demand manual runs.
Inspect live Sessions/PRs first and skip owner-held candidates (especially
CPA2680). Coalesce to one candidate per dependency. For each actual drift,
resolve exact source/version/asset digests, prepare a focused upgrade PR, run
its focused checks and exactly one relevant local Incus regression E2E for
runtime bumps, and keep one combined PR/CI Watch through review and CI.

Do not spam when there is no change. Report only a prepared/tested PR, an
owner-held skip, or a concrete exception such as patch conflict, failed E2E,
missing builder/support contract, or non-reproducible bytes. Never merge,
publish, change a packaged pin, restart a host, or create sibling-repo PRs;
those remain owner-held.
```

An on-demand run uses the same message and cadence rules; only the trigger
changes. The task's success condition is prepared, tested, reviewable work or
a concrete exception—not a version-detection notification.
