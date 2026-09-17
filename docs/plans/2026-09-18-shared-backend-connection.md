# Shared backend connection and setup readiness — issue #2011

## Authorization and baseline

Owner session `sestqz5wvu5ty` explicitly requested implementation of #2011 on 2026-09-18 at 02:16:59 Asia/Shanghai. #2010 and its fidelity follow-up #2017 are merged (#2015 and #2028). #2011 and #2012 are active in separate worktrees with separate sole writers; #2013 remains deferred. Previous sequential and #2010-only pause language is superseded by the owner’s parallel authorization.

Repository: avibe-bot/avibe. Branch: `feat/shared-backend-connection`. Assigned worktree: `/Users/max/workspace/ai/avibe/.worktrees/avibe/shared-backend-connection`. Starting master: `4019b704c99afe16223d475fccd9e1cb94a109d7`, verified against GitHub and containing #2028. The local clone has a narrow origin fetch refspec: `git fetch origin master` alone may leave origin/master stale. Refresh explicitly with `git fetch origin refs/heads/master:refs/remotes/origin/master` and verify the SHA before later integration.

## Outcome and preserved boundaries

A first-time user can install any supported assistant, apply one subscription or key connection through the same logic as Settings → Backends, and explicitly enter the workspace without configuring IM. A valid existing connection counts. Executable discovery, selecting a method, seeing a code, opening a browser, or an optimistic UI flag never counts as a connection. One unrelated missing/installing/upgrading assistant cannot block another usable assistant.

Use the approved 568px dialog family in `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen`. Preserve #2028's 1104px shared width, responsive geometry, assets and automatic animation with no playback controls. Use existing en/zh localization and Light/Dark/System ownership. Existing selectors retain their interaction. Do not reinterpret the design as a full Settings page squeezed into a modal.

This issue does not implement Workbench/General Settings, native Desktop presentation, a new credential store, Model Hub management, real credential onboarding on this machine, or a release. It does not change App authorization rules, member/remote eligibility, or continuing access for previously completed setup. A user's later credential expiry does not force them through setup again.

## Contract and ownership

One implementation writer owns this complete React/Python connection flow; a read-only design lane supplies source evidence. The separate #2012 writer consumes the frozen public readiness interface without modifying this lane’s owners.

1. **Installation:** existing detect/install/lifecycle producers own CLI discovery and canonical paths. `found` is installation evidence only; a rejected probe is unknown/error, not confirmed missing. Keep per-backend ownership, live local toggles, canonical non-ASCII paths, stale-result protection and the reconciliation fixes from #2015.
2. **Claude:** `ApiContext.getClaudeAuth` / `saveClaudeAuth` and their existing Python handlers own effective state and persistence. `active_auth_mode`, `has_api_key`, `credential_type`, `has_oauth_credentials`, and runtime apply receipts must be interpreted consistently with Settings. Support subscription/manual callback, API Key and Auth Token. No plaintext credential readback or copying into another store.
3. **Codex:** `getCodexAuth` / `saveCodexAuth` own auth state and key/base-URL persistence. Preserve the distinction between saved `auth_mode`, effective `active_auth_mode`, and `auth_mode_uncertain` (keychain-backed credentials must not be falsely declared absent or valid). Subscription uses the existing device flow. A confirmed existing connection should not require a new login or a new mandatory billable model call.
4. **OpenCode:** `getOpencodeProviders`, `setOpencodeProviderAuth`, default-provider configuration and the existing runtime provider-auth catalog own provider identity, methods and active auth. OAuth uses the OpenCode-specific provider start endpoint and existing backend flow status/cancel/submit semantics. Never route an OpenCode provider choice to Claude/Codex or Model Hub auth storage, or hard-code a replacement provider catalog.
5. **OAuth lifecycle:** share/extract `BackendOAuthPanel`, `OAuthFlowParts`, `useOAuthFlowLock` and their existing controller logic as appropriate. Lock method/provider changes during an active handshake; reject duplicate submit. Close/Escape/Cancel cancel the active operation without removing prior saved credentials. Handle close during pending start, pending poll/save and late completion so orphaned flows and stale success cannot update another backend or a closed/reopened dialog. Terminal flow success must refresh effective server state before rendering connected.
6. **Save/apply:** preserve absent/null/empty-key and URL semantics, masked-key behavior, existing credentials, errors with editable inputs, and notices/partial outcomes. A failed live application must not be mislabeled successful connection. No client-side second restart after accepted backend reconciliation. Existing Settings and the compact onboarding view must share behavior rather than separate handlers that drift.
7. **Readiness:** derive from authoritative installation and effective applied auth for the same backend. Pending/failed/uncertain reads remain non-success with retry/recovery, not false disconnected claims. Preserve explicit enablement and routing semantics; before choosing how a disabled-but-authenticated backend becomes usable, trace the existing configuration owner and document the smallest consistent behavior. Never auto-enable on render/detection. Any new API/readiness field must have its actual producer, consumer and uncertainty semantics documented in this plan before implementation, using existing state before adding a mechanism.
8. **Completion:** `Wizard` remains the orchestration owner and writes the existing `setup_completed` using narrow config mutations only after explicit Enter workspace and fresh readiness. Do not require IM/platform/channel/Summary steps on this new completion path. Preserve existing platform/config state and any separately used management surfaces. Navigate to `/` with React Router state `{ onboardingCompleted: true }`; this is a transient navigation fact for #2012, not authentication, persisted state or a fabricated available-Agent record. Do not implement the #2012 banner now. Prove a usable existing Agent/default resolution remains available after completion.

