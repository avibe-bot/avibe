# Regression Testing

`回归测试` is the manual regression workflow for this repository. It runs Avibe
inside Incus so the environment behaves like a real long-running Linux machine:
systemd service, real home directory, persistent state, source sync, service
restart, and Show Runtime preparation.

It complements automated `E2E` tests instead of replacing them:

- `E2E testing` keeps using scripts and pytest for automatable scenarios.
- capability scenario metadata lives under `tests/scenarios/`
- multi-step auth/setup journeys should add or update
  `tests/scenarios/auth_setup/catalog.yaml` and
  `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`
- `docs/regression/` is a human-facing entry layer, not the canonical source of
  truth for scenario metadata
- `Regression testing` is for human-triggered checks on real IM platforms.

## Scenario Metadata Navigation

Start here only if you are doing manual regression or need the human-readable
index.

For deterministic scenario metadata, read:

1. `tests/scenarios/INDEX.yaml`
2. `tests/scenarios/<capability>/catalog.yaml`
3. `tests/scenarios/<capability>/observations.yaml`
4. `tests/scenarios/<capability>/test_*.py`

## Runtime Model

The regression runner manages two **local Incus** environment types:

- `master`: a long-running persistent regression environment.
- `worktree`: a temporary isolated environment for the current git worktree.

The master environment keeps product state across normal updates:

- platform credentials,
- Avibe Cloud remote-access pairing,
- agent CLI homes,
- Harness/session state,
- Show Page workspaces,
- Show Runtime cache where safe.

Worktree environments get their own Incus project/instance and host port. Their
mapping is recorded under `.runtime/incus-regression/worktrees.json` in the
primary checkout.

On macOS, run the Incus daemon in a local Linux VM and use the local machine as
the operator/client. Development regression is local Incus only; do not use
remote Incus hosts, remote tenant instances, demos, or customer/user
environments for project testing.

## Setup

Source synchronization requires `rsync` on the operator machine and in the
container. The regression base image already includes it; on a Linux operator
machine, install the distribution's `rsync` package if needed. The macOS system
rsync is supported. Transfers use Incus exec as the existing service user, not
an SSH daemon or host ownership metadata.

1. Configure the local Incus host.

   ```bash
   python3 scripts/incus_regression.py doctor
   ```

   If you are initializing a fresh Linux host directly:

   ```bash
   python3 scripts/incus_regression.py init-host --minimal
   ```

2. Build or provide the reusable base image.

   ```bash
   python3 scripts/incus_regression.py build-base
   ```

   The base image contains slow-changing dependencies such as Python, Node,
   build tools, and agent CLIs. Normal code updates do not rebuild this image.

3. Copy the local env template:

   ```bash
   cp .env.regression.example .env.regression
   ```

4. Fill in `.env.regression` with:

