# Antigravity PR scope audit

Owner decision: PR #4252 contains only Antigravity and shared changes its integration/acceptance requires. No merge is authorized. Audit compares the complete `origin/master...HEAD` delta (master `376522e95`) and pending scope corrections, including UI, backend, docs, tests and build policy. No CI workflow or database migration file is changed.

## Removed

| File | Reason | Decision |
| --- | --- | --- |
| `ui/src/components/workbench/ShowPageShareControl.tsx` | Unrelated share-panel splitting. | Remove via normal revert `4eea00eb0` (`fd65f841c`). |
| `ui/src/components/VaultSandboxAppearanceBridge.tsx` | Unrelated Vault appearance wiring in an owner-gated security area. | Remove via normal revert `f4e7bb567` (`f2ace7dbf`). |
| `ui/src/lib/vaultSandboxClient.ts` | Same Vault workaround; no Antigravity dependency. | Remove via the same normal revert. |
| `ui/src/lib/vaultSandboxIntegrity.test.ts` | Only tests that reverted Vault wiring. | Remove via the same normal revert. |
| `docs/plans/model-hub-implementation.md` | Incidental insertion of an Antigravity test into an old ownership ledger; no build/test dependency. | Remove; restore master's text. |

The three Vault files and Show sharing control have no remaining PR diff. Existing Vault cryptography, appearance delivery and authorization are unchanged.

## First-screen measurement

Same dependencies, build settings and current master source; measure the real synthetic merge tree after both reverts using `check:first-load` (Node gzip level 6).

| Route | Master | Antigravity after reverts | Delta | Old budget | New budget |
| --- | --- | --- | --- | --- | --- |
| Home | 607.7 KiB / 4 JS files | 609.3 KiB / 4 JS files | about 1.6 KiB | 608 KiB | 610 KiB |
| Chat | 903.8 KiB / 59 JS files | 905.4 KiB / 59 JS files | about 1.6 KiB | 904 KiB | 906 KiB |

There are **zero newly loaded modules** in either route's static/preloaded module closure. The same 11 existing modules change (one locale is loaded at a time). The added Antigravity code is small shared catalog/route/picker branches and metadata, with new copy delivered in the existing complete locale. Provider-specific content does not add an eager import; moving locale keys to feature namespaces would alter the shared i18n/type/test contract. The owner-authorized allowance is the measured growth rounded up to the next KiB, 2 KiB per route; no unrelated code is re-split.

| Existing loaded module with added Antigravity content | First-screen responsibility | Decision |
| --- | --- | --- |
| `ui/src/App.tsx` | Route registration; the provider page itself is lazy. | Keep route stub. |
| `ui/src/routePages.ts` | Lazy provider-page declaration, not its implementation. | Keep deferred. |
| `ui/src/lib/agentBackendCatalog.generated.ts` | Identify available Agents/backends for the first-screen picker. | Keep metadata. |
| `ui/src/lib/agentBackends.ts` | Backend identity, picker/list labels and avatar styles. | Keep metadata. |
| `ui/src/components/visual/backendBrandMarks.ts` | Backend mark where Antigravity appears in a list/picker. | Keep tiny existing mark table entry. |
| `ui/src/lib/backendAccent.ts` | Agent/backend identity colors. | Keep metadata. |
| `ui/src/lib/backendModels.ts` | Existing picker read helper; only its API branch is added. | Keep shared helper. |
| `ui/src/lib/effortOptions.ts` | Existing picker effort mapping. | Keep tiny mapping entry. |
| `ui/src/components/settings/models/types.ts` | Direct-only/Hub membership distinction used by existing shared UI. | Keep predicate; type-only declarations erase. |
| `ui/src/context/ApiContext.tsx` | Existing first-screen API provider; added Antigravity request closures run only when called. | Keep shared provider, no added feature import. |
| `ui/src/i18n/en.json` or `zh.json` | Existing whole-locale bundle supplies backend labels; provider-copy keys are not individually drawn on the first screen. | Keep locale contract; measured bytes included in allowance. |

`SettingsAntigravityProviderPage`, `AntigravityProviderConfig`, `AntigravityConnectionForm`, onboarding's `BackendConnectionDialog`, and design frames are absent from both first-screen closures. No new on-demand boundary is needed for those Antigravity modules.