## Source and reuse inventory before edits

Read `AGENTS.md`, `.agents/skills/pr-delivery-loop/SKILL.md`, `docs/plans/2026-09-17-shared-web-desktop-design.md`, `docs/plans/2026-09-17-onboarding-design-fidelity.md`, `docs/plans/setup-backend-runtime-reconcile.md`, and the current issue body. The old 2026-09-15 design handoff is historical: current issue semantics and final canvas annotations govern when it contradicts the compact setup flow.

Read final auth boards through Pencil: initial IDs from the existing shared plan are `V0zHJ`, `OHULW`, `MvT2k`, `oLe1U`, `FwRTK`, `laAX1`, `MtOWg`, `KWKHX`, `JtVGU`, `r2y97O`, `D6uRiy`, `MGCD0`; re-discover current names, frame variants and descendants rather than assuming this list is complete. Use explicit `filePath`: another agent is working on Miao's active canvas. Read/export only; do not mutate, save or copy designs. Record actual frames, geometry, tokens, states and remaining deliberate exceptions. Missing Light EN duplicates can compose approved English copy with the same Light tokens; do not invent a new design.

Inventory backend form/controllers and the full Python apply/cancel path before extracting. Relevant existing files include `settings/providers/{BackendProviderConfig,ClaudeProviderConfig,CodexProviderConfig,OpencodeProviderConfig,opencodeProviderAuth}`, `settings/BackendOAuthPanel`, `settings/oauth/OAuthFlowParts`, `settings/shared/{useOAuthFlowLock,useBackendRuntime}`, `context/ApiContext`, `Wizard`, `steps/AgentDetection`, `onboarding/AssistantRow`, `lib/wizardConfigMutations`, existing backend auth service/handlers and `vibe/api.py` / `vibe/ui_server.py`.

## Allowed implementation scope

- Above setup/auth/provider components and directly related reusable hooks/forms, tests and scoped onboarding styles; relevant localization keys only.
- Minimal additive `ApiContext` types/methods and existing Python auth/setup/config owners where traced call paths require it; no unrelated schema migration or auth architecture rewrite.
- `tests/scenarios/auth_setup/` catalog and scenario cases, directly relevant unit/consumer/browser tests and this plan. Consolidate the shared plan's active status to #2011 and prior slice completion without rewriting unrelated history.
- Global theme primitives/tokens, App routing/AuthGuard semantics, Model Hub implementation and other redesign issues are outside scope; route a concrete necessary deviation to PM before editing.

## Verification and acceptance