- shared LLM credentials: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- optional API base URLs: `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `OPENAI_API_BASE`
- optional UI proxy bind host: `REGRESSION_PORT_BIND_HOST`
- platform-specific bot credentials for Slack, Discord, Feishu, and WeChat
- the target regression channel for each platform, if you want channel routing
  preseeded at startup
- the backend that each platform's channel should pin to by default

Channel IDs are optional. If you leave them empty, the environment still starts
and you can configure channels later from the Web UI.

5. Keep these local-only files out of git:

- `.env.regression`
- `.runtime/incus-regression/`

## Usage

The compatibility entry point now uses Incus by default:

```bash
./scripts/run_regression.sh
```

Normal updates synchronize source incrementally using rsync's size/mtime quick
check. Unchanged files are not rewritten; stale source is deleted. Excluded
dependency/cache/runtime directories are pruned and preserved. `--no-build-ui`
includes local `ui/dist` in synchronization, so stale assets are deleted there
too. `--clean` remains an explicit full source reset, including generated files;
it does not reset product state. If a tool deliberately changes source while
preserving both its size and timestamp, use `--clean` to force replacement.

Environment files are different from reusable caches: host `.env` and `.env.*`
entries are never sent, and stale entries in the receiver's source directories
are deleted during normal synchronization. Excluded runtime/dependency trees
remain outside this cleanup boundary.

Show Runtime resolves the upstream commit on each update, but builds only when
that commit, the target Node/platform/npm versions, the build recipe, or the
archive checksum changes. The archive and its build receipt are kept together
in the regression cache. Network resolution failures remain errors, not silent
permission to deploy a stale build. Runtime preparation and validation still run
on every update.

Updates and base-image builds from worktrees of the same primary checkout share
one heavy-I/O slot per daemon. An update waits for that slot before creating an
instance or stopping its existing service, and holds it through recovery if the
update fails. Other environments keep serving while it waits. Old runner versions
and independently cloned repositories do not participate in this shared lock.

Direct runner commands:

```bash
python3 scripts/incus_regression.py up --target master
python3 scripts/incus_regression.py status --target master
python3 scripts/incus_regression.py logs --target master
python3 scripts/incus_regression.py shell --target master
python3 scripts/incus_regression.py down --target master
```

Temporary worktree environment:

```bash
python3 scripts/incus_regression.py up --target worktree
python3 scripts/incus_regression.py status --target worktree
python3 scripts/incus_regression.py delete --target worktree --yes
python3 scripts/incus_regression.py reconcile
python3 scripts/incus_regression.py reconcile --yes
```

Delete worktree environments promptly after the worktree is merged, abandoned,
or removed. The persistent `master` environment should stay running and preserve
its product state across normal source updates.

`reconcile` answers "what is actually still here?". It enumerates worktree
environments from Incus rather than from the runner's metadata, so an
environment created outside the runner shows up too — otherwise it is invisible
to every command that reads `.runtime/incus-regression/worktrees.json`, and can
only be removed by hand. For each one it prints what Incus was observed to hold
— its project and its instance, reported separately, because an environment can
be half gone — plus whatever provenance the metadata holds and the `delete`
command that removes it. Every instance Incus reported under that name is listed,
because Incus scopes instance names per project and one name can be several
instances. An instance living in a project other than the one its slug implies is
listed with that project and warned about by hand: `delete --slug` derives the
project from the slug, so it would report success without touching the instance.
An environment holding both kinds gets both the command and the warning — one
statement covers what the convention reaches, the other what it does not.

Those names are the ones the daemon reported, never derived a second time from
the slug. A slug here is an observed name with a known prefix removed, so it is
bounded by what Incus accepts rather than by what the runner would choose — and
`--slug` is stricter than Incus. A discovered name the runner would not have
minted therefore gets no command at all: the report names its project and
instance for a manual reclamation instead of printing a `delete --slug` that
would exit on its own argument.

Deletion stays a separate, explicit call. Nothing recorded about an environment
can prove it is no longer wanted: the recorded path is the checkout the runner
was invoked from, shared by every environment created there and still present
long after that worktree is gone; the slug is chosen by the caller; and an
environment may sit on a detached HEAD with no branch whose merge status could
be checked. `reconcile --yes` therefore changes exactly one thing — it drops
metadata rows for environments Incus no longer has, releasing their reserved
host ports. Without `--yes` it only reports, and `--dry-run` withholds the write
even with it.

Reporting is what the command is for, so having something to report is not a
failure: `reconcile` exits 0 whether or not any row was stale. A non-zero exit
for the case the command exists to show cannot be told apart from a command that
broke, by a `&&` chain, a CI step, or anyone reading `$?` — while the report it
just printed says the run went fine.

"No longer has" is read strictly, because releasing a port that is still in use
is worse than keeping a row nobody needs:

- The daemon must have completed a listing that held neither the environment's
  project nor its instance, and every entry in that listing must have been
  readable. A daemon that could not be reached, or an entry the runner could not
  identify, is an unanswered question rather than an absence, and aborts instead.
- A row whose slug a live `up` is still holding is left alone. `up` records the
  slug and its port before it creates the project and the instance, so this is
  what a concurrent `up` looks like from the outside. Who holds a slug is asked
  of the kernel, not of the file: an `up` takes that environment's update lock —
  named by the daemon and the project, so a `--remote` run is never read as the
  local environment of the same name — before it writes the row and holds it until
  the environment is built, so `reconcile` answers the question by trying to take
  the same lock. Every command that changes what a slug names holds it, not just
  `up`: a `delete` takes it across removing the objects and forgetting the row,
  and stops rather than waiting when somebody else has it. Those two halves are
  one change, and a delete that split them around a live `up` would remove
  nothing — the objects do not exist yet — drop that run's reservation anyway,
  and leave the finished environment untracked with its host port free for the
  next slug. Nothing a run writes
  could answer it. A record over-reports — a run killed outright leaves its own
  behind — and under-reports, since a run from an older checkout writes no field
  a newer `reconcile` would recognise; a lock does neither, because the kernel
  drops it however the run ends and every version of the runner that builds a
  worktree environment takes the same one. So an abandoned reservation is
  reported as an environment the daemon no longer has and `--yes` prunes it,
  while a live run's row is kept without any operator having to tell the two
  apart. Not knowing counts as held: a platform without `flock`, or a lock file
  that cannot be opened, keeps the row like every other unanswered question here.
  Two things the rule does not cover, and both are in older checkouts rather than
  in the rule. Their `up` writes the row and then takes the lock, so a
  `reconcile --yes` landing between the two prunes a reservation whose build is
  about to start. What that exposes is those two operations and not the build:
  between releasing the mapping lock and acquiring the update lock they run one
  `git rev-parse` and the two lock calls, and contention shortens the window
  rather than widening it, since a slug whose lock somebody is already holding
  reads as in flight. The older run then repairs the row itself, because its `up`
  rewrites the mapping when the build finishes. What is left of it needs a third
  `up` to start inside that same window and take the freed port, and that ends
  loudly: an `up` creating a local environment re-checks the host port inside its
  own update lock before it builds. Their `up` also keys the lock on the project
  alone, so a released `up --remote` takes the local environment's lock and reads
  as a local run — which errs towards in flight, and keeps a row. Closing either
  from this side would mean deciding on the row's stamp again, written by a clock
  this command cannot check and no evidence about whether a run is live, or never
  pruning a row without a claim, which is every row any release has written.

  Giving a row back at the end of a failed `up` is still worth doing — it frees
  the port now instead of at the next `reconcile --yes` — and both of its
  conditions are read at that moment rather than remembered from an earlier one.
  The daemon must report no project for the slug, since a project it holds may
  bind that port. The row must also still be the one that run wrote, which is
  what the opaque claim in it is for: `up` merges over whatever row it finds, so
  a later run can take the row over, and the first one failing afterwards must
  not free a port the second is building on. `reserved_at` and `updated_at` are
  provenance for whoever reads the file, and nothing decides on them: a stamp
  written by another machine's clock, or by this one before it was corrected, is
  no evidence about whether an `up` is running right now. For the same reason a
  reservation records no branch or commit until it completes — the run that built
  the environment is the one that can say what it built — so a slug reserved
  again reports the branch it is still running, not the one being installed over
  it.
- `worktrees.json` is reached only through an accessor bound to the daemon it
  describes. The file reserves host ports on this machine and records what this
  machine's daemon holds, so every read of it and every write to it is a claim
  about exactly one authority, and a `--remote` command has no name for it: it
  neither reads nor writes a byte. `reconcile --remote` reports the remote
  inventory and says once that runner metadata is not shown; `delete --remote`
  removes the remote environment and says it kept the local row; `up --remote`
  requires `--host-port`, because allocating from this machine's reservations is
  no evidence about which of another daemon's ports are free. The `delete`
  commands `reconcile --remote` prints carry `--remote`, or they would name the
  same slug on the wrong daemon. The lock over that file belongs to the same
  accessor, for the same reason: a `--remote` command holds nothing of this
  file's inside its listings, so it does not keep local runs out of the file
  while it waits on another daemon.
- An empty `--remote` names this machine's daemon, normalized once where the
  flag is defined rather than at each reader. An unexpanded
  `--remote "$INCUS_REMOTE"` would otherwise be two authorities at once: the
  environment created here, while the accessor bound to some other daemon and
  recorded nothing about it — leaving the host port allocated with no row naming
  it.

Useful flags:

- `--host-port <port>`: set the host-side Web UI proxy port.
- `--slug <slug>`: set the worktree environment slug.
- `--reset-mode config`: re-seed config/state/runtime.
- `--reset-mode all`: wipe and re-seed the environment state.
- `--clean`: compatibility flag; normal syncs already remove stale source files.
- `--force-deps`: force Python dependency refresh.
- `--no-build-ui`: skip UI asset build.
- `--dry-run`: print the planned Incus commands without changing the host.

The wrapper maps common legacy flags:

```bash
./scripts/run_regression.sh --status
./scripts/run_regression.sh --logs
./scripts/run_regression.sh --worktree
./scripts/run_regression.sh --reset-config
./scripts/run_regression.sh --dry-run
```

## What You Get

On success, the runner prints one local UI URL:

```text
Incus regression environment is ready:
  URL: http://127.0.0.1:15130
  Target: master
  Project: avr-master
  Instance: avibe-master
  Show Runtime source: archive
