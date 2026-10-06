# Avibe Agent registration (C-8)

> Superseded in part by [`avibe-agent-always-on.md`](avibe-agent-always-on.md): the Avibe Agent is the built-in
> backend, always enabled, and `agents.avibe` no longer exists. The opt-in and disabled-state statements below are
> this lane's history.

## Change contract

Avibe Agent is an opt-in, in-process backend. The backend catalog declares the
Agent universe and the native-CLI subset. Agent creation, routing, listings,
configuration, Model Hub and ordinary IM selectors consume the Agent universe.
Native authentication, install/version probes, native session discovery/import,
native configuration files and manual process restart consume the CLI subset.

The existing backend cards, settings page, Agent picker and model-route controls
are reused. Catalog capabilities hide unsupported native operations. A checked-in
TypeScript projection of the Python catalog supplies frontend capabilities and
types; its exact contents are contract-tested.

Persisted configs without `agents.avibe` load with it disabled. The new section
contains only `enabled`. A successful enablement mutation uses the existing
controller reconciliation IPC path. Adapter registration and controller refresh
implementation belong to `feat/avibe-agent-adapter`.

## Validation boundaries

- Catalog/classification contract: all full-universe declarations equal the Agent
  or CLI catalog set; explicit capability subsets/supersets retain their relation.
  A newly introduced literal list must fail the inventory test.
- Released config fixtures and round trips: old backend settings remain unchanged,
  absent Avibe stays off, invalid optional Avibe config recovers off on disk load.
- Agent store and run target: create, list, select and resolve an Avibe Agent using
  temporary SQLite state. Existing tests cover only CLI-backed Agents.
- UI rendering: Avibe is selectable and configurable with no native action or CLI
  probe; Model Hub exposes its existing backend model controls.
- Native boundaries: installing, authenticating, restarting or resuming a native
  Avibe session is rejected before touching any native state.
- Changed-file Ruff, focused pytest/Vitest and UI production build; exact-head
  Codex review, CI and unresolved-thread gates before delivery.

## Known by design

- No default model is invented for an empty Avibe model catalog. Model Hub owns
  its available models and the selected Agent stores the model.
- Native resume modals in Slack/Discord/Feishu are not ordinary Agent pickers;
  Avibe has no native session store and is excluded there.
- Product Session fork and Ask-in-new keep their existing UI. The adapter lane
  supplies C-5 context anchors; native-session exclusion does not disable them.
- Native setup onboarding, import and global native prompt files remain CLI-only.
- No new visual element, dependency, production restart or merge is authorized.

## Validation recorded

- Catalog, config load, readiness and regression-script batch: 143 passed.
- Expanded config-save/API/auth/native-session/IM batch: 490 passed, 99 subtests.
- Routing, dispatch, C-8 inventory, Model Hub and IM batch: 176 passed, 2 subtests.
- UI focused suites, ESLint, test type checking and production build passed.
- Changed Python Ruff and whitespace checks passed.

These are hermetic boundary checks, not a live adapter/provider acceptance run.
The adapter dependency and a later integrated Incus acceptance pass remain
explicit handoff work; no local service was restarted.

## Review ledger

### Round 1 — `f5ea8124b`

One findings-bearing reviewed head; no repeated root-cause class and no
architecture rewrite. The circuit breaker has not tripped.

- Public-symbol migration: the only removed public Python name is
  `core.vibe_agents.SUPPORTED_AGENT_BACKENDS`. Whole-repository search, including
  tests, scripts and UI, found one stale consumer module:
  `tests/test_agent_service.py` (import and parametrization). It now imports the
  catalog directly. The contract document's historical mention remains prose,
  not a caller. No compatibility alias is retained.
