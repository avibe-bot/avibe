# Model Hub completeness audit

Status: **master static audit complete**  
Baseline: `cf1b4314d04e871d5072ddeed9c74a402983efb4` (`origin/master`, 2026-07-25)  
Scope: signed behavior spec `model-hub.md` sections 4-5 and frozen contracts under
`model-hub-contracts/`. F1/F2 worktrees were not inspected. No runtime, VM,
container, network, or production-state probe was performed.

## Verdict

Model Hub is **not feature-complete on master**. The public UI is enabled and
uses the live API client, but four independent P0 seams leave core promises dead:

1. the UI server resolves the engine to a fail-closed placeholder;
2. native subscription OAuth has no implementation;
3. a successful OAuth flow is never finalized into a persisted Source by the
   live UI; and
4. the complete resolution loop (same-turn retry/fallback, one refresh on 401,
   and stream retry guard) is implemented in `ModelHubService.resolve()` but no
   production turn path calls it.

The primary matrix has **47 rows**:

| Verdict | Count |
| --- | ---: |
| `OK-wired` | 24 |
| `implemented-but-UNWIRED` | 9 |
| `MISSING` | 10 |
| `mock-only` | 1 |
| `flag-off` | 3 |

Unique defect count: **4 P0, 8 P1, 2 P2**. Rows can share one root defect, so
severity totals intentionally do not equal matrix totals.

## Legend

- `OK-wired`: the production path reaches the implementation and persistence
  required by the contract.
- `implemented-but-UNWIRED`: implementation exists, but the live path bypasses
  it or resolves a placeholder.
- `MISSING`: a required behavior or layer does not exist.
- `mock-only`: the mock client supplies behavior absent from the live client.
- `flag-off`: deliberately unavailable behind a constant/default-off gate. A
  broken path behind that gate is still called out as a defect.

## Feature completeness matrix

Each row traces UI/action -> API -> service -> adapter/runtime -> persistence.
`n/a` means the rule is internal and does not require that layer.

