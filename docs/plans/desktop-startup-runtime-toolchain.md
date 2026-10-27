# Desktop startup: keep external npm out of the immutable Runtime

## Background and goal

Desktop's discovery phase includes Runtime preparation, not just a health probe.
An unprefixed external `npm install --global` can run on the bundled Node because
`RuntimeCommand::private` prepends its tools directory to every descendant's
`PATH`. npm then derives its default global prefix from that Node installation,
adds files to the immutable Runtime, and causes a later launch to extract a
repair slot. A diagnostic npm log confirms this Node/npm combination and a
successful global install; the exact initiating process is not established.

Prevent this cross-toolchain pairing without changing endpoint/config discovery,
Runtime integrity checks, repair-slot reuse, or a running service.

## Change contract

- Only the bundled `vibe` entry-point directory leads the Runtime-wide PATH.
  External Node/npm retain the user's login-shell selection.
- Avibe's own backend installer, Show Runtime, and computer-use Node consumers
  retain their existing explicit bundled Node/npm handles. Backend installs keep
  their explicit mutable staging prefix and isolated npm config/cache.
- Do not inject global `PREFIX` or `NPM_CONFIG_PREFIX`, override user npm config,
  create endpoint caches, or add another mutable toolchain installation.
- No release, deployment, live service restart, active-slot deletion, or user
  config modification is part of this source change.

## Scope and consumers

The owner is the Rust private Runtime launch environment; the bundle builder's
probe environment mirrors that contract and must not hide a shared-PATH dependency.
Python consumers already
select private Node explicitly (`vibe.desktop_backends`, `core.show_runtime`,
`core.computer_js_kernels`, and `vibe.computer_script_runner`). Published private
backends are native executables, not npm JavaScript shims. Dedicated backend
installation additionally prepends Node only in its isolated subprocess env.

External tools that require Node must use their own Node installation, as they do
in a terminal; the bundled toolchain is not a general-purpose user installation.
Existing contaminated slots remain intact. The existing repair-slot mechanism
handles them without deleting the Runtime a live service may still use.

## Validation plan and test rationale

- Extend the existing cross-platform launch-environment contract: retain the
  private vibe shim and explicit Node/npm handles, but exclude bundled Node from
  inherited PATH. Its pre-fix failure is the extra tools directory.
- Extend the private login-PATH coverage with a Unix subprocess
  regression: an external npm-shaped `#!/usr/bin/env node` launcher must select
  the external Node fixture, not the bundled one. This tests actual shebang/PATH
  resolution rather than simulating npm's prefix algorithm. Existing tests only
  asserted the old, harmful PATH order.
- Run a real offline npm write probe with copied (not symlinked) Node in scratch,
  isolated HOME/config/cache and a local fixture package. Compare old/new PATH:
  only the former should write beneath the scratch immutable Runtime. Never
  install with production Node's implicit prefix.
- Extend the existing bundle-probe environment test for its distinct packaging
  risk: the release smoke probe must use the launcher's PATH contract, rather than
  masking a consumer's accidental dependency on globally available bundled Node.
  It failed before the builder correction because of the extra tools directory.
- Run focused Rust launcher tests, runtime-host tests, fmt and clippy; run existing
  Python private backend and explicit Node consumer tests with hermetic state.
- Open a PR and require exact-head Codex review and applicable CI before delivery.

## Progress

- [x] Correlate Runtime contamination with npm command metadata.
- [x] Withdraw speculative endpoint-cache prototype; net diff contains no cache.
- [x] Inventory explicit private Node consumers.
- [x] Implement the launch environment correction and regression coverage.
- [x] Complete focused validation and offline npm write probe.
- [ ] Complete review and CI.

## Validation evidence

- The new shebang regression failed before the fix with `bundled-node` instead of
  `user-node`, and passes afterward. The bundle mirror assertion also failed on
  its old extra PATH entry and passes with the corrected environment.
- A real offline npm local-tarball install, with copied Node executables and every
  config/cache/home path in scratch, wrote into `Runtime With Spaces/tools` with
  the old PATH and only into `user-node` with the new PATH. The Runtime fingerprint
  stayed unchanged after the corrected install. No real install was modified.
- Rust formatting and runtime-host clippy (`-D warnings`) passed. The full
  runtime-host suite passed with `--test-threads=1`: 184 unit tests, 87 integration
  tests, and one doc test. A default-parallel run hit the unchanged login-path
  subprocess-start timeout test; that test passed alone and in the serial suite.
- Existing Python desktop backend/runtime tests passed (161 tests). A separate
  run with a scratch-owned Node passed all 127 bundle-builder, computer JS kernel,
  and computer script SDK tests. HOME, Avibe home, XDG state, and credentials were
  isolated. Dependencies live in this worktree's own venv; its local source-path
  entry supports the existing isolated-interpreter bundle test without building
  or installing any UI onto the user's service.
- Real installed Desktop reopening remains a post-build manual check. This patch
  prevents repeated repair caused by this PATH leak; it does not remove the
  expected initial extraction for a new Runtime version or claim instant startup.