```

Default names:

- master project: `avr-master`
- master instance: `avibe-master`
- master URL: `http://127.0.0.1:15130`
- worktree project: `avr-wt-<slug>`
- worktree instance: `avibe-wt-<slug>`
- worktree ports: allocated from `15200-15399` unless overridden

## Architecture

The Incus runner separates slow-changing dependencies from fast-changing source:

- **Base image**: Ubuntu plus Python, Node, build tools, systemd unit helpers,
  and agent CLI prerequisites.
- **Source sync**: current worktree source is streamed into
  `/opt/avibe/source`, excluding `.git`, `.runtime`, dependency directories, and
  generated assets.
- **Service**: Avibe runs under `avibe-regression.service` as user `avibe`.
- **Home**: `/home/avibe/.avibe` is the active product state home;
  `/home/avibe/.vibe_remote` is a compatibility symlink.
- **Build identity**: the version badge and `/api/version` report the commit
  recorded by the latest source sync separately from install-time package
  metadata. Source targets do not use that package metadata for update prompts.
- **Show Runtime**: every successful update runs `vibe runtime prepare --strict`
  and then verifies `vibe runtime status --json`.

The runner fingerprints dependency inputs:

- Python dependencies: `pyproject.toml`, `uv.lock`
- UI dependencies: `ui/package.json`, `ui/package-lock.json`
- UI source: `ui/src`, `ui/public`, `ui/index.html`, Vite config, and TypeScript config
- Show Runtime provider/ref

If fingerprints are unchanged, the runner skips unnecessary Python dependency
installation. Source syncs replace the source tree, so UI dependencies and UI
assets are rebuilt for each update to avoid serving stale or missing `ui/dist`
content.

## Secret Safety

- Never commit `.env.regression`.
- Never commit generated files under `.runtime/`.
- Runtime secrets are written into the Incus instance through stdin to
  `/etc/avibe-regression.env`; they should not appear in command-line logs.
- Share `.env.regression.example` if you only need to show the structure.
