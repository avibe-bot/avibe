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

## Running it

```bash
cd ui
npm install                      # once
npx playwright install chromium  # once

VIBE_E2E_BASE_URL=http://127.0.0.1:5199 \
VIBE_E2E_MOCK_UPSTREAM_URL=http://127.0.0.1:9931 \
npm run e2e
```

`VIBE_E2E_BASE_URL` is required — the suite refuses to start without it, because
it mutates the instance it points at (see below).

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
#     processes; the suite needs both.
AVIBE_HOME=$E2E_HOME VIBE_MODEL_HUB_ENABLED=1 python main.py &
AVIBE_HOME=$E2E_HOME VIBE_MODEL_HUB_ENABLED=1 python -c \
  "from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5199)" &
```

Then turn the gateway on once — from the UI at
`http://127.0.0.1:5199/settings/models`, or by letting the A-suite's own toggle
spec do it. Suite B, D, and G need the runtime **running**; they skip otherwise.

Set `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH` to a local manifest if the machine
should not fetch the engine archive from the network.

Tear it down by killing both processes and deleting `$E2E_HOME`. Nothing outside
it was written.

## Against a remote VM

Same suite, one variable:

```bash
VIBE_E2E_BASE_URL=http://<vm-host>:5123 npm run e2e
```

Two caveats. The browser must reach the instance **without a login**, which is
true for loopback and for a tunnelled instance you are already authenticated to,
and not true otherwise. And the mock upstream must be reachable *from the VM* as
well as from this machine, because the instance is what dials it — a
`127.0.0.1` mock URL will resolve to the VM's own loopback, not to yours.

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
restarting the engine, and stopping and starting the whole gateway do not.

B6 retries that sticky state by rebuilding its arrangement (delete the source,
recreate it, re-chain, re-settle) up to three times, and then **fails** with a
message naming the engine defect if all three attempts land in the same
cooldown. Per §5a this is a product-side failure once the mock has answered, so
it is the suite's finding rather than its exit — it has been reported to the
orchestrator either way. Any other verdict fails immediately, because a wrong
classification is exactly what the scenario exists to catch.

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