| ID | Spec behavior | UI/action | API | Service | Adapter/runtime | Persistence | Verdict | Sev |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| R01 | Source aggregate and ordered global list (§4.1) | `SourcesCard` renders/reorders (`SettingsModelsPage.tsx:71-125`) | GET sources + PUT priority (`ui_server.py:3059-3061`, `3115-3125`) | `list_sources` / `set_priority` | n/a | `V2ModelHubConfigStore` | `OK-wired` | - |
| R02 | `supply_channel`, defaults, consent fields (§4.1) | Native is default; experimental Hub option gated | Source schema/client types | `create_source` validates channel/consent (`service.py:688-699`) | allowed-origin projection for Hub sources | V2 config round-trip | `OK-wired` | - |
| R03 | API-key credential provision, discovery, source creation (§4.1/§5-06r) | API-key dialog calls live POST | POST sources (`ui_server.py:3064-3072`) | `create_source` provisions/discovers/commits (`service.py:736-779`) | UI default is `UnavailableEngineAdapter` | rollback-aware config + engine state exists but is unreachable | `implemented-but-UNWIRED` | P0 |
| R04 | Native subscription OAuth source (§4.1/§5-09) | OAuth dialog calls live OAuth API | start/status/submit/cancel routes exist | dispatches to `native_oauth_adapter` | only `UnavailableNativeOAuthAdapter` exists (`oauth.py:39-52`) | flow registry exists; no native adapter output | `MISSING` | P0 |
| R05 | Consent-gated Hub-held subscription (§4.1) | option constant false; when true it changes only `channel` (`OAuthConnectDialog.tsx:278-333`) | client omits `experimental_consent` (`modelsApi.ts:109`) | start rejects Hub without consent (`service.py:1092-1098`) | engine OAuth exists but UI cannot enter it | consent cannot be recorded from live UI | `flag-off` | P1 |
| R06 | Auto-discovered + manual model supply lists (§4.1) | tooltip and custom form consume model list | create/test/custom-model routes | discovery merge and manual provenance implemented | real adapter implements discovery | source model list persists | `OK-wired` | - |
| R07 | Per-turn dispatch before launch (§4.2 step 0) | n/a | n/a | controller creates one runtime router (`controller.py:228-232`) | router resolves on every Claude/Codex/OpenCode turn | reads current V2 config each resolution | `OK-wired` | - |
| R08 | Per-agent mapping (§4.2 step 1) | fixed-menu drawer calls PUT mappings | PUT mappings (`ui_server.py:3147-3158`) | validates target + persists | router applies mapping (`model_hub.py:443-452`) | V2 config + mapping event | `OK-wired` | - |
| R09 | Eligible candidates, priority, origin binding (§4.2 step 2) | priority drag + mappings/menu | priority/mapping/menu routes | `_resolution_candidates` filters and orders | runtime router enforces sanctioned native backend; engine bindings carry origins | source state/priority persist | `OK-wired` | - |
| R10 | 429/quota/5xx/network -> next candidate in current Hub request (§4.2 step 3) | n/a | n/a | complete loop exists only in `ModelHubService.resolve` (`service.py:1291-1409`) | production router chooses exactly one source (`model_hub.py:461-504`); backend failure is recorded for the **next** turn (`model_hub.py:524-529`) | cooldown persists, failed user turn does not retry | `implemented-but-UNWIRED` | P0 |
| R11 | 401 refresh once, then retry (§4.2 taxonomy) | n/a | n/a | classification/refresh loop is covered only through `service.resolve` | no production caller of `adapter.invoke`; backends receive an injected gateway and own the turn | no production resolution result | `implemented-but-UNWIRED` | P0 |
| R12 | Never retry after streaming starts (§4.2 taxonomy) | n/a | n/a | implemented in dead `service.resolve` path | live path performs no Model Hub retry at all | n/a | `implemented-but-UNWIRED` | P0 |
| R13 | Native/Hub cooldown and recovery on subsequent turns (§4.2 step 0) | reflected in source chip/current supply | GET sources/agents/events | cooldown/recovery state machine | router records terminal diagnostics and re-resolves next turn | source state + event log persist | `OK-wired` | - |
| R14 | Live same-process switch/cooldown/recovery event emission (§4.2) | recent-switch card consumes GET events | GET events (`ui_server.py:3199-3206`) | `_record_event`, `_emit_switch`, cooldown/recover emitters | router calls emitters (`model_hub.py:383-425`, `502-503`) | bounded event file | `OK-wired` | - |
| R15 | **Every** switch survives process restart (§4.2) | n/a | GET events can only show what was written | failure reason/previous launch are not durable | `_last_launch`, `_pending_switch_reason`, `_pending_source_failure` are memory-only (`model_hub.py:266-268`) | cooldown survives; corresponding switch can be lost after restart | `MISSING` | P1 |
| R16 | Cross-vendor automatic substitution (§4.2) | advanced row is a coming-soon toast | none | none by design | none | none | `flag-off` | - |
| R17 | Claude runtime-only injection (§4.3/§6) | n/a | n/a | session handler resolves before client creation (`session_handler.py:724-732`) | env/settings sources + client fingerprint are applied (`session_handler.py:890-1013`) | no native config write | `OK-wired` | - |
| R18 | Codex runtime-only injection (§4.3/§6) | n/a | n/a | turn resolves inside session lock (`codex/agent.py:175-189`) | transport args/env and fingerprint replacement are applied | no native config write | `OK-wired` | - |
| R19 | OpenCode overlay + drain/restart (§4.3/§6) | n/a | n/a | overlay prepared before server/turn | `OPENCODE_CONFIG` overlay and source snapshot launch are applied | overlay/hash runtime metadata | `OK-wired` | - |
| R20 | Stable `provider/model-id` (§4.4) | grouped full identifiers + generated preview | menu/custom-model APIs | shared identifier helpers | overlay provider IDs never encode source IDs | checked menu persists | `OK-wired` | - |
| R21 | Engine installs/starts on first Hub need | runtime pill is observational | n/a | `_gateway_credentials` calls adapter start (`model_hub.py:375-381`) | supervisor `ensure_running` calls installer `ensure` (`supervisor.py:58-64`, `124-129`); engine OAuth uses `supervisor.client` | managed runtime install metadata | `OK-wired` | - |
| R22 | `/api/models/runtime/status` reports the real runtime | main page calls it | GET runtime/status (`ui_server.py:3276-3283`) | delegates to adapter status | UI default adapter reports `not_installed`; never assigned real adapter | hard-coded manifest + placeholder status returned | `implemented-but-UNWIRED` | P0 |
| R23 | One engine lifecycle owner across live processes | n/a | UI process owns API calls | controller and UI each construct local services | UI is a separate subprocess (`runtime.py:1670-1700`); adapter/supervisor singletons are process-local (`adapter.py:571-578`, `supervisor.py:218-239`) | state/config locks are thread-only | `MISSING` | P1 |
| R24 | GA platform set + Avibe-owned asset availability | n/a | runtime status exposes manifest | managed installer is implemented | only darwin-arm64/linux-amd64, upstream GitHub URLs (`cliproxyapi_manifest.json:10-26`) | verified cache exists | `MISSING` | P1 |
| R25 | Runtime status reflects active/overridden manifest | main page displays health | GET runtime/status | `_runtime_payload` uses module constant (`service.py:346-359`) | adapter discards supervisor's actual manifest while manifest path/URL are overridable | response can drift from installer truth | `MISSING` | P2 |
| R26 | Migration scan is read-only (§5-03) | backend card/dialog call scan | POST migration/scan | parses Claude/Codex/OpenCode stores (`migration.py:530-572`) | no engine required | no source/native write | `OK-wired` | - |
| R27 | Migration apply: `keep_native` (§5-03) | dialog posts selected IDs | POST migration/apply | creates native source and skips empty engine sync | native credential remains in sanctioned store | V2 source persists; originals untouched | `OK-wired` | - |
| R28 | Migration apply: API key import (§5-03) | same dialog | same route | provisions + discovers before commit (`migration.py:617-723`) | UI default engine placeholder fails | rollback exists but import cannot succeed | `implemented-but-UNWIRED` | P0 |
| R29 | Backend-page migration banner trigger (§5-03) | scans on backend-card mount and opens dialog (`BackendSupplyModeCard.tsx:92-113`, `186-218`) | scan/apply | real migration service | mixed as R27/R28 | mixed as R27/R28 | `OK-wired` | - |
| R30 | First open after upgrade + setup wizard migration triggers (§5-03) | no mount/call outside backend supply card | none | scan exists but is never invoked there | n/a | n/a | `MISSING` | P1 |
| R31 | Main Sources band (§5-01r) | list, priority drag, chips, supply tooltip, add menu | list/priority/create | live service | mixed create as R03 | V2 config | `OK-wired` | - |
| R32 | Source edit/delete/re-test actions for contracted endpoints | source rows expose only drag; no lifecycle actions (`SourceRow.tsx:64-108`) | PATCH and test have no client method; DELETE client is never invoked | methods exist | Hub operations need engine | persistence code exists | `MISSING` | P1 |
| R33 | Main Agent band + menus/current/mode (§5-01r) | all three agents, composite supply, mode/action | agents/mode/mapping/menu | current projection + mutations | runtime consumes config each turn | V2 config | `OK-wired` | - |
| R34 | Recent switches “view all” across bounded feed (§5-01r) | expands only initially fetched 20 (`SettingsModelsPage.tsx:74`, `RecentSwitchesCard.tsx:44-58`) | API supports `before` | service supports pagination | n/a | up to 500 events exist | `MISSING` | P2 |
| R35 | Advanced row (§5-01r) | coming-soon toast (`AdvancedRow.tsx:14-26`) | none | intentionally deferred by implementation plan | none | none | `flag-off` | - |
| R36 | Backend Hub/Direct supply card (§5-02) | all three provider pages mount the shared card | agents/mode + migration scan | real service | runtime consumes selected mode | V2 config | `OK-wired` | - |
| R37 | API-key test-and-add dialog (§5-06r/07) | calls create and reports discovered count | POST sources | real provision/discovery workflow | default placeholder blocks it | no source persisted | `implemented-but-UNWIRED` | P0 |
| R38 | OAuth shell completes a subscription Source (§5-09) | live success only toasts/refetches (`OAuthConnectDialog.tsx:105-112`); mock pushes a Source (`modelsApi.ts:355-383`) | source finalization requires a second POST that UI never sends | `_create_oauth_source` exists (`service.py:586-650`) | native missing; Hub blocked by F1 | completed live flow never becomes a Source | `mock-only` | P0 |
| R39 | Fixed-menu mapping drawer (§5-04) | Claude/Codex drawer calls PUT mappings | route exists | validates backend scope and target | router applies mapping | V2 config | `OK-wired` | - |
| R40 | OpenCode model menu (§5-05r) | provider grouping, featured/full, checkbox, edit | PUT menu | validates stable identifiers | overlay consumes menu | V2 config | `OK-wired` | - |
| R41 | Custom model add/edit + identifier preview (§5-08) | dialog calls POST for create and metadata edit | POST custom-models | appends or updates manual entry (`service.py:1029-1061`) | default engine placeholder blocks required source sync | update is rolled back | `implemented-but-UNWIRED` | P0 |
| R42 | Custom model delete contract | client method exists but no component invokes it | DELETE custom-models | guarded delete exists | engine sync | V2 source | `implemented-but-UNWIRED` | P1 |
| R43 | Model Hub i18n parity | 149 static references checked | n/a | n/a | n/a | en/zh have identical 2,613-leaf key sets; dynamic model keys accounted for | `OK-wired` | - |
| R44 | Public/live feature activation | nav/menu true; API mode live (`featureFlags.ts:18-32`) | live `/api/models/*` | live service factory | affected by R03/R04 | live files | `OK-wired` | - |
| R45 | UI calls to nonexistent endpoints | 16 contracted endpoints are invoked | all invoked paths have registered routes | handlers exist | mixed by row | mixed by row | `OK-wired` | - |
| R46 | Hub/Direct switch is truthful and “never silent” | Connect Hub reports success (`SettingsModelsPage.tsx:135-145`) | PATCH mode accepts Hub without a usable route | `set_agent_mode` only changes the bit (`service.py:977-985`) | any unconfigured Hub route silently returns Direct (`model_hub.py:435-439`) | persisted mode can disagree with actual launch | `MISSING` | P1 |
| R47 | OAuth flow survives/reconciles UI-process restart | poll assumes live flow | persisted registry can recover channel binding | registry persists binding only | engine OAuth state is process-memory (`adapter.py:99-101`); restart loses it | binding survives without resumable adapter flow | `MISSING` | P1 |

