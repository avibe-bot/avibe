# Model Hub end-to-end suite

Playwright specs that drive the Model Hub surface of a **running Avibe
instance** through a real browser. They are not wired into CI, and they do not
start anything for you: you point them at an instance, and they exercise it.

Scenario IDs in the spec titles (A1…E3) refer to
`docs/plans/model-hub-e2e-test-plan.md` §3 — the web-interaction plan this suite
implements. They are that plan's working labels, deliberately not the
`MH-*` ids of `tests/scenarios/model_hub/catalog.yaml`: the catalog's canonical
evidence gate binds each `MH-*` row to one pytest or vitest case it can verify
collects, and a Playwright spec drives a live instance rather than collecting
under either runner, so citing it there would make an unverifiable claim. The
cross-reference lives in the plan, which maps every §3 id to its `MH-*`
counterpart; if the pytest lane lands an orchestration that runs this suite and
records its result, that runner — not the spec files — is what belongs in the
catalog.

## What it talks to

| Variable | Default | What it is |
| --- | --- | --- |
| `VIBE_E2E_BASE_URL` | *(none — required)* | The Avibe instance under test. The run refuses to start without it. |
| `VIBE_E2E_DESTRUCTIVE_TARGET` | *(none — required)* | The same URL again, stating that instance is disposable. |
| `VIBE_E2E_MOCK_UPSTREAM_URL` | *(unset)* | A controllable model upstream. Specs that need one **skip** when it is absent. |

Nothing else is read, and the suite never imports the mock's code — it types the
mock's URL into the dialog's Base URL field the way a user would, and steers it
over its own HTTP control plane.

### The mock upstream

