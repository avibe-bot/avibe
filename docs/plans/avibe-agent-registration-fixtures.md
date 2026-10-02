# Avibe Agent registration: test fixture inventory

Inventory date: 2026-10-02.

The first `rg` pass searched `tests/**/*.py` and fixture JSON/YAML for the
exact backend ids. An AST pass then retained literal lists, tuples, sets, and
dict keys containing at least two distinct ids. The baseline scan found 328 Python container
sites in 87 Python files, plus two relevant JSON fixtures. Nested containers
with the same purpose are grouped below. Function-call arguments, per-record
mapping values, provider/model text, and scenario records are excluded unless
the enclosing container is itself a backend membership fixture.

Locators below refer to that baseline. This inventory records final decisions,
not pending recommendations. Native sample fixtures do not declare the Agent
universe. In particular, native inherited/default routes and Direct mode must
not be widened to Avibe's explicit-route-only C-4 contract.

The decision boundary is:

- `AGENT_BACKENDS`: Agent creation, routing, listings, Model Hub consumers,
  shared prompt/output behavior, and ordinary IM selectors. These sites gain
  `avibe`.
- `NATIVE_CLI_BACKENDS`: CLI presence, install, OAuth, native session/process
  discovery, direct mode, restart, native files, and native-only protocol
  behavior. These remain native.
- A fixed native subset or released compatibility shape stays literal when the
  test is intentionally proving that subset or historical data shape.

Parent updates already applied and therefore not pending in this inventory:

- `tests/test_agent_service.py` uses `AGENT_BACKENDS`.
- `tests/test_v2_config_platform_registry.py` compares against
  `list(AGENT_BACKENDS)`.
- `tests/test_v2_compat_platforms.py` covers the compat `None` lifecycle.
- `tests/test_backend_restart.py` uses the catalog-derived optional-backend
  snapshot.
- `core/backend_restart.py` no longer carries the obsolete native pair.
- `tests/test_model_hub_failure_presentation.py` uses `AGENT_BACKENDS` for the
  shared failure/evidence projection; its native HTTP recorder pair remains
  native-only.
- `tests/test_prompt_render_export.py` uses `AGENT_BACKENDS` for generic
  product/parameterization sites; its approved-guidance digest remains a
  historical native-only fixture.

## A. Agent-surface candidates: final disposition