- No TypeScript public export was removed or renamed. `AssistantId`,
  `ASSISTANT_ORDER`, the two `BACKEND_ORDER` exports, `Backend`, `BackendId`,
  `OAuthBackend`, `AgentBackend`, `NativeCliBackend`, `AGENT_BACKENDS`,
  `AGENT_ID_TO_BACKEND` and `backendsFromAgents` keep their public names and
  derive/re-export the relevant catalog types. Private `BackendKey` and
  `BackendOption` were removed; whole-repository search found no references.
  Python `MODEL_HUB_BACKENDS`, script `SUPPORTED_BACKENDS`,
  `recommended_agent_model` and `default_cli_for_backend` retain their names.
- Reproduction: `pytest --collect-only tests/test_agent_service.py` failed with
  the reported import error before the fix. The complete file passes 104 tests
  after the import correction, including Avibe model-selection cases.
- Backend brand ownership: all four backend display names belong to catalog
  metadata and are locale-invariant product names. Translated surrounding
  prose/descriptions remain in i18n. UI maps must project the catalog rather
  than define a second literal name table; the redundant `avibeTitle` keys
  are removed.
- CI native-boundary drift: widening `DirectHome` to all Agent backends caused
  key coverage to expand a nonexistent Avibe Direct-mode description. The
  native-only landing is narrowed again; no Avibe Direct key or test exception
  is introduced.
- CI test registration: `test_v2_config_platform_registry` now compares the API
  projection with the catalog. With the orchestrator's explicit authorization,
  `tests/test_backend_registration_contract.py` is registered exactly once in
  the **I7 contract-completion implementation (backend)** binding row of
  `docs/plans/model-hub-implementation.md`; no checker/schema change was made.
  The complete Model Hub/config/registration batch passes 202 tests locally,
  including the O1 live ownership guard.
- Cross-lane integration, same round: disabled Avibe compat configuration must
  be `None`, just like Codex/OpenCode, so rolling refresh unregisters it.
  `AppCompatConfig.avibe` is optional and `to_app_config` creates it only when
  enabled. The compat-boundary regression failed first on the disabled state;
  prior config tests checked persisted fields, not the runtime absence sentinel.
  A distinct coordinator projection regression failed first because its old
  two-backend subset labeled disabled Avibe unavailable. That predicate now
  uses the Agent catalog, retaining Claude's registered-with-flag exception.
  Disabled optional backends are applied; enabled-but-unregistered backends
  remain unavailable. The obsolete native-only classification was removed.
- Compat reader audit: current-tree `.avibe` attribute reads outside tests are
  V2 config serialization/recovery, not the optional compat field. Dynamic
  compat consumers in `AgentAuthService` resolve/load `None` safely, unregister
  on absence, and skip absent auth-mirror targets; `SettingsHandler` reports
  absent config disabled. The native process inventory's CLI-path fallback is
  native-only and accepts absence. The coordinator snapshot is corrected above.
  The adapter branch at the inspected remote SHA `b64957872` guards startup
  registration with `getattr(config, "avibe", None)` and receives non-None config
  for refresh registration. Its owner retains responsibility for the requested
  `.enabled` guards and real-wiring enable/disable test; no adapter code changed
  here. Compat/coordinator/connection/registration batch: 151 passed.
- Test-fixture enumeration is recorded in
  [the fixture inventory](avibe-agent-registration-fixtures.md): the baseline
  search covered 328 literal container sites in 87 Python files and two JSON
  fixtures. A final AST pass also checks that every container-bearing test
  file is represented. Shared serializers, prompt/skill renderers, retry,
  callback, ownership probes, model catalog/metadata and IM picker matrices
  now include Avibe through the catalog. Native inherited/passthrough routes,
  Direct mode, native launch receipts, credentials and released inputs retain
  their specific semantics; they are not generalized into invented Avibe
  native behavior.

#### Backend display-name consumers

The shared path is `catalog.display_name` -> generated frontend projection ->
`AGENT_BACKENDS.label` -> `getBackendUiMeta` or derived `BACKEND_LABEL`.