## REST endpoint audit

All **20/20** endpoints in `api.md` have registered `vibe/ui_server.py` routes.
The live UI invokes **16/20**. No UI call targets a nonexistent route. “Real
handler” below means the route reaches the intended service method; it does not
override the default adapter defects already called out.

| Contract endpoint | Route | Real handler | Live UI invocation | Result |
| --- | --- | --- | --- | --- |
| GET `/api/models/sources` | yes | yes | yes | OK |
| POST `/api/models/sources` | yes | yes, default engine placeholder | yes, API-key only; no OAuth finalization | P0 |
| PATCH `/api/models/sources/<id>` | yes | yes | **none** | P1 |
| DELETE `/api/models/sources/<id>` | yes | yes, Hub delete needs engine | client exists, no component call | P1 |
| POST `/api/models/sources/<id>/test` | yes | yes, needs engine | **none** | P1 |
| PUT `/api/models/priority` | yes | yes | yes | OK |
| GET `/api/models/agents` | yes | yes | yes | OK |
| PATCH `/api/models/agents/<backend>/mode` | yes | yes | yes | P1 truthfulness gap R46 |
| PUT `/api/models/agents/<backend>/mappings` | yes | yes | yes | OK |
| PUT `/api/models/agents/opencode/menu` | yes | yes | yes | OK |
| POST `/api/models/custom-models` | yes | yes, Hub source sync needs engine | yes | P0 on master |
| DELETE `/api/models/custom-models` | yes | yes | client exists, no component call | P1 |
| GET `/api/models/events` | yes | yes | yes, no `before` pagination | P2 |
| POST `/api/models/oauth/start` | yes | dispatches to placeholder(s) | yes | P0 |
| GET `/api/models/oauth/status/<flow_id>` | yes | dispatches to placeholder(s) | yes | P0 |
| POST `/api/models/oauth/submit` | yes | dispatches to placeholder(s) | yes | P0 |
| POST `/api/models/oauth/cancel` | yes | dispatches to placeholder(s) | yes | P0 |
| POST `/api/models/migration/scan` | yes | yes | yes | OK |
| POST `/api/models/migration/apply` | yes | yes; API-key branch needs engine | yes | mixed: native OK, API-key P0 |
| GET `/api/models/runtime/status` | yes | yes, default placeholder | yes | P0 |