The driver the repo's own instructions reference (`tests/e2e/drivers/
mock_llm_upstream.py`) ships with the pytest lane and is not in this PR, so a
clean checkout has no bundled copy. Any server implementing the §5a control
plane will do: `POST /__control/config` with `auth`, `protocol`,
`models_endpoint`, `stream`, and `models`; `GET /__control/requests` returning
`{"requests": [...]}`; `DELETE /__control/requests` to reset; plus the
Anthropic/OpenAI protocol endpoints under `/v1/*`. Until the pytest lane lands
its driver, run the suite with a mock you provide on that contract.

## Preconditions, and why they skip instead of fail

A live instance can legitimately be missing the thing a spec needs: the Model Hub
capability can be off, the gateway runtime can be stopped, no agent backend CLI
can be installed. Those are facts about the instance, not defects, so each one
produces a **skip whose message says what to start**. A red test here always
means the product did something wrong.

The line between the two is deliberate (test plan §5a): only an environmental
fact may skip. Once the instance has answered — the capability read succeeded,
the mock's control plane replied — a later refusal (a source it will not create,
a mode switch that does not take, a forced route PUT it rejects) is a product
failure and fails the spec. `VIBE_E2E_BASE_URL` has no default for the same
reason: the suite mutates the instance it is pointed at, and refusing to start
on an unspecified target is safer than any warning.

### Admitting a target

Being named is not the same as being disposable, and the suite is destructive on
every axis the Model Hub has — it force-deletes sources, rewrites route chains,
flips agent modes, and stops the gateway. So a target is admitted only when:

- `VIBE_E2E_DESTRUCTIVE_TARGET` names the **same** URL as `VIBE_E2E_BASE_URL`
  (trailing slashes and case are normalized away, so only the target has to
  match, not the spelling); and
- that URL is not on port `5123`, which is where the packaged service listens
  unless its operator moved it. That port is refused outright, consent or not —
  the likeliest way to reach it is pasting the URL of the vibe UI you have open.

The consent variable names the target rather than being a flag on purpose: an
answer given for one instance then expires the moment `VIBE_E2E_BASE_URL` moves,
instead of sitting in a shell profile approving whatever comes next. Nothing a
running instance reports could replace it — a hermetic instance and the one you
work on serve the same API — so consent is the only honest form the check has.

## Running it

```bash
cd ui
npm install                      # once
npx playwright install chromium  # once

VIBE_E2E_BASE_URL=http://127.0.0.1:5199 \
VIBE_E2E_DESTRUCTIVE_TARGET=http://127.0.0.1:5199 \
VIBE_E2E_MOCK_UPSTREAM_URL=http://127.0.0.1:9931 \
npm run e2e
```

Both target variables are required and must agree — the suite refuses to start
otherwise, because it mutates the instance it points at (see below).

`npm run e2e:headed` runs the same thing in a visible browser. Artifacts (traces
for failures, screenshots, the HTML report) land in `ui/e2e/.artifacts/` and are
gitignored.

## Against a local hermetic instance

The suite mutates the instance it points at — it creates sources, edits routes,
and deletes things. **Do not point it at the Avibe you use.** Give it its own
`AVIBE_HOME` and its own port:

```bash
export E2E_HOME=/tmp/avibe-e2e-home
mkdir -p "$E2E_HOME"

# 1 · a config the instance will accept, with no IM platform enabled and the
#     Model Hub on. The UI port must not be 5123 if a real vibe is running there.
mkdir -p "$E2E_HOME/config"
python3 - <<'PY'
import json, os
home = os.environ['E2E_HOME']
json.dump({
    'version': 'v2',
    'platform': 'avibe',
    'platforms': {'enabled': [], 'primary': 'avibe'},
    'mode': 'self_host',
    'avibe': {'enabled': True, 'proxy_url': None},
    'runtime': {'default_cwd': f'{home}/work', 'log_level': 'INFO'},
    'agents': {'claude': {'enabled': True, 'cli_path': 'claude'}},
    'model_hub': {'enabled': True},
    'ui': {'setup_host': '127.0.0.1', 'setup_port': 5199, 'open_browser': False},
    'language': 'en',
    # Without this every browser navigation is redirected to the setup wizard,
    # and the suite fails on "the Model Hub shell is not there" for every spec.
    'setup_completed': True,
}, open(f'{home}/config/config.json', 'w'), indent=2)
PY

# 2 · the controller (owns Model Hub state) and the UI server are separate
#     processes; the suite needs both. Both run from the REPOSITORY ROOT — the
#     `cd ui` above is for the browser side only, so return before starting them
#     (`python main.py` finds no `ui/main.py`, and `vibe` is imported from the
#     root, not from `ui/`).
#
#     And `AVIBE_HOME` alone does NOT make the run hermetic: switching a
#     backend to Gateway mode scans the CLI homes the controller process
#     sees — `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, the XDG dirs, `~/.claude`,
#     `~/.codex` — and on a machine with real CLI credentials that scan can
#     read production tokens and import a real native account into the test
#     instance's state. The processes get test-owned copies of all of them.
#
#     On macOS there is one more channel the env cannot redirect: a Claude CLI
#     that stores OAuth in the SYSTEM KEYCHAIN still sees your real login even
#     under a redirected HOME, because `claude auth status` (which the mode
#     switch's presence probe executes) reads the keychain, not the filesystem.
#     If your install is keychain-backed, point the suite at a shim instead of
#     the real binary — put a directory FIRST on PATH carrying an executable
#     `claude` that answers `auth status --json` from test-owned state (e.g.
#     `{"oauth_account":null}` printed and exit 0) and execs nothing else. A
#     container or separate OS user is the stronger form of the same boundary.
cd "$(git rev-parse --show-toplevel)"
export E2E_ISOLATED_HOME="$E2E_HOME/home"
mkdir -p "$E2E_ISOLATED_HOME/.claude" "$E2E_ISOLATED_HOME/.codex" \
  "$E2E_ISOLATED_HOME/.config" "$E2E_ISOLATED_HOME/.cache" \
  "$E2E_ISOLATED_HOME/.local/share"
export HOME="$E2E_ISOLATED_HOME"
export XDG_CONFIG_HOME="$E2E_ISOLATED_HOME/.config"
export XDG_CACHE_HOME="$E2E_ISOLATED_HOME/.cache"
export XDG_DATA_HOME="$E2E_ISOLATED_HOME/.local/share"
export CLAUDE_CONFIG_DIR="$E2E_ISOLATED_HOME/.claude"
export CODEX_HOME="$E2E_ISOLATED_HOME/.codex"
AVIBE_HOME=$E2E_HOME VIBE_MODEL_HUB_ENABLED=1 python main.py &
AVIBE_HOME=$E2E_HOME VIBE_MODEL_HUB_ENABLED=1 python -c \
  "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5199)" &
```

Then turn the gateway on once, from the UI at
`http://127.0.0.1:5199/settings/models`. Suite B, D, and G need the runtime
**running**; they skip otherwise. No spec in the suite starts a stopped gateway
for you: A2's round-trip spec skips unless the runtime is already running, and
the install-entry spec opens and cancels the install dialog without ever
completing an install.

Finally, return to `ui/` before running the suite — step 2's `cd` to the
repository root left the shell there, and `npm run e2e` needs `ui/package.json`:

```bash
cd "$(git rev-parse --show-toplevel)/ui"
# Linux: Chromium must be (re)installed HERE, after the isolated cache exists —
# `npx playwright install chromium` run before step 2 exported XDG_CACHE_HOME
# put it in the original cache, which the isolated run no longer reads. Setting
# PLAYWRIGHT_BROWSERS_PATH to a stable path instead works equally well.
npx playwright install chromium
VIBE_E2E_BASE_URL=http://127.0.0.1:5199 \
VIBE_E2E_DESTRUCTIVE_TARGET=http://127.0.0.1:5199 \
VIBE_E2E_MOCK_UPSTREAM_URL=http://127.0.0.1:9931 \
npm run e2e
```

Set `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH` to a local manifest if the machine
should not fetch the engine archive from the network.

Tear it down by killing both processes and deleting `$E2E_HOME`. Nothing outside
it was written — the CLI config stores were redirected into it at step 2 before
either process read anything.

## Against a remote VM

Same suite, same two variables:

```bash
VIBE_E2E_BASE_URL=http://<vm-host>:5123 \
VIBE_E2E_DESTRUCTIVE_TARGET=http://<vm-host>:5123 \
npm run e2e
```

Port `5123` is refused only on loopback, where it is recognizably the local
service; on another host it is admitted with consent, because nothing here can
tell whose machine that is. Which makes the consent yours to mean: a shared
regression VM accumulates product state on purpose, and this suite deletes
sources and rewrites routes on whatever it is pointed at.

Two caveats. The suite has **no login step**, so the endpoint must be one that
serves without one — a VM's own `host:port` on a network you trust, not a
tunnel fronted by a remote-access login. Being signed in to that instance in
your own browser does not carry: Playwright opens a fresh browser context and
its API context keeps a separate cookie jar. Pointed at a tunnel that wants a
login, the run fails on a precondition read and says exactly that; supporting
one would mean a bootstrap that authenticates the page and the API context
alike, and this suite does not have it. And the mock upstream must be reachable
*from the VM* as well as from this machine, because the instance is what dials
it — a `127.0.0.1` mock URL will resolve to the VM's own loopback, not to
yours.

## What it leaves behind

Every source the suite creates is named `e2e-playwright-*`, and each suite
removes its own in teardown. If a run is killed mid-way, the next run's teardown
sweeps whatever is left by that prefix.

The route-chain and priority-order specs **cancel** rather than save: they test
the editor, and the instance's real routing is not theirs to change. The one
spec that commits is B7, and it commits to a source the suite created.

## Known flakes

**B6 · the replace-key precondition.** A source only offers to have its key
replaced once the key has actually been rejected, and the only thing that can
reject it is a real request down the route chain — a refetch reads the model
list, which an upstream that refuses completions still serves. So B6 arranges a
chain onto its own source, sets the mock to answer `401`, and drives
`POST /api/models/agents/{backend}/probe` until the source settles.

Roughly one run in three, the gateway engine answers that dry run with a 5xx of
its own and the mock upstream never receives anything. Avibe classifies it as
`models.source.cooldown.server_error`, which blocks the chain for thirty seconds;
the retry after that window fails the same way and re-arms it, so the source
never reaches a verdict about its key. Recreating the source clears it; waiting,
restarting the engine, and stopping and starting the whole gateway do not. It is
filed as [#1818](https://github.com/avibe-bot/avibe/issues/1818).

B6 retries that sticky state by rebuilding its arrangement (delete the source,
recreate it, re-chain, re-settle) up to three times. If all three attempts land
in the same cooldown, that is #1818's exact signature, and the spec retires
itself via a runtime `test.fixme` naming #1818 — the run reports one skipped
spec with the issue in its reason, because an intermittent engine defect does
not get to burn the suite red on its own schedule. Any other verdict fails
immediately, because a wrong classification is exactly what the scenario exists
to catch.

`test.fixme` rather than `test.fail`, for two reasons. `test.fail` sets the
expected status of the whole test — its own `finally`, the suite's `afterEach`,
and the gateway fixture's teardown included — so a failed restoration would
have satisfied "expected to fail" and reported green over a displaced route
chain. And a skip states the outcome: the scenario was not reached, which is
what happened, where a green tick says it passed.

Neither marker detects its own obsolescence, and an earlier version of this
note claimed otherwise. A `test.fail(cond)` whose `cond` and whose following
`expect` read the same verdict can never produce *"Expected to fail, but
passed"*: when `cond` holds the assertion necessarily fails, and when it does
not the test is not marked. What signals that #1818 is fixed is the report
itself — the skip and its reason stop appearing, and the spec runs to its
assertions.

## What this suite cannot reach

Recorded here so the gaps are visible rather than merely absent:

- **Metered usage figures.** They come from turns the gateway actually proxied.
  The E specs assert that each tab reaches a *stated* state; asserting numbers
  needs seeded gateway traffic and belongs to the pytest layer.
- **`discovered_at` preservation across a refetch** (B9). Not rendered anywhere
  a browser can read it.
- **Member-role dead-ends** (D13). Reaching them needs a second, non-operator
  auth context; every request from this browser to loopback is trusted by
  construction, which is exactly why the suite needs no login.
- **Subscription sources** (D1). No browser-reachable way to create one against
  a mock without a real OAuth provider.
- **Malformed guard echoes** (G1). The echo is built by the product; a browser
  cannot corrupt it without a product-side hook.
- **Locked-tier provenance on a server that predates the field** (B10). The
  catalog and upstream lock specs `test.fixme` unless the live instance stamps
  `reasoning_efforts_source` as `catalog` / `upstream` on the seeded model.
  Ghost suggestions and the `source_model_tiers_managed` refusal copy still
  run: the copy is reached by intercepting that one PATCH, and Chinese is a
  config-GET rewrite for that page only — the instance's language is never
  saved.
