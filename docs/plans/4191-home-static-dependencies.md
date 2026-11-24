# PR 4191 home static dependency experiment

## Contract

Base: `89bdf39caa9e6fa60038a8449183c65ec48f5c8e`.
This is a local isolated experiment for the PR owner, with no push or new PR.
Preserve all runtime behavior, offline first use, App lock delivery fences and
recording lifecycle. Keep the existing home 614400 B and chat 921600 B budgets,
production build configuration, measurement and dependency lockfile unchanged.

Candidate 1 separates the pure routeable catalog ID read from catalog editing.
The home model picker must keep its synchronous helper. Settings and onboarding
must retain the exact editing functions through their existing load boundaries.
No new dynamic import or prefetch requirement is permitted.

Validate with clean production builds and the unchanged budget checker, capture
actual output/module import ownership, run both existing consumer unit suites,
and run the hermetic production entry/offline retained-recording tests.

## Result: candidate 1 passes, no second candidate needed

The only executable change is moving `routeableCatalogModelIds` unchanged into
`backendCatalogIds.ts` and pointing its two consumers there. The production
entry previously imported the whole catalog-editing module:

`main -> App -> AppShell -> NewSessionSheet -> AgentRoutePicker -> backendModels -> backendCatalog`

The home composer is another consumer through `Workbench -> MemberHome ->
AgentRoutePicker`. Settings, onboarding and their existing dialogs consume the
catalog editing module. It has no side effects or runtime imports before this
change, so removing its eager evaluation removes no initialization behavior.

The production module graph reports exactly one rendered module removed from
the entry (`backendCatalog.ts`) and exactly one added (`backendCatalogIds.ts`).
The old module contributed 8,944 rendered bytes before minification. The entry
now contains only the 277-byte shared reader; the 8,722-byte editing module is
in the bundler-generated `backendCatalog-BDQN7hFl.js`, statically imported by the
existing AddModelsDialog, SettingsModelsPage, Wizard, dialogFields,
reconcileUnknownWrite and supply chunks. No source dynamic-import edge changed.
The helper body is byte-identical to the base. This is a real ownership change,
not a minifier or gzip setting experiment.

| Production gzip JS (unchanged level-6 gate) | Base | Candidate | Change | Budget | Headroom |
| --- | ---: | ---: | ---: | ---: | ---: |
| Home | 614,509 B | 613,105 B | -1,404 B | 614,400 B | 1,295 B |
| Chat | 915,200 B | 913,738 B | -1,462 B | 921,600 B | 7,862 B |

Home still loads four JS files. The vendor (234,546 B), runtime (627 B) and
largest locale (124,293 B) are byte-identical. The entry drops from 255,043 to
253,639 gzip bytes. Chat still loads 59 files. Thresholds, preload logic,
lockfile, build configuration and gzip measurement are unchanged.

## Verification

- Installed the exact lockfile with `npm ci`; built the baseline and candidate
  with `npm run build`, which clears Vite output normally. Ran the repository's
  unchanged `npm run check:first-load`: base fails home; candidate passes both.
- A read-only `generateBundle` observer captured real production module
  ownership and importers. Its baseline output filenames and per-file gzip
  bytes exactly match the ordinary build. The candidate's later independent
  E2E-triggered clean production build exactly reproduces its full measurement.
- Existing `backendModels`, `backendCatalog` and `AgentRoutePicker` suites:
  3 files, 95 tests passed, covering both readers and the editing consumers.
- Existing hermetic production `test:workbench-general-build`: typecheck and
  all 4 Chromium cases passed (three chat mention-picker entry cases and
  `first retained take stays usable offline without prewarming a card chunk`).
  The offline case disables idle prefetch, disconnects before the retained card
  first appears, verifies no script fetch, preserves the draft and take, then
  reconnects and verifies one transcription and one insertion.
- Oxlint on the three changed TypeScript files passed. `git diff --check` passed.
- No new asynchronous boundary: every editing consumer statically imports the
  extracted editing chunk as part of its already-deferred module. The synchronous
  picker helper stays in the entry, so its first offline use needs no new fetch.

Local runtime: macOS arm64, Node 24.8.0, npm 11.6.0. CI uses Linux and Node
26.13.1; this experiment uses its exact build and byte-measurement commands,
not its OS/runtime. Baseline numbers reproduce the owner's earlier measurement
exactly. Actual CI and exact-head review belong to the PR owner after adoption.
No full native App-lock acceptance or physical recording run was performed.

## Evidence and handoff

Raw build logs, byte inventories, full module graphs, read-only observer and
comparison script output are retained outside the checkout at
`/tmp/4191-static-evidence/`. `comparison.json` records identical dynamic-import
edges and helper source, and enumerates the sole entry module replacement.

The local task branch is `experiment/4191-home-static-sesbvz8`, based exactly on
`89bdf39caa`. Only these three source files and this report belong to the patch.
The owner can cherry-pick the local commit into #4191, then run exact-head review
and CI. No push, PR, merge, package install, live service operation or changes to
the existing App-lock worktree were made.