| File and literal site | Updated to catalog or intentionally retained native scope |
| --- | --- |
| `tests/e2e/test_model_hub_migration_persisted.py:34`, `_seed_hub` | Retained native credential-migration setup; no in-process native credential store is invented. |
| `tests/scenario_harness/model_hub.py:306`, `config_with_sources` | Retained native seed map. `ModelHubConfig` already supplies the empty Avibe row, and Avibe explicit-route consumers populate it. This is not an exhaustive-map assertion. |
| `tests/scenario_harness/model_hub_native_oauth.py:82`, `MemoryStore.__init__` | Retained native OAuth setup; maps at 131 and 209 are credential subsets. |
| `tests/scenarios/model_hub/test_model_hub_configured_route_scenarios.py:72,108,140,182,233` | Retained native automatic/passthrough/default routing and Hub/Direct products. Avibe intentionally does not inherit native routes. |
| `tests/scenarios/model_hub/test_model_hub_live_resolution_scenarios.py:231,234`, `_config`; `397,405-414`, gateway switch; `1360`, unlisted model | Retained native requested-model/protocol fixtures, Direct-to-Hub switch, and native-launch refusal. Avibe has no native requested-model fallback. |
| `tests/scenarios/model_hub/test_model_hub_migration_scenarios.py:56,720-721,818` | Retained native vendor catalog placement, credential migration and adoption-mode assertions. |
| `tests/test_api_save_config_merge.py:62-83,169,1276` | Merge/deprecated-field and serializer completeness assertions now use `AGENT_BACKENDS` (plus `avault`). The old input payload deliberately omits Avibe and retains deprecated default-model fields to test compatibility. |
| `tests/test_controller_config_reload.py:21-26`; `tests/test_discord_guild_settings.py:25-30` | Retained released-compatible native config inputs, as detailed in C; omitted optional Avibe must still load. |
| `tests/test_backend_failure_retry.py:234` | Updated canonical-continue/draft matrix to `AGENT_BACKENDS`; native restart-receipt fixture at 99 is retained. |
| `tests/test_harness_failure_retry.py:133` | Retained native start-receipt lifecycle via `_bind_test_native_start`. Generic retry and callback settlement are expanded separately. |
| `tests/test_model_hub_api.py:142,4086,4135,4151,9833` | Catalog ID edit/admission/reload matrices use `AGENT_BACKENDS`. Retained MemoryStore native seed map and native automatic Source-order example; Avibe's normalized empty row and explicit routes are tested separately. |
| `tests/test_model_hub_api.py:1049`, `test_runtime_stop_protects_configured_avibe_models`; `4477-4486` and `4867-4876` | These already expose Avibe explicitly. Keep them as the boundary checks while deriving surrounding expected backend maps from the Agent catalog. |
| `tests/test_model_hub_avibe_consumer.py:705`, `test_terminal_response_origin_follows_its_carrier` | Already includes `avibe`; it is the correct shape for an all-Agent origin matrix. |
| `tests/test_model_hub_config.py:1410,3132,3154` | Serializer and empty-route normalization/recovery matrices now use `AGENT_BACKENDS`; native Direct-mode recovery expectations remain fixed. |
| `tests/test_model_hub_failure_presentation.py:44` | Parent updated the shared failure/evidence projection to `AGENT_BACKENDS`; its native HTTP recorder pair remains native-only. |
| `tests/test_model_hub_injection.py:267` | Explicit route-alias metadata matrix now uses `AGENT_BACKENDS`; native overlay assertions remain conditional. |
| `tests/test_model_hub_l3.py:691-695,1177` | Retained native requested-model and configured native-route templates, not exhaustive Agent membership assertions. |
| `tests/test_model_hub_resolution.py:317,456,2363` | Cross-backend Hub Source eligibility now uses `AGENT_BACKENDS`; retained native catalog/default seed and vendor placement examples (including 2377/2387). |
| `tests/test_model_hub_routing_modes.py:376,409,439,472,512,560,595,619,643,673,731,739,763,799` | Retained native inherited automatic/passthrough/default-route behavior, Hub/Direct products and native legacy-ID compatibility matrix. Avibe's explicit-route policy must not inherit those expectations. Line 457 is a historical native fixed-menu pair. Shared catalog admission now includes Avibe in `test_model_hub_api.py`. |
| `tests/test_prompt_render_export.py:100,154,180,210,223,259,287,310,522` | Parent audit already assigned all product/parameterization sites to `AGENT_BACKENDS`. Keep line 522, `test_approved_guidance_changes_preserve_all_other_injection_bytes`, as the historical native-only digest fixture. |
| `tests/test_runtime_ownership.py:303` | Shared AgentService probe loop uses `AGENT_BACKENDS`; actual OpenCode process-resource assertions in the same test remain native-specific. |
| `tests/test_scheduled_tasks.py:7434` | Callback terminal-once matrix now uses `AGENT_BACKENDS`; native binding example at 13055 remains fixed. |
| `tests/test_settings_handler.py:510-514,532,601,650,607-610` | Registered/enabled fixture and whole-set assertions derive from `AGENT_BACKENDS`; Avibe stays visible when native peers are disabled. Claude/Codex reasoning metadata remains a capability-specific expectation. |
| `tests/test_telegram_bot.py:1336,1434,1460,1498` | Routing fixture derives from `AGENT_BACKENDS`; fourth-backend picker cases exercise both Avibe and the unknown-extra-backend case. |
| `tests/test_ui_api.py:4183` | Built-in preference test now supplies `AGENT_BACKENDS`; fixed two-backend enablement examples at 4084/4092 remain. |
| `tests/test_managed_skills.py:709` | Backend-neutral prompt/skill matrix now uses `AGENT_BACKENDS` and actually passes the selected backend to the renderer (previously it rendered the same default three times). |

## B. Intentionally retained native capability fixtures

These fixtures retain their native literal memberships. They exercise CLI,
native files, credentials, session/process or protocol behavior, and are not
Agent-universe declarations. No Avibe native resource is fabricated.

