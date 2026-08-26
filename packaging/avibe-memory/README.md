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

For the first `avibe-os` release that exposes the `memory` extra, publish the
matching `avibe-memory` release first and verify that the package index serves
it before publishing `avibe-os`. This order is required because the host extra
must resolve at the moment it becomes public.

The package split changes distribution ownership only. The installed import
path remains `avibe_memory`, the host keeps its fixed loader and protocol
constant, and the EverOS artifact manifest remains available as
`vibe/memory_runtime_manifest.json`. Runtime, storage, configuration, and data
formats are unchanged, so the previous source-compatible Avibe release can
load the same persisted state. Upgrade and rollback package-shape planning is
owned by the subsequent migration wave.