There is one extra non-contract route, GET `/api/models/priority`
(`ui_server.py:3109-3112`); the UI does not use it. It is not harmful, but it is
outside the frozen endpoint list.

## Gap dossiers

### P0-01 — UI server engine adapter is never resolved (known F1 exemplar)

- `vibe/ui_server.py:3022-3035` declares engine/native/service globals as `None`
  and passes them directly to `create_default_service`.
- Repository-wide assignment grep finds no production assignment to any of the
  three globals.
- `core/handlers/model_hub/service.py:1417-1451` replaces a missing adapter with
  `UnavailableEngineAdapter`; its provision/discovery/sync/OAuth/invoke methods
  fail closed (`service.py:146-203`).
- Impact: API-key add, Hub OAuth, API-key migration, Hub source mutation, and
  runtime status are dead on the shipping live UI.

### P0-02 — Native OAuth adapter does not exist (known F2 exemplar)

- `core/handlers/model_hub/oauth.py:39-52` is the only native OAuth adapter and
  every method raises `NativeOAuthUnavailableError`.
- `ModelHubService.__init__` selects it by default (`service.py:363-380`).
- The UI exposes Claude/ChatGPT connect actions while `MODELS_API_MODE='live'`.
  Both start calls therefore return `engine_down` on master.

### P0-03 — OAuth success is not converted into a Source