| File and literal site | Native boundary |
| --- | --- |
| `tests/scenario_harness/auth_setup.py:13`, `save_direct_auth_config` | Direct-mode auth setup; Avibe only supports Hub mode. |
| `tests/test_agent_auth_service.py:41`, `_IsolatedClaudeConfigDirMixin.setUp` | Native auth isolation and Direct-mode setup. Lines `2421-2455` belong to the explicit C-8 classifier inventory and are excluded from this audit. |
| `tests/test_backend_connection.py:64`, `connection`; `152,160,187`; `255,263`; `885` | Native direct readiness, install application, disabled native configuration, and pending takeover. The separate Avibe connection test is already present at lines 436-460. |
| `tests/test_backend_restart.py:174-180,202` | Fixed native restart/IPC examples. The new catalog-derived optional-backend projection test covers Avibe's disabled state separately. |
| `tests/test_claude_cli_path.py:235-239,302` | Desktop/native executable resolution and missing private CLI selectors. |
| `tests/test_controller_model_hub_gate.py:147,338` | CLI presence probe and direct-mode fallback. Avibe has no CLI and cannot be forced into Direct mode. |
| `tests/test_desktop_backends.py:117,997` | Native install publication and private backend removal. |
| `tests/test_global_agents_md.py:90,136` | Global prompt files are owned by native CLI backends in production. |
| `tests/test_internal_client.py:372,378,380-383`; `tests/test_internal_server.py:846,852-856` | Backend restart/reconcile IPC contract; keep the selector native. |
| `tests/test_member_management_parity.py:168` | Native backend connection management route; Avibe has no native connection projection. |
| `tests/test_model_hub_api.py:820, runtime-stop branch`; `1041,1049,1064,1143,1524`; `2341,2681`; `5344,9629` | Fixed native Direct-mode setup, native protocol projection and recovery; explicit Avibe Hub-only cases remain in A. |
| `tests/test_model_hub_avibe_consumer.py:347,350`, native history; `897`, native transport clients | Explicit native callers do not carry Avibe origin metadata. |
| `tests/test_model_hub_retry_lifecycle.py:33-37`, `BACKENDS`; `156`, native failure recovery | Protocol endpoint map and native failure behavior are native-only. |
| `tests/test_native_login_single_flight.py:49`; `tests/test_settings_disk_fallback.py:38` | Native auth fixtures force Direct mode. |
| `tests/test_native_process_inventory.py:46,119,139,263,310,204,241` | Native executable/process inventory and smaller native credential subsets, not in-process runtime ownership. |
| `tests/test_native_session_providers.py:590` | Native session visibility and provider behavior. |
| `tests/test_native_takeover_lifecycle.py:28,138-139,492,526,584,699` | Native takeover journal, process inventory, auth service, and native writer ownership. |
| `tests/test_native_writer_custody.py:42,65,263` | Native credential custody and writer refusal. |
| `tests/test_ui_api.py:2263,2291,2314,2392,2440,2532` | Desktop install, private runtime ownership, update checks, and CLI path resolution. |
| `tests/test_ui_server_fastapi.py:2278`; `tests/test_ui_server_install.py:159,210` | Native runtime projection and installation jobs. |
| `tests/test_update_checker_platforms.py:1099-1103,1162` | Native package update and admission behavior. |
| `tests/test_web_oauth_flow.py:47,505` | Native Web OAuth setup and callback failure recovery. |

## C. Keep as intentional native-only, subset, or historical fixtures

These sites should not be expanded to an unimplemented in-process adapter.
They either encode a released shape, a credential store subset, a fixed native
vendor example, or a native adapter-specific comparison. They may retain
literal ids; where the test means “every native backend”, replacing the literal
with `NATIVE_CLI_BACKENDS` is optional only if it does not weaken the historical
or subset assertion.

