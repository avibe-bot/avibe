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
python3 scripts/incus_regression.py cleanup-stale --yes
```

Delete worktree environments promptly after the worktree is merged, abandoned,
or removed. The persistent `master` environment should stay running and preserve
its product state across normal source updates.

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
  Show Runtime source: github-source
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