- The server deliberately splits flow completion from source creation:
  `create_source(... oauth_flow_ref ...)` is the only path into
  `_create_oauth_source` (`service.py:586-650`, `751-759`).
- The live `ModelsApi` has no subscription-source creation method and
  `startOAuth` sends only vendor/channel (`modelsApi.ts:39-58`, `94-113`).
- On success, `OAuthConnectDialog` only shows a toast and calls a list refresh
  (`OAuthConnectDialog.tsx:105-112`; parent callback at
  `SettingsModelsPage.tsx:193-198`).
- The mock hides the gap by directly appending a Source in `completeFlow`
  (`modelsApi.ts:355-383`).
- This remains a P0 even after implementing the native bridge unless the bridge
  itself atomically materializes the source or the UI performs the required
  source-create handoff.

### P0-04 — The production turn path bypasses the complete resolver

- The frozen rule requires candidate #1, same-request fallback on eligible
  errors, exactly one refresh retry on 401, and no retry after streaming starts
  (`model-hub.md:108-118`).
- `ModelHubService.resolve()` implements that policy (`service.py:1291-1409`),
  but repository-wide call-site grep finds only tests.
- Production uses `ModelHubRuntimeRouter.resolve()`, which selects one source and
  injects its gateway (`model_hub.py:427-504`). Terminal failures are classified
  and cooled for the **next per-turn resolution** (`model_hub.py:524-529`).
