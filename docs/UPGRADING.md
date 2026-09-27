# Upgrading

## Stable launchers

`vibe upgrade` and the installers activate a new version by repointing every
stable `vibe` launcher that already selects an Avibe install on this machine:
the one you ran, every `vibe` on `PATH`, uv's tool bin directory, and the
installer locations `~/.local/bin`, `~/bin`, `/usr/local/bin` and
`/opt/homebrew/bin`. A launcher that points anywhere else, such as a separate uv
tool environment or a wrapper script, is never changed. A launcher is managed
only under the name its install exports, `vibe` or `vibe.exe` on Windows, so an
alias under any other name is left alone.

A root install has one supported launcher: `/usr/local/bin/vibe`. The installer
chooses it when run as root even if another directory on `PATH` already holds a
`vibe`, and later upgrades keep that other launcher on the same version.

If a launcher cannot be moved, for example because it is not writable,
`vibe doctor` fails and lists each launcher with its version. Run
`vibe doctor repair stable-launchers` to point all of them at the newest
installed version, or at the most recent install when two share a version. A
launcher that another installer replaced in the meantime is left as it is, and
the repair fails if the launchers still disagree afterwards. When two different
installs tie as the last activation, the repair cannot choose between them: it
changes nothing and asks you to run the installer again, whose new install moves
every launcher. `vibe doctor` still lists the split, and the downgrade check
below treats neither install as older. On Windows, the launcher you run the repair
from cannot be replaced while it runs, so the repair moves every other launcher
and names the launcher to run it from again.

`vibe start` and `vibe restart` refuse to run an install older than the last
activated one and name the launcher to run instead. An earlier install of the
same version, such as one a reinstall left behind, counts as older; an install
whose version cannot be read never does. Pass `--allow-downgrade` to
run the older build on purpose. Bare `vibe`, which systemd and launchd use, and
the desktop app only warn, so a supervised service still comes up. While the
running service is older than the last activation, `vibe status` includes a
`generation_downgrade` object and `vibe doctor` warns; restart through the named
launcher to run the new version.

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
