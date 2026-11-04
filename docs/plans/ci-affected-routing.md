# Conservative affected routing for `lint`

Owner request (2026-11-04): skip pull-request CI work that a change provably
cannot reach, without weakening any gate. Base: master `9ea2dd696` (#3239).
No branch-protection change, runner purchase, lane-count change, release
workflow edit, retry, or timeout relaxation.

## Contract

`changes` (`scripts/ci_docs_only.py`) classifies the complete Git-tree diff
exactly as the documentation fast path does (`ci-docs-fast-path.md`). For a
pull request it compares the event base with the checked-out synthetic merge,
and verifies both merge parents. It writes four literal outputs:
`docs_only=true|false`, `windows_control_ipc=true|false`,
`browser=run|skip` and `platform=run|skip`.

A route skips only on a `pull_request` into the repository's default branch,
and only when every changed path is narrative or in the route's allowlist. Each
of the following runs everything:

- a push (master validates the whole tree, since latest-wins cancellation can
  drop an earlier run);
- an unsupported event, or a missing or malformed payload;
- a merge-parent or checkout mismatch, or an empty or incomplete diff;
- a status other than A/M/D, or any mode other than a regular file, such as
  an executable bit, a symlink, a submodule or a type change;
- a path outside every allowlist.

Rename detection is off, so a move counts both its deleted and its added path.

| Route | Jobs that may skip | Changed paths that allow it |
| --- | --- | --- |
| `browser` | `ui-browser-checks` (4 lanes, 14 commands) | narrative docs, or a path under `core/`, `modules/`, `vibe/`, `storage/`, `config/`, `tests/`, `skills/`, `desktop/`, `docs/`, `assets/`, `standards/`, `examples/`, `.agents/`, or `main.py`, and none inside the browser checkout |
| `platform` | `web-fetch-runtime` (5 legs) and `windows-install-smoke-runners` | narrative docs; `docs/**`; `tests/scenarios/<area>/catalog.yaml`; `ui/src/**`, `ui/e2e/**`, `ui/design/**`; `ui/playwright*.config.ts`, `ui/tsconfig.tests.json`, `ui/eslint-baseline.json`, `ui/.oxlintrc.json`, `ui/README.md`, `ui/agentation.d.ts`, `ui/design.html`; and every added path must be a portable name (`[A-Za-z0-9._@+-]{1,64}` per segment, no trailing dot, no Windows device name) |

Nothing else is routed. The Python unit shards, Vitest and static UI checks,
`build-linux-artifacts`, the install/upgrade regressions and distribution
contracts, `scenario-evidence`, and the six required leaf jobs (`ruff`,
`show-router-integration`, `memory-insight-contract`,
`migration-release-guard`, `npm-wrapper`, `build-linux-artifacts`) keep their
existing conditions. So do the Windows control IPC selection and every
push-only Windows job. A skipped required leaf passes branch protection with no
gate looking at it, which is why none of them is routed.

## Why each family is independent of its allowlist

**Browser lanes.**

- Each lane runs Vite dev or build+preview and Playwright.
- `ui/scripts/e2e-suite.mjs` spawns only node (tsc and Playwright), and no lane
  starts Python.
- Outside `ui/`, the lanes read only these files:
  - `ui/vite.config.ts` allows Vite to serve `vibe/message_types.json` and
    three `vibe/data/*.json` catalogs;
  - `e2e/support/vendors.ts` and `e2e/vault-integrity/integrity.spec.ts` read
    the same JSON;
  - the build lane's out-of-tree import check reads `Dockerfile`.

This is enforced rather than inferred. The browser job's checkout is a non-cone
sparse checkout of exactly `/ui/`, `/vibe/message_types.json`, `/vibe/data/`
and `/Dockerfile`, on every run including full ones. Any suite that reads
anything else fails rather than silently depending on it.
`tests/test_ci_pipeline.py` pins that list equal to `BROWSER_CHECKOUT` in the
classifier. It also requires every file the Dockerfile ui-builder stage copies,
which is what the import check validates against, to lie inside it. A local
run of all 14 lane commands against such a sparse checkout of `9ea2dd696`
passed.

**Installed-package platform checks.**

- `web-fetch-runtime` installs the wheel and the frozen requirements from
  `uv.lock`. It then runs five Python test files with `tests/conftest.py`.
  Those files import `core.web_fetch_worker`,
  `core.handlers.model_hub.local_fetch` and
  `desktop/scripts/build-runtime-bundle.py` (with a placeholder `ui/dist`).
- The Windows smoke runs `install.ps1`, `vibe version`, the engine-platform
  probe and the uninstall paths.

Neither reads Web UI content. The allowlisted paths reach them only as the
bytes of `vibe/ui/dist` inside the wheel. That output is still built by
`build-linux-artifacts`, and its member names are still checked by the
distribution contracts in `install-upgrade-shards`. What remains is
filesystem portability on Windows and macOS: whether the installed file
names can exist on those filesystems.

- **Hashed output names.** The production build names everything it takes from
  the allowlisted sources `assets/<source name>-<content hash>.<ext>`, in one
  flat directory. The other outputs are `index.html` and verbatim copies of
  `public/`. A build of `9ea2dd696` confirms this: 266 hashed assets plus
  `index.html` and the `public/` files.
- **Build code is excluded.** A Vite plugin can emit files under any name and
  path. For example, a `generateBundle` emit of both `assets/emoji` and
  `assets/EMOJI/en.json` builds and passes the Linux distribution contracts,
  but collides as a file and a directory on a case-insensitive filesystem. So
  `ui/scripts/**`, which holds the build's plugins and scripts, is not UI
  source. Neither are `ui/public/**`, the package manifests, `vite.config.ts`,
  `index.html` and the build `tsconfig` files. A test follows every local
  module `vite.config.ts` loads, transitively, and requires each to be outside
  the allowlist.
- **Added names must be portable.** Every added path in a routed change must
  be a portable name.

**Why the larger candidates stay unrouted.**

Python tests read `ui/` broadly. Examples:

- whole-tree scans of `ui/src`, in `test_show_page_legacy_ui_removed.py` and
  `test_api_save_config_merge.py`;
- negative existence checks, in `test_permissions_scenarios.py`;
- catalog citations of UI tests and `ui/e2e` specs, in
  `tests/scenarios/test_catalog_citations.py`;
- the i18n catalogs, `ui/public/push-sw.js`, and the Model Hub authority globs.

Neither a sparse checkout nor Python audit hooks can prove such a list
complete, because negative scans pass vacuously when the tree is absent. Vitest
reads four `.py` files: `vibe/remote_access.py`, `vibe/model_hub_client.py`,
`vibe/backend_model_catalog.py` and `core/bots.py`. It also reads Model Hub
contracts, `tests/fixtures` and the scenario catalogs.

## Gates

No route exists without a required gate that checks it. Both routed families
report through an existing required context; no context was added or renamed.

- `unit-tests` reads `BROWSER_ROUTE`. The browser lanes must report `skipped`
  when the documentation fast path or the literal `skip` route says so, and
  `success` otherwise.
- `windows-install-smoke` now also needs `web-fetch-runtime` and reads
  `PLATFORM_ROUTE`. Both platform producers must report `skipped` when skipped
  by docs or route, and `success` otherwise. This is a deliberate
  strengthening: `web-fetch-runtime` was previously in no required context.
  Any of its five legs that fails, is cancelled, times out, or never gets a
  hosted runner now fails the required `windows-install-smoke` context, which
  keeps its historical name because branch protection requires it. The job
  graph has no cycle: `web-fetch-runtime` needs only `changes` and
  `build-linux-artifacts`.
- Every gate fails on any of the following:
  - a classification that did not succeed;
  - a `docs_only` or route value other than the literals;
  - a producer that failed, was cancelled or is missing;
  - a producer that skipped when it should have run;
  - a producer that ran when its route said skip.
  `tests/test_ci_pipeline.py` executes each gate's shell from `lint.yml` over
  every valid scope and route combination, every producer outcome, every scope
  result, and the invalid literals.

## Master and releases

Master pushes always run every job. A release source is a master commit whose
complete push run is green, so routing affects only pull-request feedback, and
the master run is the backstop after merge. Release workflows (`publish.yml`,
`release_ai.yml`, the dispatched `model-hub-e2e.yml`) are unchanged.

## Historical replay

Scratch replay with this PR's `scope_of` on blobless Git objects. Squash-merge
diffs stand in for the PR synthetic merges, and failed runs use
merge-base..head.

| Sample | browser skip | platform skip | both | full | docs |
| --- | ---: | ---: | ---: | ---: | ---: |
| Last 400 squash merges | 128 | 91 | 10 | 170 | 1 |
| 300 merged PRs (Oct 19–Nov 4), weighted by their 755 PR runs | 176 | 139 | 25 | 414 | 1 |

On the PR runs, the browser route would have skipped 4 lane jobs on about 27%
of them, and the platform route 6 jobs (two macOS, one Windows web-fetch leg,
one ARM, one ubuntu, plus the Windows smoke) on about 22%.

**300 failed PR runs (Sep 27–Nov 4).** In 6 of them, a job that the route would
have skipped failed or was cancelled; in 5 of those 6, nothing else failed.
Each was read from its job log or annotations:

| Run | Change | Would-skip failure | Cause |
| --- | --- | --- | --- |
| 39386231456 | Python, plans and catalog | browser: `project-order/order.spec.ts` keyboard test | The project-order readiness flake later fixed by #3238 |
| 41362459680 | desktop Cargo bump | browser lane 2: `bots.spec.ts` muting case | Flake; the next head passed |
| 41392997060 | UI design | macOS-intel web-fetch: `test_model_hub_local_fetch.py` attempt-expiry timing (`service_busy` vs `fetch_timeout`) | Python test timing; the PR also failed a browser lane, which still runs |
| 41495159770, 41853843794 | UI | macOS web-fetch cancelled | "The job was not acquired by Runner of type hosted" |
| 41861194599 | `ui/e2e` only | Windows web-fetch float-boundary assertion and two unacquired macOS legs | The transport-test clock fixed in #3238 |

None of these failures came from a file the routed job reads. Separately, 11
failed runs failed only in web-fetch-runtime, 8 of them on routes where it still
runs: under the new gate, those are required-context failures.

**Limits.** The replay approximates synthetic merges with squash or merge-base
diffs, and it only exposes couplings that produced a failure. It is evidence for
the boundaries above, not proof of zero lost coverage. The proof rests on the
input analysis and on the enforced browser checkout. No time saving is claimed
until routed runs exist: wall time is still bounded by Vitest and the unit
shards, and the expected gains are runner slots and the macOS/Windows queue
tail for UI pull requests. Measure actual routed PR runs after merge before
quoting a figure.
