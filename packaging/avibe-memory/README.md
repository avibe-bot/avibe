# avibe-memory

`avibe-memory` is the optional in-process Memory implementation for `avibe-os`.
Install it through the host extra so the resolver selects a compatible pair:

```console
pip install "avibe-os[memory]"
```

The `3.0.x` package line implements Memory runtime protocol `1` and requires
`avibe-os>=3.0.14.dev0,<3.1`. The host extra applies the same compatibility
range to this distribution.

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
3. Publish `avibe-memory` with trusted publishing and skip-existing semantics.
4. Retry a no-dependency, no-cache PyPI download and require its wheel to be
   byte-identical to the staged wheel.
5. Publish `avibe-os` only after that verification succeeds.

A `gh-v*` GitHub-only release attaches both wheel/sdist pairs and the existing
runtime assets without publishing either distribution to PyPI.

The first official publication requires the PyPI pending/trusted publisher for
project `avibe-memory` to match repository `avibe-bot/avibe`, workflow
`publish.yml`, and GitHub environment `pypi-avibe-memory`. That external
configuration is an operator prerequisite; the repository workflow does not
create it.

The package split changes distribution ownership only. The installed import
path remains `avibe_memory`, the host keeps its fixed loader and protocol
constant, and the EverOS artifact manifest remains available as
`vibe/memory_runtime_manifest.json`. Runtime, storage, configuration, and data
formats are unchanged, so a source-compatible Avibe release can load the same
persisted state. Release failures stop at the failed forward step; this contract
does not add automatic package rollback, lifecycle reservation, or recovery.