| Surface | Enumerated rendering sites |
| --- | --- |
| Settings | `SettingsBackendsPage` row/configure label; `SettingsBackendPage` title/card; `BackendLifecycleChip`; `BackendRuntimeCard` through the generic page and all three native provider-config callers |
| Agent management | `NewAgentDialog` choices; `AgentsPage` group headings, filter trigger/items, import menu, import-none toast, detail prose; `GlobalPromptsDialog` native tabs/save/sync/effect; `AgentGraphOrphanStrip`; `ReadyBanner` |
| Model Hub | `AgentCard` headings/direct explanation; `SettingsModelsPage` Direct headings; `BackendSupplyModeCard`; `BackendModelCatalogDialog`; `BackendModelPickerDialog` title/builtin group; `BackendModelEditorDialog`; `SourceRow`; `SourceDetailPanel`; `SourceOrderDrawer`; `RouteChainDialog`; `RouteOriginBadge`; `GuardImpact`; `GuardGapList`; `MigrationDialog` |
| Onboarding and routing | `CollaborationStory` card/ARIA label; provider `DestinationRow`; `AssistantRow`; `BackendConnectionDialog`; `AgentDetection`; `UserList`; `ChannelList` |
| Python/IM | Slack/Discord/Feishu ordinary backend selectors; Telegram summary/inline/overflow picker; Slack/Feishu question titles; CLI doctor native-backend rows; Model Hub terminal-copy backend interpolation and blocked native-migration item names |

Removed duplicate locale keys (English and Chinese):
`settings.backends.avibeTitle`, `settings.models.backends.*`,
`onboarding.story.<native>.name`, and `agents.importFromClaude`,
`agents.importFromCodex`, `agents.importFromOpenCode`. Whole-UI search confirms
no remaining consumers. `agents.importFrom` localizes prose with backend-name
interpolation, never a display-name-derived key.

The orchestrator separately authorized catalog-only label projections/imports
in `core/handlers/model_hub/provenance.py` and
`core/handlers/model_hub/migration.py::_blocked_item`. Failure policy, vendor and
protocol mappings, Source subscription identities and non-brand copy keys are
unchanged. The three affected Model Hub fixture expectations were updated.
Python `modelHub.backends.*`, Telegram `backend.*`, and unused
`modal.question.*` brand keys were removed from both locale catalogs. Telegram's
existing overflow-picker regression first failed with `Backend: avibe`, then
passed with the catalog name.

The final repository-wide Python/TypeScript/JSON search finds no obsolete
brand-key consumers or competing rendered backend-name literals. Exact-name
hits outside the catalog/generated projection are independent test expectations,
non-rendered documentation comments, and the explicitly retained Source
subscription identities below.

Retained exceptions are Source subscription/vendor identities (`Claude`,
`ChatGPT`, etc.), complete localized native provider-page titles
(`SettingsClaudeProviderPage`, `SettingsCodexProviderPage`,
`SettingsOpencodeProviderPage`), authored
onboarding example prose/geometry, and diagnostic/monospace backend IDs. These
are not competing standalone backend display names.

UI regressions failed first for the old picker name, import-key construction,
and English/Chinese Model Hub brand ownership. Final validation: 173 focused
tests plus 2,142 Model Hub/onboarding/related tests; ESLint, test type checking
and production build pass. Existing key-coverage collector dedup fixtures remain;
the application case now guards native Direct descriptions and the absence of
an Avibe Direct key.

Additional round-1 final checks: 515 AgentService/prompt/config/Model Hub tests;
151 compat/coordinator/connection/registration tests; 20 catalog-ID cases;
11 shared delivery/ownership/default/eligibility cases; 8 planning-metadata
cases; and 269 doctor/Telegram/provenance/failure/migration/live-route tests.
These batches overlap earlier checks and are not a unique-test total.
The final i18n/migration batch passed 172 tests. Feishu/Slack routing fixture
files passed separately (8 and 7 tests): combining their existing module-level
`aiohttp` stubs with real HTTP scenario imports causes collection pollution,
so those isolated adapter fixtures were not used as a shared scenario harness.
No unrelated test bootstrap was changed.