- Claude, Codex, and OpenCode then emit the failed turn; none re-enters routing
  with the untouched user request. A user therefore sees the 429/5xx/network
  failure and must send another message before the backup source is selected.
- The 401 refresh and stream-start policies are green only in tests that call
  the dead service method directly.

### P1-01 — Engine/config ownership is split across two OS processes

- The controller constructs a real adapter-backed router
  (`controller.py:228-232`), while the Web UI runs as a separate subprocess
  (`runtime.py:1670-1700`).
- Adapter and supervisor singletons are process-local
  (`adapter.py:571-578`; `supervisor.py:218-239`); `EngineSupervisor.status()`
  knows only its in-memory child/connection (`supervisor.py:90-112`).
- There is no Model Hub RPC in `core/internal_server.py` or
  `vibe/internal_client.py`. Directly “wiring” the UI to
  `get_model_hub_engine_adapter()` would create a second supervisor rather than
  observe/control the controller-owned engine.
- Both processes also perform read-modify-write on the same V2 config. Their
  guards are `asyncio.Lock`/`threading.RLock` only
  (`service.py:386`; `v2_config.py:28`), so a UI mutation racing a controller
  cooldown/recovery write can lose one aggregate update.
- Required design: one authoritative service/lifecycle owner with RPC, or an
  explicit cross-process lease plus interprocess transaction/locking semantics.

### P1-02 — Four contracted mutation endpoints have no reachable UI action

- PATCH source and POST source-test are absent even from the `ModelsApi` type.
- DELETE source and DELETE custom-model have client methods, but repository-wide
  UI grep finds no component invocation (`modelsApi.ts:42-49`, `97-104`).
- `SourceRow` exposes only the reorder handle (`SourceRow.tsx:64-108`).
- Result: users cannot edit, delete, or re-test Sources, nor delete a custom
  model, despite complete server methods.

### P1-03 — Two required migration triggers are absent

- The spec requires first open after upgrade, setup wizard, and backend banner
  (`model-hub.md:157`).
- `MigrationDialog` is mounted only by `BackendSupplyModeCard`; its scan occurs
  only on that backend card (`BackendSupplyModeCard.tsx:92-113`, `211-218`).
- No setup-wizard or post-upgrade Model Hub scan/dialog reference exists.

### P1-04 — Experimental Hub subscription path is not merely off; it is broken

- The constant is false (`featureFlags.ts:34-41`), which is an intended default.
- If enabled, consent changes local `channel` only
  (`OAuthConnectDialog.tsx:278-333`). The client still posts only
  `{vendor, channel}` (`modelsApi.ts:109`).
- The service requires `experimental_consent: true` for Hub start and later for
  source creation (`service.py:1092-1098`, `693-699`). Thus the flagged-on path
  fails before OAuth starts and also lacks the finalization described in P0-03.

### P1-05 — Switch telemetry loses causal events across restart

- Cooldown state is durable, but previous launch and pending failure/reason are
  held only in router dictionaries (`model_hub.py:266-268`).
- `_emit_source_switch` and `_emit_channel_switch` require that memory
  (`model_hub.py:383-425`). If Avibe restarts after a failed turn but before the
  next turn, the next candidate is selected from persisted cooldown state with
  no corresponding `switch`/`channel_switch` event.
- This violates “every switch is appended,” although cooldown itself is logged.

### P1-06 — “Connect Hub” can persist Hub while launching Direct silently

- The UI reports success immediately after PATCH mode
  (`SettingsModelsPage.tsx:135-145`).
- `set_agent_mode` does not require an eligible configured route
  (`service.py:977-985`).
- `_is_bootstrap_unconfigured` then returns a Direct launch without emitting a
  switch (`model_hub.py:435-439`). This bootstrap is needed for a truly fresh
  install, but the model aggregate carries no fresh-vs-user-selected state, so
  the same silent fallback applies to existing users who explicitly click
  “Connect Hub.”