Read `standards/scenario-testing/AGENTS.md`, `tests/scenarios/INDEX.yaml`, the auth_setup catalog, observations and listed harness cases. Scenario IDs must be real catalog entries introduced/updated in the same change, never invented in the PR description.

- Cover each backend's key/subscription path, Claude Auth Token, OpenCode provider-specific device/browser/manual capability, stored valid/uncertain auth and effective-vs-saved mode.
- Exercise real consuming components for close/Escape/cancel, delayed start/save/poll, method locks, duplicate submit, retry retaining input, stale replies and serial config reconciliation.
- Cover one ready backend with other missing/upgrading/error rows; installation-only disabled entry; no automatic navigation; explicit completion without IM; existing-user state preserved; non-ASCII paths and concurrent narrow config changes.
- Add closed-loop auth_setup scenarios using existing real service/config owners with test-owned fake upstream/CLI boundaries, so persistence→apply→effective read→readiness is actually exercised. Mock-only UI screenshots do not prove real OAuth/install.
- Browser verification against exported final dialogs in en/zh and Light/Dark: representative desktop and narrow viewport, all relevant states, keyboard focus/cancel/overflow. Batch first review and one targeted correction confirmation; do not endlessly polish or regenerate matrices without a new failure.
- Run focused tests, applicable Python/Ruff checks, UI build/lint, theme/catalog and current translation/type gates. Broaden only for real failures/concerns; CI handles slow repository-wide checks.

All probes are hermetic: redirect the entire HOME/XDG/CLI-config/token/keychain path to test-owned resources and deny uncontrolled external requests. Do not use this machine's saved auth, production profiles, running Avibe service or tenant instances. No primary-checkout edits or `.runtime` writes; use test-owned local processes for evidence. Incus remains deferred here because its runner writes primary metadata. Record real isolated installation/live OAuth as residual manual acceptance where not performed; never silently perform it with the owner's credentials.

## Delivery and liveness

The owner authorized implementation, tests, branch commits, push and non-draft PR delivery; not merge. Finish the bounded implementation and required evidence, then send PM a concrete diff/current head and evidence for one pre-push visual/consumer spot-check. This is an internal PM gate, not a new owner permission request. Continue independent tests/documentation while PM checks; explicitly deliver the report to `sestqz5wvu5ty` if a background command consumes the usual callback.

After PM checks: open non-draft PR to master, `Closes #2011`, read back title/body/base/head, immediately seed and arm one durable combined PR+lint lane Watch (`--forever`, both timeout layers 0, independent persistent cursor). New-PR bootstrap exception applies. Notify PM with PR/full head/Watch ID so PM arms its independent Watch. **Never manually post `@codex review`; cyhhao triggers automatically.** Observe pickup and report a verified gap instead of duplicate triggering.

Paginate all findings/threads and count reviewed heads/root-cause classes each round. Two heads with a repeated class, or three findings-bearing heads after a model rewrite, stop blind repair for PM diagnosis; green tests do not waive the breaker. Final report precedes lane Watch cleanup. No merge, release, service restart or work on the parallel #2012 / deferred #2013 implementation.

## Progress

- [x] Owner start instruction and latest master verified; isolated worktree created.
- [x] Native design evidence and Settings/API ownership inventory.
- [x] Implement shared connection forms/lifecycle and authoritative readiness/completion.
- [ ] Focused/scenario/browser verification and independent PM spot-check.
- [ ] PR, exact-head Codex review, complete CI and zero unresolved threads.
- [ ] Owner acceptance / separately authorized merge.

## Whole-path inventory and UI extraction (2026-09-18)

- `AgentDetection` owns per-backend discovery/install/canonical paths and live
  enable drafts; `Wizard` owns explicit completion. The old unconditional
  OpenCode permission gate must become specific to OpenCode's readiness.
- Settings providers currently own native credential reads and saves. Extract a
  shared connection form used by Settings and the compact dialog; runtime,
  Model Hub supply controls, provider/model management and optional billable
  probes remain in Settings. No new credential persistence.
