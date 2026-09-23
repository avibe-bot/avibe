# Upgrading

## Memory removal

Avibe no longer includes the optional Memory product. Upgrading does not delete
existing Memory data under `~/.avibe/memory` or the legacy `~/.vibe_remote/memory`
path. The old `memory` configuration is ignored, and the new service does not
start a Memory sidecar.

### From an official release

Use `vibe upgrade` to install the next published official release. If Avibe is
running, this command arranges the service and Web UI restart; if it is stopped,
the new version takes effect on the next start.

An installed v3.1.0 with Memory enabled or the companion installed asks for a
matching `avibe-memory` wheel before replacing the core package. New releases
include a metadata-only compatibility wheel for that old updater. It contains no
Memory modules and does not enable Memory in the new service. Fresh installs do
not need the compatibility wheel.

### From a GitHub-only pre-release (`gh-vX.Y.ZrcN`)

For a pre-release-to-pre-release update, download the **`avibe_os-*.whl` core
wheel** from the target tag's GitHub Release. Replace the existing uv tool using
that downloaded wheel, for example:

```bash
uv tool install --force "/path/to/downloaded/avibe_os-<version>-py3-none-any.whl"
```

Replace the example path with the actual wheel path. Do not add `[memory]` or
`--with avibe-memory`. This command rebuilds the tool environment from the wheel
and drops the old Memory package; specify any other extra packages you still
need again. Older pre-release `vibe upgrade` commands may instead try to resolve
a Memory package from the package index, so do not use them to move between
GitHub-only pre-releases.

`uv tool install` replaces the installed files but does not switch an already
running Avibe service to the new code. Stop the old service before replacing the
tool, then start Avibe and check `vibe status`. Stopping Avibe interrupts active
sessions; perform this from a separate terminal if your current session runs
inside Avibe. Existing Memory data is not part of the uv tool environment and
remains in place.