### P1-07 — OAuth flow persistence is internally incomplete

- `OAuthFlowRegistry` persists channel/source/vendor binding, but the real
  engine adapter stores active flows only in `_oauth_flows` and
  `_active_oauth_providers` memory (`adapter.py:99-102`).
- A UI-process restart retains a registry entry whose adapter flow no longer
  exists. Status/submit cannot resume or deterministically reconcile it.
- The restart-oriented API test reuses the same fake adapter instance, so it
  does not exercise adapter state loss.

### P1-08 — L7 availability/platform gate is still incomplete on a live nav

- The managed manifest contains only darwin-arm64 and linux-amd64 and points at
  upstream GitHub release assets (`cliproxyapi_manifest.json:10-26`).
- Frozen contract notes explicitly defer darwin-x64, linux-arm64, and the
  Avibe-owned mirror to L7 before GA
  (`model-hub-contracts/README.md:47`).
- Unsupported hosts correctly fail closed with Direct as escape, but the Model
  Hub nav already ships on. This is an explicit pre-GA completeness gap, not a
  newly discovered implementation regression.

### P2-01 — Runtime status can report the wrong manifest

- `EngineRuntimeManager` supports manifest path/URL overrides
  (`installer.py:35-50`) and the supervisor returns the actual contract manifest.
- `CLIProxyEngineAdapter.status()` discards it (`adapter.py:121-132`), while the
  API reconstructs a duplicated module constant (`service.py:89-107`,
  `346-359`). A repin or override can therefore install one manifest and report
  another.

### P2-02 — “View all” means only the first 20 events

- The API and log implement `before` pagination (`ui_server.py:3199-3206`,
  `events.py:162-169`).
- The UI fetches 20 once and the button merely expands that array
  (`SettingsModelsPage.tsx:74`; `RecentSwitchesCard.tsx:44-58`).

## Mandatory sweep evidence

### Fail-closed placeholders

Repository-wide searches over Model Hub code found:

- `UnavailableEngineAdapter` (`service.py:146-203`);
- `UnavailableNativeOAuthAdapter` (`oauth.py:39-52`);
- UI globals `_MODEL_HUB_ENGINE_ADAPTER`, `_MODEL_HUB_NATIVE_OAUTH_ADAPTER`, and
  `_MODEL_HUB_SERVICE` initialized to `None` (`ui_server.py:3022-3024`), with no
  production assignment;
- no Model Hub `NotImplementedError` path; and
- remaining `raise ModelHubError(...)` sites are validation/fail-closed branches,
  except that the default placeholders turn otherwise built routes into
  `engine_down` as described above.

### Endpoint/UI call graph

- 20/20 contract routes registered.
- 16/20 invoked by a live component.
- Missing component calls: source PATCH, source DELETE, source test, custom-model
  DELETE.
- No UI request path points at a nonexistent `/api/models/*` route.
- `MODELS_API_MODE='live'`, nav enabled, menus enabled
  (`featureFlags.ts:18-32`).

### Feature flags and i18n

- Subscription Hub and cross-vendor advanced behavior are default-off.
- The subscription Hub branch cannot work when enabled (P1-04).
- Static check covered 149 `t('...')` references in the Model Hub UI: zero
  missing in either locale.
- `en.json` and `zh.json` each have 2,613 scalar leaves with identical paths.
  The nine `settings.models.*` keys without literal occurrences are all reached
  by dynamic backend/provider key templates; there are no orphaned Model Hub
  locale keys after accounting for those templates.

### Per-turn backend reachability

- Claude: session creation resolves/binds the launch before client reuse/create
  (`session_handler.py:724-749`) and applies Hub env/settings/fingerprint
  (`session_handler.py:890-1013`).
- Codex: resolution occurs under the per-session lock before transport selection
  (`codex/agent.py:175-189`), and transport identity is changed with the launch.