| File and literal site | Why the native-only shape is intentional |
| --- | --- |
| `tests/e2e/test_api.py:14-19`, `_VALID_CONFIG`; `tests/test_controller_config_reload.py:21-26`; `tests/test_discord_guild_settings.py:25-30`; `tests/test_ui_server_mutation_protection.py:144-149,185-190,217-222,259-264,431-436` | Minimal/released-compatible config POST fixtures. They prove old payloads remain loadable; absent optional Avibe config is part of the compatibility contract. |
| `tests/fixtures/agent_registration/released_agents.json` | Released config snapshots with only native agent sections. Keep unchanged to prove missing optional Avibe loads disabled. |
| `tests/fixtures/model_hub/released_pre_avibe_consumer.json` and `tests/test_model_hub_avibe_consumer.py:538-539` | Pre-Avibe Model Hub snapshot. The test explicitly proves native rows are preserved and an empty Avibe row is added. |
| `tests/test_agent_organization_onboarding.py:82` | Explicit two-backend system-agent onboarding count fixture; it is a controlled example, not the Agent universe. |
| `tests/test_backend_model_catalog.py:1660,1678-1681`; `tests/test_latest_version_cache.py:177,182-185,245,264` | Native package/model cache neighbor fixtures. Avibe has no native CLI/latest probe. |
| `tests/test_model_hub_api.py:2295`, fixed Claude/Codex built-in catalogs; `2783`, provider candidate pair | Native fixed-menu/provider examples, not all Agent rows. |
| `tests/test_model_hub_config.py:1486`, legacy payload; `2747`, legacy Direct mode; `3166`, recovered native Direct modes | Released legacy/recovery shapes. They intentionally assert native rows stay Direct while Avibe stays Hub. |
| `tests/test_model_hub_config_read_compatibility.py:280` | Compatibility fixture for a native pending runtime service. |
| `tests/test_model_hub_l3.py:1258`, `_canonicalize_fixed_test_routes; 2259` | Fixed native menu route examples used by native resolution tests. |
| `tests/test_model_hub_migration_auth_mirror.py:54,84`; `tests/test_model_hub_migration_review_boundaries.py:135-152`; `tests/test_model_hub_migration_source_identity.py:29,83,207` | Native credential-file, journal-guard, and source-identity subsets. |
| `tests/test_model_hub_migration_custody_history.py:107,140,172,197,226,243,331,435,443` | OAuth custody/history examples deliberately cover Claude/Codex and selected OpenCode API-key combinations. |
| `tests/test_model_hub_native_takeover.py:28,175,271,430`; `tests/test_model_hub_takeover_integration.py:154`; `tests/test_model_hub_takeover_inventory.py:111` | Native takeover, credential cleanup, keychain, and journal fixtures. |
| `tests/test_model_hub_persisted_inventory.py:90,190-191,367-368`; `tests/test_model_hub_persisted_migration.py:139-151` | Shared credential consent examples for selected native stores. |
| `tests/test_model_hub_provenance.py:360` | Historical native catalog identity persisted in provenance. |
| `tests/test_model_hub_resolution.py:2377,2387` | Subscription vendor placement expected only for the fixed native vendor catalogs; Avibe has no such native vendor catalog. |
| `tests/test_model_hub_retry_policy.py:78,255`; `tests/test_opencode_model_hub_retry.py:65` | Native retry/protocol examples and cross-native launch comparison. |
| `tests/test_model_hub_runtime_default_upgrade.py:33,145-148,167,244,265,278` | Legacy on-disk upgrade fixtures intentionally omit Avibe, then assert the migration adds an empty Hub row without changing native Direct rows. |
| `tests/test_prepare_regression.py:230-234,282-286,301-305` | Regression layout/config fixtures for installed native executables. |
| `tests/test_save_opencode_provider_auth.py:484-489,718-723` | OpenCode provider-auth fixtures with disabled native peers; not a backend-universe selector. |
| `tests/test_session_fork.py:2113`; `tests/test_scheduled_tasks.py:13055` | Explicit native session/binding examples; product Session fork and Harness behavior are broader, but these fixtures exercise native ids and state. |
| `tests/test_skills_service.py:174,200-240,658,709-720,763,888,925` | CLI skill command arguments target the three native skill adapters. Do not add Avibe until an in-process skill adapter exists. |
| `tests/test_memory_removal.py:20,45` | Historical config includes native agent keys and the separate `avault` section while proving a retired top-level section is ignored. |
| `tests/test_model_hub_avibe_consumer.py:679-687` | Synthetic historical native provenance records intentionally omit Avibe origin fields. |
| `tests/e2e/test_model_hub_runtime.py:749,758` | Fixed native Direct-to-Hub switch setup and its three configured runtime blockers; empty Avibe does not block runtime stop. |
| `tests/test_agent_steering.py:288` | Constructs the real three native adapters and checks native attachment steering; no in-process adapter double is substituted. |
| `tests/test_cli_task_command.py:3652`; `tests/test_command_handler_user_names.py:760`; `tests/test_controller_im_ready.py:17`; `tests/test_controller_vibe_agent_routing.py:63,84,175`; `tests/test_runtime_activation.py:595`; `tests/test_slack_app_mention_empty.py:271` | Explicit two-native-Agent examples for default selection, cleanup, startup failures, legacy routing, activation and backend-switch isolation. They do not claim exhaustive backend membership. |
| `tests/test_incus_regression.py:1071` | Verifies installed native executable files, not Agent availability. |
| `tests/test_managed_skills.py:355,381,437` | Expected fixture skill names under native CLI home directories, not backend-universe declarations. |
| `tests/test_resume_session.py:106,176,276,299,320` | Fixed native session import/resume bindings and native hook implementations. |
| `tests/test_sqlite_sessions_store.py:679,1501,1874,4158,4285`; `tests/test_sqlite_state_migration.py:5968` | Native binding/legacy adoption and historical multi-backend native session-map conflicts. |

The following literal sites were intentionally not classified as backend
universe fixtures:

- `tests/test_backend_registration_contract.py:37-84` is the explicit C-8
  classification inventory itself.
- `tests/test_model_hub_avibe_contract_matrix.py:88` scans schema membership
  and already names all four backends; it is a contract expectation.
- `tests/test_agent_backend_catalog.py:22` and
  `tests/test_v2_config_platform_registry.py` are already catalog-derived or
  parent-fixed.
- Dicts that map one test record to a backend-specific object, such as the
  controller/resume/session-store fixtures, are per-record data rather than
  universe declarations.
- `tests/scenarios/**/catalog.yaml` backend fields identify individual
  scenarios; they are not membership lists. Other YAML/JSON hits are prose,
  observations, or single scenario records.

## Outcome

Each enumerated fixture is catalog-expanded or intentionally retained as a
native capability/example/historical shape. The production C-8 contract remains
the single automatic guard against unclassified backend declarations. Tests
do not invent native Avibe login, CLI, model defaults, Direct mode or sessions.
