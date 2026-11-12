# Show Runtime npm installs from reviewed projects

## Contract

The default remains `manifest-cache`: the release supplies a manifest and the
client verifies the selected prebuilt archive. There is no npm fallback when a
manifest is absent. The npm provider is an explicit advanced configuration.

New npm installations and forced replacements must consume an existing,
reviewed consumer `package.json` and `package-lock.json`, never resolve a fresh
lock during installation. Lifecycle scripts are disabled. A failed installation
or validation must preserve the prior runtime and its current pointer.

## Configuration and migration

Set `VIBE_SHOW_RUNTIME_SOURCE=npm` and
`VIBE_SHOW_RUNTIME_NPM_PROJECT=/absolute/path/to/reviewed-project`. That directory
must contain a standalone consumer project with `@avibe/show-runtime` in
`dependencies` and a version 2 or 3 npm lockfile. Avibe copies only those two JSON
files to its private staging directory. It never copies `.npmrc`, scripts,
`node_modules`, workspace sources, or other project files.

The lock must use HTTPS tarball resolutions and SHA-256 or SHA-512 integrity for
all installed packages. Local directories/tarballs, workspaces, bundled packages,
git dependencies, npm aliases, and dependency overrides are not supported by
this provider. Build and
review custom packages separately, publish their built tarballs, and lock their
HTTPS URLs and integrity; alternatively use `VIBE_SHOW_RUNTIME_BIN` to select an
already built, explicitly managed runtime. Package scripts are never a build
mechanism here. npm itself checks manifest/lock consistency with `npm ci`.

`VIBE_SHOW_RUNTIME_PACKAGE_SPEC` alone no longer authorizes a network install.
The default/bare `@avibe/show-runtime` delegates version selection to the reviewed
project. A deliberate custom spec must exactly match either that dependency's
value or `@avibe/show-runtime@<dependency-value>`; Avibe never changes the project
to accommodate a mismatch. Missing/invalid projects and mismatched specs are
configuration failures with `change_setting` recovery. The installation log
explains how to correct them without echoing potentially credentialed specs.

Previously installed npm runtimes remain usable without a project, including
when offline. Forced repair requires the reviewed project; failure reports an
unsuccessful operation while retaining the working runtime. Changing the project
is not an automatic replacement request: explicitly repair to adopt its new
lock. The published install records the manifest and lock digest for provenance,
not as a claim that earlier installed bytes have been retroactively verified.

## Installation

Read and validate the two inputs once, then stage those exact bytes. Run
`npm ci --ignore-scripts --omit=dev --include=optional --no-audit --no-fund`
against the staging directory, adding `--offline` when requested. Optional
platform packages remain enabled for native dependencies. There is no
`npm install`, lock-generation command, implicit script allowance, or retry
with weaker flags. Retain the existing immutable-tree publication, candidate
validation, reference guard, and rollback behavior.

The supplied project is an explicit trust decision, not a sandbox: the selected
runtime executes when started. Integrity locks artifacts; it does not establish
that the artifacts are benign. No builtin registry lock is fabricated: on
November 12, 2026 public registry reads for `@avibe/show-runtime` and
`@avibe/show-ui` returned 404. Upstream workspace package manifests still use
`0.0.0`. Publishing those packages or changing upstream archive production is a
separate task.

## Validation plan

The provider boundary owns the primary tests. Existing tests only mocked
`npm install` and did not protect against fresh resolution or install scripts.
New tests assert the exact frozen-install flags and copied input bytes, reject
missing/unpinned/local source inputs before spawning npm, reject conflicting
custom specs, and prove that invalid configuration leaves a retained runtime
usable. Existing immutable-publication and failed-replacement tests must supply
reviewed projects and continue proving the same rollback guarantees. A real npm
fixture verifies that lifecycle scripts do not execute and that npm rejects a
manifest/lock mismatch rather than rewriting the lock. All test state is
hermetic; no runtime service is restarted.