- OpenCode: overlay is prepared before server configuration and the requested
  model is bound to the overlay source snapshot before the prompt.
- Terminal errors from all three reach `record_native_failure`; the gap is not
  router reachability but the absence of an in-turn re-entry/replay loop.

### Migration and engine lifecycle

- Scan is live, read-only, and parses all three native config families.
- Apply is live for `keep_native`; API-key import reaches the unwired engine.
- Engine auto-install is on-demand at first Hub gateway/OAuth use, not on status
  read. That trigger placement is correct.
- Runtime/status exists but reports the placeholder in the UI process and cannot
  see a controller-process engine.

## Why green tests missed these seams

The following tests inject fakes at exactly the broken boundaries:

| Test area | Fake seam | What stays unproved |
| --- | --- | --- |
| REST contract | `tests/test_model_hub_api.py:170-182` builds `ModelHubService` with one `FakeAdapter` for both engine and native OAuth; `:213-218` monkeypatches `_model_hub_service` | default UI service resolution, real engine/native adapter selection |
| OAuth source completion | REST test manually POSTs `/sources` after making fake OAuth successful (`test_model_hub_api.py:420-443`) | live UI success -> source finalization |
| Resolution taxonomy | `tests/test_model_hub_resolution.py:45-116` uses `FakeAdapter`; tests call `service.resolve` directly (`:205-331`) | any real backend turn calling the resolver, same-turn retry in Claude/Codex/OpenCode |
| Runtime router | `tests/test_model_hub_injection.py:58-149` injects `LaunchAdapter`/memory store; failover tests explicitly call `router.resolve` a second time (`:152-247`) | failed current turn replay, default adapter, process restart causal events |
| Migration | scenario fixture uses `MemoryStore` + `MigrationAdapter` (`test_model_hub_migration_scenarios.py:37-108`) | default API-key migration using the UI service adapter |
| Engine runtime | adapter/supervisor tests instantiate isolated fake installers, processes, or clients | UI/controller cross-process ownership and status truth |
| UI | no Model Hub component integration tests were found | endpoint reachability, OAuth materialization, migration triggers, flag-on consent payload |

Required seam/default-resolution regression coverage:

1. Instantiate the unpatched UI `_model_hub_service()` and assert it resolves the
   production engine adapter and production native OAuth bridge, never either
   `Unavailable*` class.
2. Run each adapter-dependent REST route through that default service factory:
   API-key add/test/delete, API-key migration apply, OAuth start/status/submit/
   cancel/finalize, and runtime status.
3. Add live-client component coverage for OAuth success -> exactly one persisted
   Source for Claude native, ChatGPT native, and experimental Hub.
4. Add a production-boundary scenario where each backend receives a pre-stream
   429/5xx/network outcome and completes the same user turn on candidate #2; add
   401-refresh-once and post-stream-no-retry variants.
5. Restart between terminal failure and the next turn; assert cooldown plus
   switch/channel-switch/recovery events remain complete and nonduplicated.
6. Assert UI/controller share one engine lifecycle owner: runtime/status observes
   the turn-started engine, OAuth cannot spawn a second engine, and concurrent
   cooldown plus UI mutation preserves both changes.
7. Add UI call-graph tests for all 20 contract endpoints, including source
   edit/delete/re-test and custom-model delete; assert no non-contract path.
8. Turn `SUBSCRIPTION_HUB_EXPERIMENTAL` on in a test build and assert explicit
   consent reaches both OAuth start and source finalization.
9. Mount first-run/post-upgrade/setup/backend contexts and assert the migration
   dialog trigger policy exactly once per required context.
10. Override the engine manifest in a test and assert runtime/status returns the
    effective installer manifest rather than a duplicated constant.

## Close-out gate

F1 and F2 close only P0-01/P0-02 unless their final code also proves the adjacent
seams. Model Hub should not be called complete until P0-03/P0-04 are independently
closed, P1-01 establishes a single process owner, all current-head P0/P1 default-
resolution tests are green, and the explicit L7 pre-GA gate is finished.