## Retained file-by-file delta

Every remaining path is accounted for below. Shared entries are required by Antigravity, rather than general cleanup. Regression-only changes in Claude/OpenCode keep existing contracts valid under the new shared environment/catalog semantics. In particular, removing the empty-effort fixture update breaks the exhaustive catalog test, and removing the SQLAlchemy preload breaks the new real-writer effort test under the isolated loader/SQLAlchemy 2.1.

| File | Reason | Decision |
| --- | --- | --- |
| `README.md` | Document Antigravity among supported native backends. | Keep — Antigravity integration/coverage |
| `README_ZH.md` | Chinese supported-backend documentation counterpart. | Keep — Antigravity integration/coverage |
| `config/v2_compat.py` | Convert Antigravity V2 config into the controller launch config. | Keep — shared, required by Antigravity |
| `config/v2_config.py` | Antigravity config defaults/auth fields and separation of native backends from Model Hub membership. | Keep — shared, required by Antigravity |
| `config/v2_settings.py` | Persist Antigravity native-agent routing selection. | Keep — shared, required by Antigravity |
| `core/agent_auth_service.py` | Antigravity sign-in, PTY/code flow, credential probe, logout, and standard failure recovery. | Keep — shared, required by Antigravity |
| `core/agent_tool_policy.py` | Antigravity schedule/workflow/swarm tool entries route durable work to Harness. | Keep — shared, required by Antigravity |
| `core/backend_restart.py` | Let direct-only Antigravity credential leases bypass Model Hub supply lookup. | Keep — shared, required by Antigravity |
| `core/caller_context.py` | Session-stable environment excludes Run/source, fixing agy respawns; Claude shares this projection. | Keep — shared, required by Antigravity |
| `core/controller.py` | Register/clean up Antigravity; keep Model Hub iteration restricted to its own backends. | Keep — shared, required by Antigravity |
| `core/handlers/command_handlers.py` | Antigravity resume aliases; move native history RPC reads off the controller loop. | Keep — shared, required by Antigravity |
| `core/handlers/message_handler.py` | Antigravity subagent routing and cached/off-loop model-menu effort materialization required by acceptance. | Keep — shared, required by Antigravity |
| `core/handlers/model_hub/migration.py` | Restrict existing Model Hub migration/routing/state logic to Hub-capable backends; Antigravity is direct-only. | Keep — shared, required by Antigravity |
| `core/handlers/model_hub/migration_journal.py` | Restrict existing Model Hub migration/routing/state logic to Hub-capable backends; Antigravity is direct-only. | Keep — shared, required by Antigravity |
| `core/handlers/model_hub/resolver.py` | Restrict existing Model Hub migration/routing/state logic to Hub-capable backends; Antigravity is direct-only. | Keep — shared, required by Antigravity |
| `core/handlers/model_hub/rpc.py` | Restrict existing Model Hub migration/routing/state logic to Hub-capable backends; Antigravity is direct-only. | Keep — shared, required by Antigravity |
| `core/handlers/model_hub/service.py` | Restrict existing Model Hub migration/routing/state logic to Hub-capable backends; Antigravity is direct-only. | Keep — shared, required by Antigravity |
| `core/handlers/session_handler.py` | Antigravity route persistence and off-loop native history preview RPC. | Keep — shared, required by Antigravity |
| `core/handlers/settings_handler.py` | Antigravity IM model/effort menu from the canonical catalog. | Keep — shared, required by Antigravity |
| `core/managed_skills.py` | Discover agy native Skill roots alongside the existing canonical catalog. | Keep — shared, required by Antigravity |
| `core/mcp_hub/native.py` | Read/import Antigravity global/project MCP configuration. | Keep — shared, required by Antigravity |
| `core/services/running_agents.py` | Antigravity live runtime inspection and end action. | Keep — shared, required by Antigravity |
| `core/services/session_fork.py` | Reserve and persist bounded Antigravity native/summary fork handoffs with retry-safe delivery. | Keep — shared, required by Antigravity |
| `core/services/skills.py` | Keep askill Antigravity attribution and canonical .agents/skills installation. | Keep — shared, required by Antigravity |
| `core/session_handoff.py` | Bound Antigravity summary forks to their reserved transcript point. | Keep — shared, required by Antigravity |
| `core/session_turns.py` | Mark Antigravity accepted work as process-bound for restart reconciliation. | Keep — shared, required by Antigravity |
| `core/update_checker.py` | Antigravity script idle updates, settled-version protection and verified restoration. | Keep — shared, required by Antigravity |
| `core/vibe_agents.py` | Antigravity default model, native definitions/import and thought_level fields. | Keep — shared, required by Antigravity |
| `desktop/src-tauri/src/workbench-routes.json` | Allow the Antigravity backend settings route in desktop navigation. | Keep — Antigravity integration/coverage |
| `docs/CLI.md` | Document Antigravity model changes applying at the next Turn. | Keep — Antigravity integration/coverage |
| `docs/COMMANDS.md` | Document Antigravity aliases, resume and setup commands. | Keep — Antigravity integration/coverage |
| `docs/plans/2026-11-24-acp-runtime-antigravity.md` | Antigravity implementation/acceptance change contract. | Keep — Antigravity integration/coverage |
| `docs/plans/2026-11-25-antigravity-parity-fixes.md` | Owner-authorized Antigravity parity and acceptance contracts. | Keep — Antigravity integration/coverage |
| `modules/agents/acp/__init__.py` | ACP stdio/session/update transport used by Antigravity; no existing backend migrates to it. | Keep — Antigravity integration/coverage |
| `modules/agents/acp/client.py` | ACP stdio/session/update transport used by Antigravity; no existing backend migrates to it. | Keep — Antigravity integration/coverage |
| `modules/agents/acp/events.py` | ACP stdio/session/update transport used by Antigravity; no existing backend migrates to it. | Keep — Antigravity integration/coverage |
| `modules/agents/antigravity/__init__.py` | Antigravity adapter implementation:   init  . | Keep — Antigravity integration/coverage |
| `modules/agents/antigravity/agent.py` | Antigravity adapter implementation: agent. | Keep — Antigravity integration/coverage |
| `modules/agents/antigravity/auth.py` | Antigravity adapter implementation: auth. | Keep — Antigravity integration/coverage |
| `modules/agents/antigravity/models.py` | Antigravity adapter implementation: models. | Keep — Antigravity integration/coverage |
| `modules/agents/antigravity/rules.py` | Antigravity adapter implementation: rules. | Keep — Antigravity integration/coverage |
| `modules/agents/base.py` | Off-loop title backfill prevents native Antigravity history RPC from blocking the controller. | Keep — shared, required by Antigravity |
| `modules/agents/catalog.py` | Catalog-owned Antigravity registration, capabilities and agy executable name. | Keep — Antigravity integration/coverage |
| `modules/agents/model_hub.py` | Direct-only Antigravity must not enter Hub routing or supply lookup. | Keep — shared, required by Antigravity |
| `modules/agents/native_sessions/antigravity.py` | Antigravity passive native history listing/loading with resolved executable. | Keep — Antigravity integration/coverage |
| `modules/agents/native_sessions/providers.py` | Register Antigravity native history provider. | Keep — Antigravity integration/coverage |
| `modules/agents/native_sessions/types.py` | Extend native history backend type with Antigravity. | Keep — Antigravity integration/coverage |
| `modules/agents/subagent_router.py` | Discover and select Antigravity native definitions in supported project/global roots. | Keep — shared, required by Antigravity |
| `scripts/incus_tenant.py` | Explicitly disable optional Antigravity in regression fixtures until the CLI is installed. | Keep — Antigravity integration/coverage |
| `scripts/prepare_regression.py` | Preserve disabled Antigravity config and normalize its agy executable path. | Keep — Antigravity integration/coverage |
| `skills/use-avibe/SKILL.md` | Expose Antigravity configuration/routing to the Avibe operator. | Keep — Antigravity integration/coverage |
| `skills/use-avibe/references/backends.md` | Antigravity operator reference for auth, updates and model selection. | Keep — Antigravity integration/coverage |
| `storage/settings_service.py` | Load/store Antigravity channel routing variant. | Keep — shared, required by Antigravity |
| `tests/helpers/antigravity_harness.py` | Hermetic Antigravity/ACP fake peer and adapter harness. | Keep — Antigravity integration/coverage |
| `tests/helpers/fake_acp_server.py` | Hermetic Antigravity/ACP fake peer and adapter harness. | Keep — Antigravity integration/coverage |
| `tests/scenarios/auth_setup/catalog.yaml` | Antigravity passive-auth and recovery scenarios alongside existing auth ownership guards. | Keep — Antigravity integration/coverage |
| `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` | Antigravity passive-auth and recovery scenarios alongside existing auth ownership guards. | Keep — Antigravity integration/coverage |
| `tests/test_acp_client.py` | Hermetic regression coverage for Antigravity/ACP acp client. | Keep — Antigravity integration/coverage |
| `tests/test_agent_backend_catalog.py` | Antigravity registration/integration regression at agent backend catalog boundary. | Keep — Antigravity integration/coverage |
| `tests/test_agent_tool_policy.py` | Antigravity registration/integration regression at agent tool policy boundary. | Keep — Antigravity integration/coverage |
| `tests/test_antigravity_agent.py` | Hermetic regression coverage for Antigravity/ACP antigravity agent. | Keep — Antigravity integration/coverage |
| `tests/test_antigravity_auth_and_install.py` | Hermetic regression coverage for Antigravity/ACP antigravity auth and install. | Keep — Antigravity integration/coverage |
| `tests/test_antigravity_parity.py` | Hermetic regression coverage for Antigravity/ACP antigravity parity. | Keep — Antigravity integration/coverage |
| `tests/test_antigravity_registration.py` | Hermetic regression coverage for Antigravity/ACP antigravity registration. | Keep — Antigravity integration/coverage |
| `tests/test_backend_model_catalog.py` | Antigravity registration/integration regression at backend model catalog boundary. | Keep — Antigravity integration/coverage |
| `tests/test_backend_registration_contract.py` | Catalog-owned backend universes distinguish Antigravity direct-only membership and capability subsets without weakening enumeration. | Keep — Antigravity integration/coverage |
| `tests/test_caller_context.py` | Session-stable Run/source exclusion and retained trusted identity. | Keep — Antigravity integration/coverage |
| `tests/test_claude_cli_path.py` | Claude fixed-env parity for the shared caller-context fix: new Run reuses, new Session recreates. | Keep — Antigravity integration/coverage |
| `tests/test_cli_task_command.py` | Inherited incompatible effort clears; explicit unsupported effort still refuses. | Keep — Antigravity integration/coverage |
| `tests/test_core_services_settings.py` | Antigravity registration/integration regression at core services settings boundary. | Keep — Antigravity integration/coverage |
| `tests/test_desktop_backends.py` | Antigravity registration/integration regression at desktop backends boundary. | Keep — Antigravity integration/coverage |
| `tests/test_global_agents_md.py` | Antigravity registration/integration regression at global agents md boundary. | Keep — Antigravity integration/coverage |
| `tests/test_member_management_parity.py` | New Antigravity HTTP routes remain in the complete authorization inventory with their required roles. | Keep — Antigravity integration/coverage |
| `tests/test_message_handler_typing.py` | Actual SQLite effort materialization matches Antigravity applied menu; SQLAlchemy preload preserves isolated loader coverage. | Keep — Antigravity integration/coverage |
| `tests/test_model_hub_api.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_model_hub_config.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_model_hub_failure_presentation.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_model_hub_injection.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_model_hub_resolution.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_native_login_single_flight.py` | Antigravity registration/integration regression at native login single flight boundary. | Keep — Antigravity integration/coverage |
| `tests/test_native_login_structure_guards.py` | Antigravity registration/integration regression at native login structure guards boundary. | Keep — Antigravity integration/coverage |
| `tests/test_native_takeover_lifecycle.py` | Keep Model Hub tests scoped to Hub-capable backends after adding direct-only Antigravity. | Keep — Antigravity integration/coverage |
| `tests/test_opencode_reasoning_variants.py` | Existing exhaustive catalog test must accept Antigravity models with explicitly empty effort menus; removing this update breaks it. | Keep — Antigravity integration/coverage |
| `tests/test_session_fork.py` | Antigravity bounded summary/native-tip fork selection and persisted handoff state. | Keep — Antigravity integration/coverage |
| `tests/test_session_model_cli.py` | Antigravity authoritative menus, inherited clearing and explicit-effort refusal. | Keep — Antigravity integration/coverage |
| `tests/test_session_titles.py` | Native history title lookup is asynchronous after adding the ACP provider. | Keep — Antigravity integration/coverage |
| `tests/test_settings_handler.py` | Antigravity registration/integration regression at settings handler boundary. | Keep — Antigravity integration/coverage |
| `tests/test_skills_service.py` | Antigravity askill attribution and actual canonical .agents/skills discovery under isolated local-source fixtures. | Keep — Antigravity integration/coverage |
| `tests/test_ui_server_fastapi.py` | Antigravity registration/integration regression at ui server fastapi boundary. | Keep — Antigravity integration/coverage |
| `tests/test_update_checker_platforms.py` | Antigravity idle-update settlement and restore verification contract. | Keep — Antigravity integration/coverage |
| `ui/design/frames/models/backend-detail.design.tsx` | Antigravity provider/auth design frames and backend fixtures; design-only entry, not first screen. | Keep — Antigravity integration/coverage |
| `ui/design/frames/models/backends.fixtures.ts` | Antigravity provider/auth design frames and backend fixtures; design-only entry, not first screen. | Keep — Antigravity integration/coverage |
| `ui/e2e/onboarding-fidelity/lineup.spec.ts` | Onboarding lineup now includes Antigravity and its healthy direct-only connection. | Keep — Antigravity integration/coverage |
| `ui/e2e/onboarding-fidelity/support.ts` | Onboarding lineup now includes Antigravity and its healthy direct-only connection. | Keep — Antigravity integration/coverage |
| `ui/scripts/check-first-load-budget.mjs` | Measured 2 KiB per-route allowance for Antigravity metadata and current whole-locale delivery. | Keep — Antigravity integration/coverage |
| `ui/src/App.tsx` | Register the lazy Antigravity settings route. | Keep — Antigravity integration/coverage |
| `ui/src/components/Wizard.tsx` | Separate Model Hub bootstrap order from the setup lineup that now contains Antigravity. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/AssistantSetup.test.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/BackendConnectionDialog.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/BuiltinAssistant.test.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/CollaborationStory.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/Welcome.test.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/WizardCompletion.test.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/collaborationTimeline.test.ts` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/entryGate.test.ts` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/entryGate.ts` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/onboarding.css` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/onboarding/useSetupModelChoice.ts` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/BackendOAuthPanel.tsx` | Antigravity backend OAuthPanel auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/BackendTestPanel.tsx` | Antigravity backend TestPanel auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/SettingsAntigravityProviderPage.tsx` | Antigravity SettingsProviderPage auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/SettingsBackendPage.tsx` | Antigravity Settingsbackend Page auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/SettingsBackendsPage.test.tsx` | Antigravity Settingsbackend sPage auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/models/SettingsModelsPage.tsx` | Antigravity SettingsModelsPage auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/models/migrationGrouping.ts` | Antigravity migrationGrouping auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/models/types.ts` | Antigravity types auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/oauth/useBackendOAuth.ts` | Antigravity usebackend OAuth auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/providers/AntigravityConnectionForm.tsx` | Antigravity ConnectionForm auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/providers/AntigravityProviderConfig.test.tsx` | Antigravity ProviderConfig auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/providers/AntigravityProviderConfig.tsx` | Antigravity ProviderConfig auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/providers/BackendConnectionForm.tsx` | Antigravity backend ConnectionForm auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/providers/connection.css` | Antigravity connection auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/settings/settingsRoutePages.ts` | Antigravity settingsRoutePages auth/provider/menu integration and its coverage. | Keep — Antigravity integration/coverage |
| `ui/src/components/steps/AgentDetection.tsx` | Antigravity setup lineup and direct-only admission; existing Hub-backed admission stays gated. | Keep — Antigravity integration/coverage |
| `ui/src/components/visual/backendBrandMarks.ts` | Antigravity backend mark for picker/list identity. | Keep — Antigravity integration/coverage |
| `ui/src/context/ApiContext.tsx` | Antigravity typed auth/model API methods and runtime update fields. | Keep — Antigravity integration/coverage |
| `ui/src/i18n/en.json` | Antigravity English setup, auth, model-test, backend and source labels. | Keep — Antigravity integration/coverage |
| `ui/src/i18n/zh.json` | Antigravity Chinese counterparts, including the owner-approved personal sign-in notice. | Keep — Antigravity integration/coverage |
| `ui/src/lib/agentBackendCatalog.generated.ts` | Generated Antigravity catalog entry from the Python source of truth. | Keep — Antigravity integration/coverage |
| `ui/src/lib/agentBackends.ts` | Antigravity picker branding, order, avatar and presentation. | Keep — Antigravity integration/coverage |
| `ui/src/lib/backendAccent.ts` | Antigravity color and Skills backend label mapping. | Keep — Antigravity integration/coverage |
| `ui/src/lib/backendModels.ts` | Antigravity native model menu/label API branch. | Keep — Antigravity integration/coverage |
| `ui/src/lib/effortOptions.ts` | Antigravity supported effort fallback family, with live per-model menus authoritative. | Keep — Antigravity integration/coverage |
| `ui/src/routePages.ts` | Lazy Antigravity settings-page export. | Keep — Antigravity integration/coverage |
| `vibe/api.py` | Antigravity auth/model/runtime/install/update surfaces and shared cached menu projection. | Keep — shared, required by Antigravity |
| `vibe/authorization.py` | Antigravity endpoints receive the same Member auth and Editor catalog permissions as existing backends. | Keep — shared, required by Antigravity |
| `vibe/backend_model_catalog.py` | Antigravity bundled/live authoritative catalog and exact per-model efforts. | Keep — shared, required by Antigravity |
| `vibe/cli.py` | Antigravity multi-vendor menu and shared inherited-vs-explicit effort rule required by acceptance. | Keep — shared, required by Antigravity |
| `vibe/cli_health.py` | Recognize Antigravity in native CLI health checks. | Keep — Antigravity integration/coverage |
| `vibe/cli_paths.py` | Map agy/agy.exe to the Antigravity managed backend through the catalog. | Keep — shared, required by Antigravity |
| `vibe/cli_repair.py` | Recognize Antigravity script install targets without treating it as an npm/brew package. | Keep — shared, required by Antigravity |
| `vibe/data/backend_models.json` | Captured agy 1.5.17 eleven-model menu and exact per-model efforts. | Keep — Antigravity integration/coverage |
| `vibe/desktop_backends.py` | Antigravity verified release archives, platform executable resolution, pinning and managed update environment. | Keep — shared, required by Antigravity |
| `vibe/global_agents_md.py` | Antigravity user-global instructions path. | Keep — Antigravity integration/coverage |
| `vibe/i18n/en.json` | Localized Antigravity runtime/auth/fork/update notices. | Keep — Antigravity integration/coverage |
| `vibe/i18n/zh.json` | Localized Antigravity runtime/auth/fork/update notices. | Keep — Antigravity integration/coverage |
| `vibe/ui_server.py` | Antigravity auth/model routes and E2E registration; async native probes stay off-loop. | Keep — shared, required by Antigravity |
| `docs/plans/2026-11-26-antigravity-scope-audit.md` | Requested complete Antigravity scope/measurement ledger. | Keep — scope evidence. |

## Validation

- UI build and explicit `check:first-load` pass both on the task branch (608.1 / 904.1 KiB) and on the prospective current-master merge tree (609.3 / 905.4 KiB). `build` and `check:first-load` are separate npm scripts in this checkout, so both were run explicitly.
- Desktop build passes.
- 166 tests pass in `ShowPageShareControl`, `ShowPageSharingSettings`, its i18n suite, `vaultSandboxClient` and `vaultSandboxIntegrity`; no real service or account is used.
- Ruff passes on all 90 Python files in the complete PR diff; this scope correction adds no Python changes.
- `git diff --check` passes. The existing budget check reproduces both pre-allowance failures (609.3 > 608 and 905.4 > 904) and passes with the minimal measured allowance.
- Fresh exact-head Codex review and CI are required after the scope-correction push. Do not merge.
