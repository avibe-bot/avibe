# avibe-memory

`avibe-memory` is the optional in-process Memory implementation for `avibe-os`.
It is distributed only as a same-version GitHub Release wheel and sdist, not
on PyPI. Install core normally and enable Memory in Avibe; the existing
Dependencies reconciler obtains its matching companion from GitHub.

Every built companion requires `avibe-os==X` at its own normalized version X.
Core has no indexed Memory dependency. Its empty `memory` extra is retained
only as a compatibility spelling; it does not install the companion.

The companion source retains the `>=3.0.14.dev0,<4` compatibility window for
source/editable development. Its Hatch build hook replaces that range in every
publishable artifact and fails unless the metadata contains the exact core pin.

## Distribution contract

Build the independent distribution from the repository root:

```console
python -m build packaging/avibe-memory
```

Both release workflows derive one package version from the release tag and use
that version to build an `avibe-memory` wheel and sdist alongside the matching
`avibe-os` wheel and sdist. They run the independent-content, metadata, and
core-only/core-plus-Memory installation matrix before staging release assets.

An official release follows this forward-only order:

1. Verify the runtime assets, both distribution pairs, and their shared version.
2. Finalize the asset-complete GitHub Release before any PyPI publication.
3. Download the public GitHub companion wheel and sdist anonymously over HTTPS.
4. Require the wheel to be byte-identical to the staged wheel and likewise
   require exact sdist bytes.
5. Publish `avibe-os` only after that verification succeeds.

A `gh-v*` GitHub-only release attaches both wheel/sdist pairs and the existing
runtime assets without publishing either distribution to PyPI.

No Memory PyPI account, project, or trusted publisher is required.

Older Avibe versions may request Memory from PyPI during their built-in update,
before new code can run. Migrate through the official core-only installer first,
then start the updated core normally; enabled Memory is reconciled from the
matching GitHub Release. Do not use an old `avibe-os[memory]` upgrade request as
the migration path. GitHub-installed previews retain their original tag through
PEP 610; index-installed core uses its official `v<version>` release.

The package split changes distribution ownership only. The installed import
path remains `avibe_memory`, the host keeps its fixed loader and protocol
constant, and the EverOS artifact manifest remains available as
`vibe/memory_runtime_manifest.json`. Runtime, storage, configuration, and data
formats are unchanged, so a source-compatible Avibe release can load the same
persisted state. Release failures stop at the failed forward step; this contract
does not add automatic package rollback, a rollback plan, lifecycle reservation,
quarantine, a Gate 5 lifecycle verifier, or recovery bootstrap. Successful
upgrades follow the ordinary Avibe restart path. Package installation, upgrade,
and restart failures are structured terminal results and do not prevent a later
explicit attempt. Manifest/hash/fetch/verify and backup safeguards remain release
asset availability controls, not installed-package rollback machinery.