- `BackendOAuthPanel` owns start/status/submit/cancel. Extract its controller for
  both presentations. An operation generation invalidates stale async work;
  unmount/cancel invalidates first, cancels known flows, and cancels a late start
  by its returned flow ID. Duplicate start/submit use synchronous ownership.
  Native backend login uses `force_reset=false`: merely starting or cancelling
  login must not run logout. The existing Claude settings backup owns recovery.
- Key save keeps absent key = reuse, empty/null base URL = clear, and no OAuth
  base-URL writes. Masked values are display-only. Errors retain entered values.
  Save/apply warnings and fresh effective reads are shared across consumers.
- Native `Get` confirmed 568px / padding24 / gap20 / radius16 / shadow 0 18 48
  (#00000088 Dark), 40px method segment, 42px fields, 38px actions, 40px logo
  well with 28px existing logo; title20/600, target12, introduction13/1.55.
  Full observer packet supplies Light and all language/state variants.

### Exact type additions before their consumers

- `OpencodeMutationResult.restart?: BackendRestartResult`: producer already
  exists in `save_opencode_provider_auth_async`; both Settings and connection
  form consume it. `ok:true` means persisted, `restart.ok:false` means failed
  live apply and must retain a recoverable state, never connected.
- `OAuthWebStartResult.backend` and `OAuthWebStatus.backend` include `opencode`:
  existing Python producers already emit this value; the originating backend
  remains the controller identity for every later operation.
- Shared UI form accepts `{backend, provider?, onConnected?, onCancel?, compact?}`.
  `provider` is a runtime catalog row and is required only for OpenCode. Its
  read/save callbacks always target OpenCode ownership. `onConnected` runs only
  after persisted/apply results and a fresh effective read agree; dismissal
  invalidates that callback without pretending a submitted write was undone.

Runtime/readiness IPC additions follow the approved disposition and exact
producer/consumer contract below. Canonical `V2Config.default()` is workbench-only. Existing
incomplete enabled IM drafts retain the existing completion validator and data.

### PM-approved runtime observation boundary

PM approved the scoped IPC/coordinator extension on 2026-09-18. No persistent
ledger, config generations, schema or second lifecycle controller is added.

- `BackendRestartCoordinator.snapshot(backend)` produces `{state, error?}` with
  `state: applied | draining | failed | unavailable`. It reads the existing
  task, retained last outcome and actual registered backend. Prepare failures,
  task failures and cancellation remain failed until a successful application.
- Verified UDS `GET /internal/backend-application/{backend}` projects that state
  through the existing allowlist. The UI's existing API owner combines it with
  original save/OAuth receipts. A bounded process-local receipt per backend
  retains failure to deliver a refresh marker; querying an idle controller
  cannot erase that failure. Only explicit successful apply clears it.
- `GET /api/backend/{backend}/connection` produces `BackendConnectionState`:
  `{ok, backend, installed, enabled, auth: subscription | api_key | none | unknown,
  application: applied | draining | failed | stopped | unknown, ready,
  entry_eligible, permission_required?, message?}`. The existing native auth
  readers/provider catalog produce `auth`; no model-call test is added. A
  provider counts only with an effective API/OAuth entry, not a keyless/local
  catalog row. Codex keychain uncertainty is `unknown` regardless of saved mode.
- `ready` requires the same backend installed/enabled, confirmed credential
  source, controller applied and (OpenCode only) existing tool permission.
  `entry_eligible` additionally accepts a confirmed stopped controller with
  persisted auth. A running service with unavailable IPC stays unknown.
- A fresh GET observes native launch auth and the last Avibe application, not
  continuous remote credential validity or external-file hot reload. This
  limitation applies equally to Settings; known apply failure takes precedence.
- Wizard explicitly rechecks candidates, starts only a confirmed stopped
  service through the existing start owner, confirms usable application/Agent
  selection, and writes only `setup_completed` before navigation. If start
  requires completion first, the ordering and failure recovery will be tested
  and documented before choosing that route. Existing invalid enabled IM drafts
  retain credential validation and the inline saved-platform recovery below.

### Owner amendment and parallel delivery

On 2026-09-18 the owner restored the existing Web `BrandLogo` at the left of the
header (language remains right) and required Welcome and setup to share the
same top-anchored title/subtitle geometry, including wrapped narrow copy. This
supersedes the earlier centered Welcome and no-logo statements. Both screens
use the same normal-flow layout and scroll on short windows. No host detection
or playback controls are added.

#2011 and #2012 now run in parallel from master `4019b704c`, in separate task
worktrees with one writer each. #2011 owns auth/install/runtime/API/Wizard and
onboarding, this plan and the shared plan's status consolidation. #2012 owns
Workbench/sidebar/Composer/General/Settings shell and `App.tsx` General wiring;
AuthGuard is unchanged. Global `index.css`, primitive additions and language
presentation belong to #2012; #2011 consumes existing public APIs and uses
scoped styles. Shared localization edits are disjoint (`onboarding`/auth/provider
versus `sharedWorkspace`/General/workbench/nav/layout), preserving sibling text.
#2013 native work remains deferred. No primary worktree, merge or service update.

The canonical exported consumer interface is `BackendConnectionState` and
`ApiContext.getBackendConnection(name: 'claude' | 'codex' | 'opencode'):
Promise<BackendConnectionState>`. Its fields are listed above. #2012 consumes
`ready === true` only to corroborate the transient completion banner, with real
Agent/default identity. `enabled` alone and `onboardingCompleted` authorize
nothing. Ordinary existing Workbench remains accessible when readiness expires.
No second auth/start/save/restart in that consumer. Integration verifies the
actual transition after #2011 merge; separate fixtures do not prove the seam.

First start does not require `setup_completed` in the service start owner.
Wizard therefore checks entry eligibility, explicitly starts a stopped service,
re-reads runtime application and real enabled Agents, preserves a usable default
(or selects an existing ready Agent if the seeded default is unusable), then
saves only `setup_completed=true` and navigates. A failed start leaves completion
false. The existing enabled-IM validator remains the final save boundary.

### Runtime-declared OpenCode callback mode (PM disposition, 2026-09-18)

`WebAuthFlow`, its start/status serializers and `OAuthWebStartResult` /
`OAuthWebStatus` add optional `callback_kind: 'code' | 'device' | 'redirect'`.
The producer maps OpenCode authorize `method: code` to `code`; its start-time
waiter retains the selected method index and prompt inputs. `auto` (and missing
method for compatibility) uses `device` when a device code exists, otherwise
`redirect`. Unknown explicit methods fail start. Claude/Codex can omit this
field; their existing callback/device contract is unchanged.

Manual-code flows arm the existing waiter at start. `_arm_flow_waiter` remains
the only deadline publisher; the waiter first awaits a flow-owned in-memory
`submitted_code: Future[str] | None`, then uses the remaining deadline for the
callback. An abandoned browser expires and releases provider admission without
polling. Explicit nonempty submit resolves the future once and never replaces
the waiter or deadline; cancellation owns that same task. These flows make no
callback request before submission, and send the code only to the existing
OpenCode callback endpoint. `OpenCodeServerManager.wait_provider_oauth` adds an
optional code argument; reserved method/code fields cannot be replaced by prompt
answers. The scope extension includes this one transport method and direct
transport tests, with the auth_setup runtime-provider scenario consuming it.
No backend readiness fields or parallel-lane interfaces change.

Installation jobs reconcile every refresh-capable backend, including Claude,
through the existing rolling-refresh owner after persisting the detected CLI
path. The job reports a failed application receipt as failure; a draining
receipt remains non-ready until the coordinator confirms application. A stopped
controller keeps apply-on-next-start semantics. Neither the frontend nor entry
adds another restart. Tests consume actual job/config/coordinator/readiness
owners with isolated non-ASCII paths and cover applied/draining/failed/stopped
states for all three backends.

The additive connection GET has an explicit Member management rule alongside
backend runtime/auth/install operations. Owner and Member can read it; Editor,
Viewer and unauthenticated remote callers cannot reach its readiness consumer.
Unknown routes retain the Owner default, supported backend validation remains
inside native dispatch, and the existing lower-tier read-role baseline is
unchanged. This corrects a CI-discovered omitted route classification without
changing credential write permissions.

The internal application projection also returns `controller_pid` from the
serving process. Failed UI apply receipts retain the already-existing runtime
owner PID. A registered backend in a confirmed *new* controller clears an old
failed delivery receipt because startup loaded persisted config; an unchanged
controller (including recovered IPC and a new browser page) cannot erase it.
This uses existing process identity only, with no generation/config-epoch scheme.
The public BackendConnectionState contract remains unchanged.

### OpenCode Agent/model recovery (PM disposition, 2026-09-18 03:29)

Backend readiness remains the frozen auth/application contract above. Explicit
completion separately checks accessible enabled Agent routing: Claude/Codex keep
existing usability rules; OpenCode Direct provider-qualified models require
that provider's effective API/OAuth auth. Catalog membership or configured=true
alone does not prove that auth. Hub uses its existing canonical model catalog
and supply owner, never Direct prefix parsing. Preserve a usable current default
and allow another usable backend to bypass an unrelated OpenCode mismatch.

When no usable Agent exists and an accessible manageable OpenCode Agent has the
Direct mismatch, expose an inline recovery using the existing model catalog,
Combobox and updateVibeAgent. Display the Agent and current model; require an
explicit compatible model selection and Apply. Do not choose a model, create an
Agent or infer untouched intent from builtin source/timestamps. Save only model
(and effort only if the catalog explicitly requires a compatibility change).
After save rerun fresh connection/default/completion checks. Display, cancel and
catalog reads never write. Unavailable/empty catalogs and failed saves retain
credentials, old persisted selection and the setup gate, with local retry.

Focused consumers cover Anthropic/Poe-only auth, seeded OpenAI mismatch,
explicit model persistence, cancellation/failure, preserved custom default and
another usable backend. A hermetic auth_setup scenario crosses the real Agent
store/default, OpenCode route resolution and narrow setup completion. Desktop
and narrow browser recovery checks extend the existing frozen dialog evidence;
no new dialog capture matrix or changes to #2012-owned picker files are needed.

### Actual-image review and targeted confirmation

The read-only observer viewed all 16 frozen final renders. PM confirmed compact
labels must use Credential type / API Key / Auth Token and generic provider key
labels; Settings retains its technical variable disclosure. Locked segmented
controls use whole-control opacity .6 once, with disabled radio semantics and
visible mint selection. Scoped focus-visible styling uses the existing ring
token. Light primary white-on-mint is the approved pairing; disabled opacity .4
and enabled state are verified separately, without a palette change.

Existing backend glyph/tile accent differences are inherited and outside scope.
Text-flow height differences (428 versus native 423/434; 536 versus 552) are
accepted. EN Light and narrow compose approved tokens/copy because no complete
authored counterparts exist. The frozen 16 captures stay unchanged; only small
corrected label/lock/focus/Light action crops and model recovery are added.

### Saved incomplete IM recovery (PM disposition, 2026-09-18 03:48)

Before explicit startup/completion, Wizard reads fresh config and its server
platform catalog. Existing platformHasRunnableConfig/credential_fields and
redacted has_* markers identify already-enabled incomplete adapters; WeChat
retains its runtime-waits-for-QR exception. Canonical enabled=[] has no IM step.
Only the explicit Repair saved messaging configuration action mounts the
existing embedded platform editor, extracted unchanged from Settings into a
shared switch. No AuthGuard, platform selection, enablement or routing changes.

Apply derives configChanges only within the affected config_key, preserving
other fields and concurrent edits. Existing form validation and masked-secret
semantics remain; no automatic auth test, second restart or setup_completed
write in the editor. Failure retains drafts, cancellation writes nothing. After
a successful repair the normal fresh completion checks run again. Generic
completion failures offer local retry rather than an unreachable Settings link.
AUTH-SETUP-120 covers real API preservation/validation; actual Wizard consumers
cover explicit mounting, narrow payloads, failure/cancel and canonical bypass.

### Verification record and observation limits

- Native source: 36 dialogs and 9 boards read/exported with explicit document
  identity. The observer viewed all 16 initial final renders, then all 9
  corrected crops; D1/D2/D4 resolved with no new defect. Frozen evidence is
  `/tmp/issue2011/final-render/` and `/tmp/issue2011/corrected-render/`; the latter
  includes model recovery at desktop/narrow. Separate narrow Slack recovery
  verifies existing-form reachability. No source design changes were made.
- AUTH-SETUP-119 crosses manual provider callback transport, test-owned native
  persistence, coordinator drain and ASGI application projection. 120 covers
  canonical no-IM and saved incomplete IM narrow repair, and actual Wizard
  consumers cover explicit repair/masked secrets/failure/cancel. 121 covers
  Anthropic/Poe provider auth, real Agent/default persistence, route resolution
  and completion. IDs were checked unique locally and absent on remote master.
- Existing auth regression, focused lifecycle/native-route/readiness tests,
  actual consuming UI tests and the 70-test onboarding browser suite pass.
  Additional focused browser checks verify corrected label bounds, focus ring,
  Light action opacity and locked selection, model recovery and legacy repair.
  Final gate logs are recorded in the PR report against the committed head.
- Fixtures own config/HOME/XDG/native paths and local upstream transports.
  Screenshot/UI mocks do not prove real provider OAuth or installation. No
  production credentials, native keychain, real CLI login, billable model call,
  local runtime restart, Incus, primary checkout or design mutation was used.
- Readiness observes native launch credentials and Avibe-owned application. It
  does not continuously validate upstream tokens or certify externally edited
  files were hot-applied. Upstream automatic OAuth already sent to a provider
  may commit externally; cancellation settles Avibe-owned work and prevents
  stale UI effects, rather than claiming remote transaction rollback.

### Review circuit breaker and complete boundary repair (2026-09-18 04:50)

Two genuine findings-bearing heads (`b664dd95d5`, `31766481c8`) exposed the same
OAuth lifetime class: the first omitted deadline enforcement, and its repair
used an API unavailable on supported Python 3.10. PM paused editing, inspected
all six threads and existing owners, and authorized these bounded repairs before
another push. No architecture/schema rewrite or owner escalation is required.

- The one start-time OAuth waiter uses Python-3.10-compatible `wait_for` for
  the input/server/callback exchange. Credential commit remains outside that
  timeout under the existing settlement shield. Actual isolated Python 3.10
  tests cover the same lifetime properties as the current-runtime suite.
- The coordinator recognizes intentionally absent Codex/OpenCode registration
  only when its loaded `AppCompatConfig` has that backend set to `None`, as
  `to_app_config` and successful unregister already specify. Missing config or
  unexpectedly missing enabled registration stays unavailable; pending/failed
  outcomes win. Claude remains registered and carries its enabled flag in its
  loaded compat config. Applied configuration does not imply enabled/readiness.
  The internal projection adds optional `disabled: true` only with confirmed
  applied disabled config. The public reader preserves unknown if disk now says
  enabled while the controller still reports disabled; this prevents mixing two
  observations into false readiness before enablement has actually applied.
  The public shape is unchanged. Settings confirms native credential readback
  plus applied/stopped state and reports saved-but-disabled without enabling it
  or presenting connected. Unknown IPC and failed receipts never count as saved
  application. The form's completion callback refreshes parent data only.
  Settings uses the existing Save/Saving labels and a muted, wrapping status for
  saved credentials on a currently disabled backend. The existing runtime hook
  increments `connectionRevision` after a config mutation settles (including
  failure); Claude/Codex parents pass it to the shared form. This only triggers
  fresh native/application reads, never readiness from an optimistic toggle.
  Status refresh preserves key, URL and method drafts without remounting. There
  is no duplicate enable control, event bus or polling; pending application
  retains the existing explicit refresh action.
- Discord's auxiliary guild selections use one small shared helper around the
  existing settings API, after narrow credential mutation. It preserves access
  management capability, explicit Discord platform and touched-empty semantics;
  partial failure retains the mounted selection and cannot complete setup.
- Only successful permission reads may add an OpenCode permission gate. The
  documented malformed-file fail-open exception does not grant permission or
  prove credentials/application, and the original file remains untouched.
