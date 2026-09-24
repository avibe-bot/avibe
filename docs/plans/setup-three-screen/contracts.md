# Setup three-screen — contracts C1–C6

Status: **PR0 interface proposal; D10 owner-ratified**, audited against head
`08b82658bbbf3e3e952fa1007c13f93fbca51410`. The handoff remains unchanged user source.
The later owner-approved setup implementation edits one Model Hub route per assistant,
using the existing `RouteChainDialog`; it has no shared route draft or cross-backend writer.
The approved `model-hub-native-takeover.md` supplies runtime/custody precedent; the
orchestrator adopts its automatic runtime preparation and the routine decisions in plan §10.
The owner reaffirmed on 2026-09-21 12:56 +08 that setup uses Model Hub; disabling it
belongs to Settings afterward. There is no Direct setup branch, including explicit opt-out.
At 13:19 +08 the owner ratified D10: setup delegates migration takeover to the existing
migration feature. C1/C6 supersede the handoff's copy-only, unchanged-connection and atomic-
rollback promises for this flow; they do not alter the source handoff.

C1–C6 specify the selected custody behavior and concrete technical mappings. D4/D9 was
orchestrator-ratified as a bounded technical interpretation; the later owner-approved
per-assistant route design supersedes its shared-route mapping below. D11 is also
orchestrator-ratified for its config transport boundary. C6 distinguishes controller-owned
startup recovery from browser observation and conditional retry. The original PR0 did not
authorize feature dispatch or extend #2065 merge authority to #2082; this document now
records the subsequently approved implementation boundary.

| Contract | Where it lives | Owner |
| --- | --- | --- |
| C1 copy | `docs/plans/setup-three-screen/copy-contract.json` | orchestrator |
| C2 flow interface | `ui/src/components/onboarding/setupFlow.ts` | orchestrator |
| C3 geometry and DOM hooks | this file §C3 | L1, consumed by L2/L3 |
| C4 entry gate | this file §C4 | L3 |
| C5 stable dialog frame | this file §C5 | L2 (add + import), L3 (route) |
| C6 data mapping | this file §C6 | L2 (providers), L3 (assistants) |

## C1 — copy

`copy-contract.json` is the whole statement: the leaves under `onboarding.flow`,
`onboarding.providers`, `onboarding.import`, `onboarding.setup` and `onboarding.route`,
plus the existing strings it changes or reuses (listed in the fixture metadata). It was validated
for zh/en key parity, complete `_one`/`_other` families on every `{{count}}` string, no
nested-before-literal key collision, and identical placeholder sets per key.

Copy ownership and current implementation constraints:

1. **PR0 adds no key to the bundles.** `keyCoverage.test.ts` reads the literal keys out of
   the app source, so a key lands with the component that uses it, in the lane that owns
   both. Namespaces are the ownership boundary: L1 takes `onboarding.flow.*` and the four
   changed `onboarding.welcome.*` / `onboarding.access.*` strings, L2 takes
   `onboarding.providers.*` and `onboarding.import.*`, and L3 takes the new
   `onboarding.setup.*` and `onboarding.route.*` leaves plus the two changed
   `onboarding.setup.title` / `.subtitle` headings. `_changed_existing` carries an `owner`
   per row, and that field — not this sentence — settles a dispute.
2. **Migration takeover wording is selected by owner D10 (2026-09-21 13:19 +08).**
   `_migration_copy` records the selected copy and behavior. It supersedes copy-only,
   unchanged-connection and atomic-rollback promises for this flow; the source handoff stays
   unchanged. The existing migration feature presents the exact approved Chinese consequence
   and **Not now / Start migration**; only its explicit confirmation applies credentials.
   Setup supplies discovery/selection context and consumes confirmed results. Its copy scope
   and controlled selection need a minimal adaptation of `MigrationDialog`, not a separate
   migration coordinator. Settings keeps its existing rendering and broader migration scope.
   Counts describe migration items; an `applied` receipt alone does not establish readiness.
3. **The plural-family rule is local to the new keys.** 66 existing `{{count}}` strings
   outside `settings.models` ship without families, so a bundle-wide redline would fail
   today; this round holds the new keys to it instead of retrofitting the app.

## C2 — flow interface

`setupFlow.ts` declares the screen ids, the handoff target, the action feed
(`SetupAction`), the imperative handle (`SetupScreenHandle.activate`), the screen props
and the shell-owned `SetupFlowState`.

- The shell passes `flowState` and its React `setFlowState` setter to every screen.
  Consumers use `setFlowState(previous => ({ ...previous, changedField }))`, including
  asynchronous completions. Replacing captured state loses other screens' intervening work.
  `providerSelection` holds the complete ephemeral `MigrationScan` and selected backend
  consent groups shared with the existing migration owner, not an apply/recovery journal.
  The shell does not own model-route drafts; persisted routes are read from Model Hub.
- The shell renders the single primary and Back actions. Each `onActionChange` and
  `onNavigate` callback is bound to the emitting screen and activation; the shell rejects
  inactive/stale publications and clicks only the current `SetupScreenHandle.activate()`.
  Clear the previous action on navigation; disable it during capability reads, handoffs,
  and pending mutations. `busy` also disables clicking. `activate()` must reject re-entry.
- `SetupAction.labelKey` names a shipped C1 string, in one of the two forms C1 actually
  ships: a text leaf that resolves on its own, or the base of a `_one`/`_other` family
  together with a required `labelArgs.count`. A family does not resolve without a count, so
  the count is part of the claim rather than something the shell supplies on faith, and
  `TranslationKey` alone — deliberately the leaves that resolve without count — cannot name
  a counted label at all. The two forms are derived from the live bundle, so a string that
  has not shipped stays unnameable either way. Field names and the publication protocol are
  unchanged: a screen publishes `labelKey`/`labelArgs` and the shell resolves them in one
  `t` call, with no narrowing.
- All registered screens remain mounted, `hidden` + `inert` when inactive. This preserves
  drafts but does NOT suspend effects. Screens gate polling, authorization, scans and
  animations on `active` (and dialog open state). A hidden screen must not start installs,
  mode changes or other mutations. Already-started operations retain one owner; their
  completion may reconcile state with functional updates, but cannot navigate, steal focus
  or publish the active action. Ignore stale read completions using an activation/request
  generation, not unmount cleanup alone. Leaving and returning never replays a mutation.
- Server facts (CLI presence, backend enablement, sources and routes) are re-read on active
  entry and after mutations. C4/C6 require the existing Agent collection authority to deep-refresh
  cached CLI presence after install/path/config changes and before completion; ordinary list
  reads alone cannot establish that freshness. Preserve unconfirmed provider drafts across those reads;
  C6 defines scan reconciliation and the per-card route read boundary. `setupFlow.test.ts` consumes the actual
  props with React state and checks navigation plus a late update, not just initial values.

`setupCapability(boolean | null)` maps the shell's validated config capability boolean to
`pending | enabled | disabled`. Validate `capabilities.model_hub.enabled` before using
`modelHubEnabledFromConfig`; missing data and request errors are pending, never disabled.
`useModelHubCapability()` collapses failure to false and is not a safe producer here.
`setupNavigationReady(capability,gatewayEnabled)` requires enabled deployment capability
and fresh saved `model_hub.enabled === true`. Missing/malformed saved intent stays null,
not implicitly true; these are separate config fields. Initial config
loading stays read-only; entering active providers starts D11, never merely mounting intro.

**Setup has one fixed sequence, `SETUP_SCREENS`: intro → providers → assistants.** Capability,
installation support and errors never remove providers or create a Direct branch. Back uses
`setupBackTarget(SETUP_SCREENS,current)` and keeps drafts. Host support is learned after
D11 bootstrap on active provider entry; controller recovery already enforces server admission
before the browser can read support. Do not require an RPC before reaching its bootstrap path.
Unsupported/error holds the current step. Clear obsolete action publications on navigation;
only the active owner may retry, claim the CTA or navigate.

An authoritative disabled deployment capability **or saved runtime intent** fails setup's Hub prerequisite. Show
`onboarding.flow.gatewayRequired`, the current configuration/recovery boundary and Retry;
keep the current step and Back if applicable. Retry re-reads config first; while capability
or saved runtime intent remains disabled/unknown, do not seed config, start a controller, call Hub RPCs, install, switch mode
or complete. The user must restore the existing configuration through its existing management
owner before resuming. There is no new setup toggle or implied Settings route exemption:
`ModelHubCapabilityGate` redirects when disabled and `App.tsx` restricts incomplete setup
navigation. Preserve those boundaries rather than linking to a recovery page that cannot
be entered. Completed instances' post-setup Settings choices remain outside this contract;
manual setup re-entry cannot silently re-enable or reset them.

The shell passes **`capability`, `gatewayEnabled: boolean | null` and
`runtimeRead: RegionRead<RuntimeDependency>`** through
`SetupScreenProps`, plus `onRetrySetup` for its existing config/bootstrap/runtime owner.
The retry first re-reads config; pending/error config reads hold capability pending, and only
fresh enabled capability and saved intent may resume bootstrap/runtime work. This
reuses the shipped read shape without a parallel setup policy/state machine. `readyRegion`
requires a successful validated response; null/malformed/HTTP/transport errors remain
loading/unread. `beginRegionRead`/`failRegionRead` retain old display data while refreshing or
failed, not authorization. Ignore stale request/activation completions. No hidden screen
starts a retry, installation or other mutation.

Three small predicates define distinct necessary conditions, not complete readiness:

- `setupCanAttemptInstall(capability,gatewayEnabled,runtimeRead)` requires enabled capability, a fresh read
  and `runtimeCanAttemptInstall`, plus saved enabled intent. Manifest `unsupported` blocks installation; `unresolved`
  is admitted. Neither browser platform guesses nor a 5xx determines host support.
- `setupHubRunning(runtimeRead)` requires fresh `ok`/`degraded` health independently of
  installation admission. Provider Continue requires this plus its action-specific guards;
  an existing healthy Hub remains usable on an install-unsupported host.
- `setupCandidateAllowed(capability,gatewayEnabled,runtimeRead,mode)` admits only persisted Hub candidates
  with a running runtime and enabled capability. Direct never passes setup, even with valid
  native credentials. C4 still requires the same candidate's actual route/auth/application/
  permission evidence.
  No helper changes mode, custody or the selected Agent.

A fresh runtime payload with `enabled === false` also blocks these gates even if an older
config read said true. Its optional field may be absent on older payloads, so fresh validated
config remains required. `runtime_start` writes saved `enabled = true`; therefore configuration
recheck must precede any browser-requested ensure/start, not just installation. Controller
startup independently re-reads persisted intent through its existing recovery owner (C6). Never turn a deliberate
stop into a start while recovering setup.

Unsupported/non-running holds Continue and completion on the same flow with C1's truthful
unsupported notice, **Recheck/Retry** (`common.retry`) and **View installation guide**
(`onboarding.providers.gatewayEnvironmentHelp`). The guide action opens the existing
[installation guide](https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI.md)
(Chinese counterpart `INSTALL_FOR_AI_ZH.md`), explaining how to use a supported environment;
it does not migrate this instance, promise platform availability or offer a Direct bypass.
The runtime response remains authoritative about current installation support. Network/read
errors show the existing runtime-unread copy with retry, not the unsupported notice.

The active owner may publish Retry as the shared CTA while Continue is held; do not apply
Continue's readiness gate to Retry or Back. Retry re-reads first, holds writes while pending,
then resumes the same flow on fresh support/running evidence. If support becomes admitted
but the runtime is stopped/missing, resume C6's automatic ensure path and read back running
health before continuing. Repeated unsupported responses keep the guidance and require an
explicit recheck; no automatic impossible-install loop. A later failure on assistants keeps
that screen's drafts and recovery/Back action. A running Hub needs no install to continue.

## C3 — geometry and DOM hooks

**Task-specific design provenance.** The owner-supplied handoff §12 selects
`../avibe-docs/design_desktop.pen` and Show session `ses36vg559de2` for these setup frames.
That task-specific direction overrides the repository AGENTS default `design.pen` for this
work only. Do not replace/copy either canvas or change the repository-wide rule. The handoff
remains immutable. PR0 has not inspected native Pencil frames: L1–L3 must inspect/export the
named frames and verify their token/geometry mapping before UI implementation and fidelity
claims. Source literals are reference measurements, not CSS prescriptions.

**Anchor pair.** One `.onboarding-setup-footer` rendered by the shell, containing one
`.onboarding-action-w.onboarding-primary-action` and, below it, one
`.onboarding-action-w.onboarding-back-action`. Their boxes are identical on every screen,
in both languages, at every viewport; `geometry.spec.ts` already asserts that across two
screens and L1 extends the same assertion to three. The Back button keeps its box on
screen 1 by staying invisible (`visibility: hidden`), which is how the reference holds the
anchor without a layout shift.

**Shared reservation.** `.onboarding-stage` remains the box both the story and the screens
below it occupy, so the anchor's vertical position does not depend on what a screen draws.
Screen 2's migration offer uses the shell's primary action and a controlled review dialog;
dismissing an offer changes neither the stage nor the action anchor.

**Snapshot hooks.** The handoff reads live DOM across screens, so these class names are a
contract, not styling. L1's `setupHandoff.ts` queries them; L2 and L3 must render them.
An inactive `hidden` root has no measurable box. At handoff start, the incoming screen
becomes the displayed root: its heading and diagram or summary appear immediately, as in
Show session `ses36vg559de2`'s `begin` and `chooseAssistants`. Only the incoming destination
cards stay visually hidden while the cloned outgoing cards fly into place (about 900 ms).
The incoming root remains inert and its effects and action feed inactive until landing.
After cancellation, restore the inactive root's `hidden` state; the settled state still
has exactly one active screen. L1's geometry test must exercise this real path, not
measurements taken with every screen already visible.

| Transition | Reads from | Lands on |
| --- | --- | --- |
| intro → providers | `.onboarding-collaboration-card`, with `.onboarding-card-logo` and `.onboarding-card-name` inside it | `.setup-destination`, with `.setup-destination-logo` and `.setup-destination-name` |
| providers → assistants | `.setup-destination` and its two children, plus the retiring `.setup-provider-stage` | `.onboarding-assistant`, with `.onboarding-card-logo` and `.onboarding-card-name` |

Screen 2's own roots, in the same order the stage draws them: `.setup-provider-stage`,
`.setup-provider-grid`, `.setup-provider-card` (`data-provider` = vendor id, `aria-pressed`
when selectable), `.setup-wires--inbound`, `.setup-gateway`, `.setup-wires--outbound`,
`.setup-destinations`. Screen 3 keeps `.onboarding-assistants`, `.onboarding-assistant`,
`.onboarding-card-identity`, `.onboarding-assistant-state`, `.onboarding-assistant-body`.

**Tiers.** `onboarding.css` expresses the four desktop readings and the phone band as
`--ob-*` custom properties on `.onboarding-shell` under a fixed set of media queries
(`max-height:800`, `min-height:1001`, `min-width:1600 and min-height:950`,
`max-width:1023`, `max-width:759`, `prefers-reduced-motion`). L1 owns that file and adds
the tier variables screens 2 and 3 need from handoff §9. A screen stylesheet
(`onboarding-providers.css`, `onboarding-assistants.css`) consumes `--ob-*`, may restate a
value inside the SAME bands scoped to its own root, and may not invent a new band. No new
literal colors anywhere: values come from the `index.css` token scale or from `--ob-*`, and
`validate:theme` / `glowScale.test.mjs` keep that true. Active/hover card shadow uses
`var(--ob-active-shadow)`, already backed by `--shadow-glow-onboarding-mint`; its theme-specific
values stay in the shared tokens. Hover has no translate or scale.

**Motion bail-out.** Under `prefers-reduced-motion`, a hidden page, or an explicit pause the
handoff is skipped and the screen changes instantly; on phones the view scrolls to top
before the change. The primary action stays disabled and unmoved for the whole transition.

## C4 — entry gate

L3 owns the code; this is the semantics it must implement, and the semantics
`WizardCompletion.test.tsx` is re-pinned to.

**One assistant must satisfy the whole gate.** Three independent existential checks can be
met by three unrelated objects — an enabled Codex with no route, a persisted route on an
uninstalled Claude, a healthy source that neither route uses — and setup would then complete
on a machine whose first workspace turn cannot run. The gate is therefore correlated over a
single assistant, and it reads the server rather than any client-side draft.

With Model Hub capability and saved runtime intent enabled (the setup prerequisite), L3
builds the available set from fresh
`listVibeAgents({ cache: false })`: enabled, non-archived named Agents, joined by **backend
and Agent name** to backend connection/CLI reads and `AgentSupply.named_agents`.

| Condition for the same candidate | Authoritative read / rule |
| --- | --- |
| installed and backend enabled | `api.detectCli(cli_path)`, freshly corroborated `AgentSupply.cli_present` through `createAgentCollectionReadAuthority.refresh()` and `getBackendConnection(backend).enabled`; stale/failed refresh or read cannot prove readiness |
| Hub runtime running | `getRuntimeStatus()` with health `ok` or `degraded`; mode alone is not runtime readiness |
| model and route runnable | backend mode `hub`; that candidate's `named_agents` row has nonempty `effective_model_id`, `supply_status` in `ok` / `degraded`, and no `route_unconfigured` reason. `waiting`, `interrupted`, null or absent rows do not pass |
| application and custody | `getBackendConnection.ready === true` (including permission and matching credential ownership) and `application === 'applied'` are required alongside this candidate's route. Confirmed `stopped` permits bootstrap/recovery, never workspace readiness; draining/failed/unknown cannot enable entry |

Do not read backend-level `selected_model_id` to reject a different candidate: it describes
only `selected_by_agent`. The runnable-hop owner is the server resolver; neither an unrelated
healthy source nor a card's displayed route proves readiness. Revalidate on the completion click.

**CLI freshness owner.** `Controller` caches CLI presence; an ordinary `listAgents()` cannot
refresh it after installation. Use one shared `createAgentCollectionReadAuthority(modelsApi)`
for all setup supply reads. On active assistant entry, after install/CLI-path/agent-config
changes, and before corroborating completion, await `refresh()`: it calls
`refreshAgentPresence()` (`?refresh_cli_presence=1`) then a generation-controlled list read.
Consume only its `{kind: 'current', value}` result, not the raw refresh endpoint payload.
A concurrent newer generation can supersede that list read; hold completion and use the
same authority's current reconciliation/retry result. Failed or stale results retain display
only and keep Retry reachable. Invalidate superseded reads when a relevant mutation starts;
CLI detection success alone cannot release the gate. Re-read connection/config and join fresh
named-Agent supply by backend/name; this presence refresh is not authentication or route proof.

D11 establishes config persistence and controller availability on active provider entry
**before** Hub-dependent screen-2 work (C6). Enabled capability is a prerequisite; no bootstrap
expansion is needed for an excluded Direct branch.
`ModelHubRemoteService` uses the controller socket; `engine_down` on a missing socket is
neither an unsupported manifest nor proof of a bad model route. If the controller stops
later, retain a reachable start/retry action using confirmed application `stopped`, bootstrap
it, let controller-owned recovery run, and re-read runtime, connection and candidate supply
before allowing completion. Reuse recovered health; do not send a second unconditional
ensure/start. Startup
has no assistant/source readiness prerequisite. Do not restart after an enable/config write.

Preserve `default_agent_name` if it remains in the available set, including a usable custom
Agent outside the cards' designated builtin Agents. Otherwise prefer a runnable designated
builtin Agent in card order, then an available named Agent in stable backend/name order; call
`setDefaultVibeAgent` and verify a fresh Agent read. Write `setup_completed` last through
`api.mutateConfig` with an explicit field mutation, then validate a fresh uncached
`apiFetch('/api/config', {cache:'no-store'})` readback before navigating. Use that same
read/parse path after an unknown/failed write; cached pre-write data cannot settle it.
Unknown writes are reconciled by reads, not treated as success or blindly retried. Existing
invalid IM configuration remains saved for later repair in Settings and does not gate Model
Hub setup completion. `SetupModelRecovery` is the shipped **Direct OpenCode**
recovery; it does not repair Hub models. A missing Hub Agent model is repaired in existing
Agent/Model Hub settings, not chosen or written by Setup.

The new Hub route predicate replaces credential readiness / `entry_eligible` as the *route*
criterion, not the confirmed application-state or backend permission guard. Today's `Wizard.complete()` only
filters OpenCode routes; the all-backend named-Agent join is new L3 orchestration, not a
helper that already exists. Cards with a confirmed Hub model can edit its route even when
the chain is empty; cards without a model show a hint instead of creating a route target.

**Known by design — setup requires Model Hub.** Owner decision, 2026-09-21 12:56 +08:
“setup的契约是默认使用模型网关”; users who later need to disable it do so in Settings.
Unsupported installation without a usable running Hub cannot complete setup, even with
working Direct credentials. Explicitly disabled gateway configuration also does not create
a Direct setup branch: preserve that saved choice and expose the C2 configuration/recovery
boundary. No silent enabling, custody rewrite or setup-completion write bypasses it.
A fresh healthy Hub still uses the correlated gate above regardless of install admission.

This is the intentional non-change for `PRRT_kwDOPbFPYs6kO76r` / `4059240601` and
`PRRT_kwDOPbFPYs6kPKSx` / `4059328091`: neither unsupported-host Direct fallback nor a bootstrap
expansion for disabled-gateway setup belongs to the product journey. D11 still supports the
normal Hub setup. Existing Settings/backend Direct support and completed-instance preferences
remain untouched. This specific owner-ratified boundary is **not a Codex gate waiver**:
unavailable retry/Back, unsafe installation, lost drafts and false readiness in the normal
Hub flow remain defects. Current-head review, unresolved-thread and CI gates still apply.

## C5 — stable dialog frame

Applies to the three screen-2 dialogs. Screen 3 reuses Model Hub's existing
`RouteChainDialog` and its own focus, layout, guard and save behavior.

- One frame per dialog across every tab and state: 568 wide, 24 padding, 16 radius, 20
  gaps, and a common height taken from that dialog's longer ordinary form (not from an
  unusually long error), bounded by the viewport.
- Anchored: title, description, method tabs, close control, action footer. Scrolling: the
  middle region only — long errors, provider lists and authorization notes scroll inside
  it, and every action stays reachable at browser zoom and on a short screen.
- Loading, tab switching, authorization progress and error all reuse the same frame; a tab
  switch preserves drafts and focus. A hidden pane keeps no focusable control and starts no
  duplicate authorization effect.
- Focus returns to the element that opened the dialog; Escape and an outside click close it,
  except that the import dialog is not closable while a batch is applying.
- Phones: the frame accounts for the keyboard and the safe area. No single absolute height
  is applied app-wide.
- Acceptance is the repeated-switch test at one viewport — heading, tabs, frame and footer
  do not move while the middle scrolls — not a height transition.

## C6 — data mapping

Field names are exact. Every row names its producer and its consumer, because a shape-only
contract passes both sides' unit tests while the behavior silently disagrees.

Each row distinguishes a shipped owner from required setup orchestration. Reuse an owner
where its actual inputs and outputs fit; controlled state, support preflight and per-assistant
route reads cannot be attributed to helpers that lack them. A lane reports contradictions instead
of silently changing either the handoff or another owner.

### Screen 2 — providers

| Element | Producer → consumer and rule |
| --- | --- |
| scan and discovery | existing migration feature `modelsApi.scanMigration().items` → full `providerSelection.scan`; `isImportableKey` identifies entry-point keys only. Keep every row; reuse its group closure/blockers before the primary-action count; a blocked detected key remains visible with its reason but is not advertised as importable |
| candidate identity | `MigrationItem.id` is selection identity, never vendor alone. Use `providerLabel` / `providerVendorId` as `MigrationDialog.ItemRow` does. Optional vendor/name/mask may be absent: generic mark plus `masked_detail`, never infer vendor from backend or fabricate a mask. Show every `source_paths` locator; only when absent use `migration.source.<backend>`. `notes_key` is an explanation, not origin |
| cards | slots 1–2 show detected/provider identities then existing sources, with OpenAI/Anthropic placeholders if needed; slot 3 is Add more. Vendor grouping is presentation only: multiple keys from one vendor retain separate IDs and may require whole backend selection |
| selected and connected | selected means complete consent group selected; a blocked group is disabled with its reason. Added/connected treatment follows confirmed source IDs from `listSources` and status `active` / `standby`; `verification_pending` is disclosed as unverified, not rejected as unusable (the shipped resolver accepts standby). One vendor's healthy source does not prove every key/card connected or a runnable assistant |
| counts | Primary-action count = deduplicated appliable item IDs in selected complete groups; summary provider count = distinct displayed providers of those items. There is no independent capsule count. `importedCount` = confirmed `result.applied`, once per successful submitted batch, not number of sources or guessed key count. `addedThroughMore` = unique IDs returned by successful manual creation, badge filtered against current usable sources |
| migration offer | The primary action opens the controlled `MigrationDialog` for selected complete backend groups. `modelHubMigrationDismiss` persists declined `MigrationItem` identities; a newly discovered key remains visible for review, but its group is never automatically selected if another required item was declined. Only explicit consent to the complete group clears those identities. No `ImportKeysNotice` or reserved slot is mounted |
| import / Detected tab | use the complete-group rule below. Hiding card duplicates or out-of-scope rows must never shrink consent. Already-added status follows fresh scan/source evidence, not a vendor-name match. D10 delegates confirmation/apply/recovery to the existing migration feature; setup never applies from selection or normal navigation |
| add — API key | shared `AddApiKeyDialog` form: one create carrying `save_unverified: true` (`apiKeySourceDraft.ts`), no observe or probe first — the credential is saved on the person's word and verified afterwards, so a provider that is slow or briefly down does not cost them the key they pasted. Preserve its existing unknown-write recovery exactly: only a server-named non-409 4xx settles the write (`apiKeyWriteSettled`), anything else reconciles against `client_nonce` through `reconcileUnknownWrite` rather than repeating the create. Read `SourceCreated.source` and placement tails (`added_to`, `adopted_by`); refresh sources and affected supplies before reporting ready, and a source that comes back `verification_pending` is shown as saved-but-unverified per the `selected and connected` row — never as a completed verification |
| add — subscription | reuse `subscriptionOptions.ts` / `OAuthConnectDialog` / `OAuthFlowParts` for the offered product vendors, custody choice and flow ownership. Setup limits the shipped vendor list to OpenAI/Anthropic per handoff; there is no callable vendor-capability-list API in `ModelsApi`, and no such producer should be invented. `getOAuthStatus` / `submitOAuth` return `OAuthResult`; terminal create carries `created.source` and placement tails. Do not create the source a second time or equate terminal OAuth with assistant readiness |
| picker order | `apiKeyVendors.ts` reads `vibe/data/api_key_vendors.json`; first eight in file order, remainder plus custom. No browser re-sort or prototype Cohere |
| runtime | with enabled capability and saved intent, D11 starts only a confirmed stopped controller; its existing recovery owns initial ensure/start and server admission. The browser then observes health and only requests admitted recovery if still needed (D3). Healthy-engine writes follow the operation table below, independently of new-install permission. Unsupported/non-running or unread stays on the normal flow with retry/guide/Back; no install-confirmation or automatic credential takeover |
| first entry | cards → inbound wires → gateway → outbound wires → destinations, about 1.1s; C3 bail-outs; no production reset/replay control |

#### Complete consent groups (current thread `PRRT_kwDOPbFPYs6kOVg6`)

The shipped owner is `MigrationDialog.tsx`: `eligible` scopes entry-point **backends**; it
does not filter the scan before grouping. Starting at those backends, repeatedly union
`required_backends` from **every row** of every included backend until stable. Missing/empty
metadata means that backend alone. Retain every row of the resulting closure, including
OAuth, `reauth`, `keep_native`, non-key rows and blockers, and display required backend/file
impact. A non-`import` row blocks its entire linked group. Toggling any member selects or
deselects the full linked import group; never submit a partial backend.

C2 stores `{ scan: MigrationScan | null, selectedBackends: AgentBackend[] }`, where the
backend list is the union of complete unblocked groups. The adapted migration owner derives
apply IDs from the **same scan**: all `proposed_action === 'import'` rows whose backend is
selected, once per ID, and validates closure/blockers again before its confirmed apply. With
setup's API-key-only requirement, an otherwise importable OAuth member also blocks the group
for this entry point; show it and explain that the full migration is available in Settings.
Never silently import OAuth or drop it from a batch.

The controlled `MigrationDialog`, discovery cards and Detected tab consume the same scan
and complete-group selection. Setup supplies the key-only eligibility and write admission;
Settings keeps its broader migration scope. Keep one grouping/confirmation/apply/recovery owner: no
independent item-ID selection engine, setup-specific migration coordinator or retry engine.
Rescans invalidate old item IDs: preserve selection only for unchanged complete groups;
changed membership/custody requires renewed review, and failed rescans disable apply rather
than submitting stale IDs. Default-selection applies only to fully importable, unblocked
initial groups, respecting the server's `selected` flags.

The migration owner publishes confirmed results; setup clears confirmed group selection,
increments the receipt-derived batch count once, then refreshes scan, sources and supplies.
`applied` counts migration items, not assistant readiness. Today's `onApplied(number)` carries
a count only, and `onApplied(0)` is also called for `migration_credentials_invalid` to refresh
the caller. Treat zero as refresh-only, not proof of completed takeover; preserve the owner's
error/recovery UI. Any receipt distinction needed by setup belongs in the same small owner
adaptation, not guessed from local selection. Unknown transport outcomes remain with that
feature's reconciliation/recovery; the server journal and same item IDs can resume/replay the
operation. Setup neither retries apply itself nor fabricates a rollback or partial success.

#### Migration takeover — D10 (owner-ratified 2026-09-21 13:19 +08)

Owner decision: “setup只负责默认使用模型网关，不负责迁移，采用 迁移接管 实现”. Setup owns the
Hub journey, discovery/entry presentation, assistant readiness and receipt-derived display
state. The existing migration feature retains migration behavior through this call chain:

`MigrationDialog` / shared grouping → `modelsApi.scanMigration` / `applyMigration` →
service `migration_scan` / `migration_apply` → `apply_native_migration` and its journal.

The backend owner provisions sources, removes replaced native credentials/direct-routing
fields, commits Hub mode/placement, and owns transaction/recovery. Tests prove global/project
key cleanup, preservation of unrelated MCP/name settings, and retained custody for recovery
after exposure. Setup must not duplicate cleanup, custody writes, mode transition, rollback
or retry machinery, and must not send a second mode PATCH after successful apply.

Cards, the primary action and Detected actions delegate to this feature's complete-group review. Keep
every required backend/file visible and show the exact consequence
`迁移后，CLI的认证信息将完全交由模型网关管理`, with **Not now / Start migration**. Only the latter
explicit confirmation invokes apply. New/incomplete takeover invokes server dependency ensure
before credential withdrawal and again before sync/start; it retains installation admission
even when a healthy engine exists (operation table below). Selection, setup entry, gateway installation/start and
normal navigation are never consent. Not now leaves native authentication and mode unchanged.
Setup's entry remains API-key-only: mixed OAuth or non-importable groups are blocked as a
whole, with the broader migration feature remaining in Settings. Unrelated settings stay
preserved; source/assistant readiness is re-read after the feature's confirmed result.

This selected behavior expressly supersedes the handoff's copy-only, unchanged-assistant-
connections and atomic-rollback promises for this flow. Success reports the migration owner's
completed takeover/cleanup; failure or unknown outcome requires its reconciliation/recovery,
which can retain changed credential custody. C1's selected copy describes those consequences.
No credential-copy alternative or new native-coexistence design is part of the implementation.
The original handoff remains immutable, and D10 adoption is not permission to migrate real
credentials during PR0 or to start feature lanes.

#### Controller bootstrap — D11 (orchestrator-ratified technical sequence)

L1's existing setup shell owns one bootstrap operation on active provider entry with enabled
capability and saved runtime intent, before Hub RPCs. A hidden screen cannot start it. If a fresh config read reports
either capability or saved intent disabled/unknown, stop at C2's prerequisite boundary without changing that preference.
**Do not call `api.mutateConfig([])`:**
`configMutationsToPayload` rejects empty mutations before any HTTP request. Keep that general
validation intact. The smallest setup-only composition uses the existing CSRF-aware
`apiFetch` directly; no new API service or production helper is introduced by PR0:

```ts
const response = await apiFetch('/api/config', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({}),
});
// Validate HTTP status and parse the POST body as described below before proceeding.
const readback = await apiFetch('/api/config', { cache: 'no-store' });
// Validate HTTP status and parse the fresh GET body independently.
```

`apiFetch` supplies Accept/CSRF and its existing remote-auth handling. It returns a Response;
it does not validate HTTP success or the body for the caller. POST and GET success return a
**top-level config object**, not `{ok:true,config:...}`. Validate a JSON object with no error
or `ok:false`, `version === 'v2'`, boolean `setup_completed` and
`capabilities.model_hub.enabled` and saved `model_hub.enabled`, string `platforms.primary`, string-array
`platforms.enabled`, and object `runtime`/`agents` before consuming these fields. Preserve
load-recovery warnings for the existing recovery path. An absent/malformed required field
is unread state, not an explicit capability opt-out.

A non-2xx POST or explicit error body is not success: retain HTTP status and server error
(string or structured object) for the active owner's error UI. A definitive refusal blocks
startup. A transport failure, invalid/truncated 2xx body or server 5xx has an unknown write
outcome; do not blindly repeat POST. Both acknowledged success and unknown outcome take the
same uncached GET/JSON validation path above (a failed read stays blocked). Error responses
may also be read back for diagnosis, but that read never clears a definitive refusal.
Crucially, GET serves an **in-memory default when no config file exists**. Therefore a valid
GET alone cannot settle unknown persistence: subsequently require a successful
`getBackendConnection` response (`ok:true`, valid application state), whose server owner
calls `load_config()`. If it fails or remains unknown, keep bootstrap pending/recoverable;
never let `control('start')` fall back to creating the legacy config. Once that read proves
config existence, the empty patch's only intended effect is reconciled; a later explicit
retry can reseed an actually absent config without overwriting user fields.

A direct `apiFetch` POST **does not clear ApiContext's 30-second config cache or emit
`onConfigChanged`/configuration convergence**. L1 explicitly installs the validated fresh
GET into the shell's existing server-config state (`data` in today's Wizard), scoped to the
current request/activation. Feed capability and bootstrap decisions from
that snapshot. Future authoritative setup reads use the same direct uncached read/parse
path, including unknown-write reconciliation; do not replace it with cached `api.getConfig()`
or assume a global notification occurred. C2's draft state/setter remains unchanged.

The server empty patch seeds missing config through
`core.services.settings.default_config()` and lock-merges existing state, preserving
`setup_completed`, platforms and other user choices. It must precede connection reads,
which require a config file. `runtime.ensure_config()` alone still seeds legacy Slack
defaults; the settings API seeds workbench-only defaults (`platforms.enabled=[]`, primary
`avibe`, setup incomplete). No stale full-config round trip or reset is needed.

After verified config, a fresh connection with confirmed `application === 'stopped'` permits
`control('start')` from `useStatus`; an applied controller is reused. Draining/failed/unknown
uses existing recovery/read paths, never an inferred restart. The control consumer checks
its HTTP rejection and returned body `ok === true`/`action === 'start'`, then re-reads backend
application and controller RPC availability. Controller startup itself calls
`_recover_runtime_owners()` → real `ModelHubService.recover_runtime_intent()` →
`_prepare_engine_for_demand()` → adapter ensure and start for saved enabled intent. This can
install/start Hub before the browser's first runtime RPC; the browser does not own or precede
that recovery. `EngineRuntimeManager` / `ManagedRuntimeManager.ensure` resolves host archive
admission before fetching/installing runtime assets. Unsupported rejects there; optional Hub
recovery failure is caught by Controller, which continues other owners and retains blocked
Hub readiness. Manifest lookup may occur; no claim of zero filesystem bookkeeping or network
manifest resolution is made. Disabled intent is not enabled by recovery; previously confirmed
migration journals and orphan install claims retain their existing recovery owners.

After controller readiness, read the recovery outcome. A fresh healthy Hub needs no browser
ensure/start/restart. If recovery failed or health remains stopped/missing, C6 below governs
an explicit/active recovery attempt using fresh config and runtime evidence. A `down` status
can be the service's projection of failed demand, not proof of installed bytes. StatusProvider's
`refreshStatus()` only refreshes service status and may return null; neither it nor a cached
status alone proves config persistence or backend readiness. Missing CLI, sources, routes
and incomplete setup do not forbid controller startup. Saved incomplete platform
configuration remains intact for repair in Settings; it does not block Model Hub setup
completion. Do not disable configured transports or restart after an already-reconciled
config write.

Evidence is separated deliberately. The temporary frontend consumer imports the **actual
ApiProvider, serializer and apiFetch** with mocked HTTP: empty mutations reject before POST;
setup POST sends `{}` with JSON/CSRF; fresh GET bypasses both the primed ApiProvider cache and
HTTP cache; direct POST leaves cached config and convergence subscribers untouched; refusal,
unknown POST and invalid GET stay distinguishable. Separate hermetic backend fixtures drive
real config POST/GET, persistence and connection/control handlers with a fake process launch
and the existing real-loop readiness harness. They prove fresh seed, existing-state
preservation and control-handler startup without sources or completed setup; their fake
process did not prove actual Controller runtime recovery. Round-8 external composed tests
separately invoke the real Controller recovery method, service, adapter, installer and
supervisor with test-owned archives/storage and synthetic process/health peers: unsupported
refuses artifact installation/start while other owners recover, supported default-on restores,
disabled intent stays disabled, and later status reads do not repeat completed recovery.
`runtime_status` can reconcile an orphan installation claim through the existing owner; it
is not universally side-effect-free. These are separate transport/server/composed proofs,
**not browser/server E2E**, full Controller construction or a shipped setup implementation.

#### Observe recovered runtime, then prepare adoption

0. D11 establishes controller availability; server-owned auto-recovery has already attempted
   saved intent with installer admission. A missing socket/read error is not an unsupported
   manifest. Preserve pending/error and Retry/Back; do not manufacture readiness.
1. The active owner obtains fresh config intent and `getRuntimeStatus()`. If health is already
   `ok`/`degraded`, reuse it regardless of manifest install admission. Ordinary source/route/
   OAuth/mode work follows the table below and each existing owner's health, custody and
   consent guards. Completion still uses C4; unsupported never creates Direct setup.
2. If engine recovery is still needed, require `setupCanAttemptInstall` / authoritative
   `runtimeCanAttemptInstall` before browser ensure/install/start or helpers that may invoke
   them. `unresolved` is admitted; unsupported and failed/stale reads are not. With admitted
   support and saved enabled intent, `resumeInstallAndStartRuntime` resumes the needed phases
   for any backend mode, including already-Hub stopped/missing. Observe `onRuntime`, handle
   `failedStep`, then read back health. `installAndStartStep` only classifies;
   `installRuntimeUntilSettled` does not start. None of these helpers preflights support.
   No second unconditional lifecycle call follows successful controller recovery. Unsupported
   without running health holds the normal flow with recheck/guide/Back and intact drafts.
3. `resumeGatewayAdoption` is not a readiness/consent owner: it returns early for Hub with
   `runtime: null`, otherwise can ensure/start and returns backend-filtered candidates; it
   never changes mode/applies migration or checks support. Do not wrap healthy-engine actions
   in it just to impose an install gate. Reuse observed health and delegate full-scan consent
   to the D10 migration owner. If used for recovery, it needs the installing-path gate.
4. For a ready runtime, native rows delegate to existing complete-group review and explicit
   migration apply, with its actual ensure dependency; no native rows use the guarded
   `setAgentMode(backend, 'hub')` path without an extra confirmation/install gate. Migration
   already commits mode, so never follow it with another mode PATCH. Concurrent native state
   can still cause `mode_switch_blocked`. Read mode before eligibility (Direct has
   `sources: null`), and read each dependent write back. Runtime recovery, consent, source
   creation, mode and route writes are separate outcomes.

**Operation ownership at `08b82658bb` (not a universal mutation gate):**

| Setup operation | Actual dependency and admission |
| --- | --- |
| Runtime ensure/install/start; non-Hub `resumeGatewayAdoption` | Can call installer ensure. `runtime_start` also writes enabled intent and ensures before adapter start. Browser requests require fresh enabled config/support; server installer remains authoritative, including controller startup and other server-owned demand paths |
| API-key observe/create | Service `_observe_source_payload` provisions temporary material and probes through the adapter; create provisions and commits through `_commit_synced`. No installer ensure. With fresh healthy Hub, permit the existing form despite unsupported new installation; retain credential cleanup, nonce/unknown-write reconciliation and upstream failure semantics |
| OAuth start/status/submit and source completion | Existing OAuth owner calls its channel adapter. Hub adapter uses supervisor `client()`/`ensure_running()` on existing bytes and management endpoints; completion commits source projection. Native channel retains its separate auth/custody owner. No installer ensure in this flow. Keep single-flow, consent, cancellation, created-receipt and unknown-write handling |
| Mode change without native rows | `set_agent_mode` guards/drains native writers, rescans under lock and uses `_commit_synced`. No ensure; native rows are rejected, never silently taken over. Fresh health/config and mode readback remain required |
| Migration scan / apply | Full scan is discovery, not consent. New/incomplete `apply_native_migration._resume_takeover` explicitly ensures before native withdrawal and before sync/start; retain installing-path admission and journal recovery. A completed receipt replay returns before ensure. Healthy health alone cannot bypass this dependency; do not change migration semantics to remove it |
| Source/mode writes that change engine bindings | `_commit_synced` persists and calls adapter `sync_sources`; it waits for active transports and restarts a running engine via the existing verified disk binary, without installer ensure. Restart can fail if that binary/permissions/health is unavailable; owner restoration may also fail. Surface errors and re-read persisted config/runtime/receipts before retry, never claim atomic rollback |
| Exact route reads/preview/reorder | Pure chain reorder has unchanged bindings, so `_commit_synced` saves without restart or install. `RouteChainDialog` owns exact-hop/menu-model eligibility, guards and readback for the selected route |

Thus a usable running Hub is not disabled merely by `manifest.resolution === 'unsupported'`.
New takeover can still be blocked by its real ensure dependency; manual sources, existing
OAuth and guarded no-native mode configuration remain available under their own guards.
An unsupported host with neither usable Hub nor an admitted recovery path cannot complete
the default journey. Read failures authorize neither lifecycle writes nor completion.

### Screen 3 — assistants (current thread `PRRT_kwDOPbFPYs6kOVg4`)

| Element | Producer → consumer and rule |
| --- | --- |
| installed | detect configured path and deep-refresh through the shared `createAgentCollectionReadAuthority` on active assistant entry and before completion; consume its current result as C4 specifies. Cached `listAgents` alone is insufficient. Hidden screens do not poll |
| install | explicit `api.installAgent(name)` then detect the returned/configured path and await the collection authority refresh/current result. Path/agent-config changes have the same refresh requirement. Pending/stale/failed refresh holds completion and exposes Retry; retain install output and one loading indicator |
| enabled | existing Switch config write, followed by `getBackendConnection` and Agent reads. Saved config reconciles live backends; no second restart |
| upgrade | `BackendLifecycleChip.onVisual` still owns probe/write, activity-gated; update coexists with enabled state |
| route control | Confirmed Hub cards with a designated builtin Agent model show that model's first chain hop and backup count. Enabled cards open `RouteChainDialog` for only `(backend, menu model)`; disabled cards preview the same route but cannot edit it. A current successful route read is required to open the editor; pending or failed reads do not infer an empty model or authorize editing. Route-read failure has its own Retry. A Hub card with no model shows a hint to repair it later in Agent/Model Hub settings; Setup does not choose or write an Agent model. Direct cards retain native connection actions, but Direct cannot satisfy setup completion. Unknown or conflicting Hub/Direct ownership disables configuration until a fresh read. A disabled Hub configuration retains C2 recovery, never a Direct setup-completion bypass |
| candidate identity | for each backend, read its current designated builtin Agent, including disabled Agents, and use its saved menu model; custom Agents or the global default do not substitute as that card's route target. C4 may preserve a runnable custom default independently |
| route close | close/commit refreshes the card's server-backed route and connection; `RouteChainDialog` owns the edit draft and save lifecycle, not `SetupFlowState` |
| all uninstalled | three install actions, workspace entry disabled, Back still available |

### Per-assistant Model Hub route mapping

Each card reads its designated builtin Agent's saved menu model, including when that Agent
is disabled, and the matching chain from `GET /api/models/agents/{backend}/chains`. Read
the backend supply and sources once per refresh, not once per card. The card displays that
chain's actual first upstream model (display name when available, otherwise ID) and the
remaining hop count. The three cards may legitimately show different routes. A route on
another menu model, another backend, or an unrelated custom Agent is not substituted.

Only a current, successful read for the active screen may supply an editor selection. A
pending or failed read may retain old values for non-operational display, but it cannot
claim an empty model, enable the route action or open an old selection. Retry refreshes the
route inventory. A confirmed Hub card with a saved menu model opens the existing Model Hub
`RouteChainDialog` for exactly that `(backend, menu model)`, including when its chain is
empty. The dialog owns its own draft, preview, save guards, unknown-write reconciliation
and readback; Setup refreshes the card after commit or close. It does not write other
backends' chains. If the Agent has no model, Setup shows a hint and leaves model selection
and catalog repair to existing Agent/Model Hub settings.

Installing or enabling an assistant never adopts another assistant's route or changes its
Agent model. An install offers automatic enablement only when the click-time action promised
it for a confirmed Hub backend, and a newer explicit disable or changed backend ownership
prevents that later enable write. Direct cards retain native connection actions; they cannot
complete the Hub setup journey. There is no shared `routeOrder`, target projection,
multi-target partial receipt or cross-assistant rollback policy in Setup.

## Audit evidence and required consuming checks

| Repeated root class | Whole boundary audited / remaining evidence |
| --- | --- |
| route identity / eligibility / readiness | refreshed current CLI presence (existing generation owner) → action availability → named Agent+backend → mode → menu model → exact chain → default readback. C4/C6 keep completion on fresh server evidence; each card reads only its designated Agent's current route. `RouteChainDialog` owns single-route guards and readback; Setup does not coordinate cross-backend writes |
| lifecycle / capability / readiness | enabled setup prerequisite → active-provider persistence → controller recovery/server admission → browser observation → operation-specific recovery/consent → readback. Default Hub requirement and independent healthy-Hub use are preserved; no unsupported-to-Direct conversion. `gatewayAdoption.test.ts` proves helper scope only; it is not a screen-2 preflight test. Future L2 tests must cover unsupported with zero runtime artifact installation/start, healthy-engine source writes and restart failure, already-Hub missing/stopped, real controller recovery (external composed fixture available), start failure and native blockers |
| producer / consumer / state | C2 React consuming test covers cross-screen state plus a late functional update. Full scan → transitive backend groups → complete apply IDs follows `MigrationDialog` and its grouping tests; L2 still needs a consuming integration test for discovery → owner confirmation → receipt/readback, including no implicit apply and unchanged Settings behavior |
| duplicated instructions / copy | plan §§4/9/10, C1 metadata and C2/C4/C6 reviewed together. Automatic runtime setup reuses approved precedent; owner D10 selects existing migration takeover and supersedes the handoff promises; setup owns only discovery/context/result presentation. Task-specific design source overrides the general default; existing theme shadow token replaces the literal prescription. Source-ranking substitution and false approval/rollback claims are removed; handoff remains unchanged user source |

The original PR0 tests established interface shapes only. Current consuming tests cover
per-card route reads, current-read editor admission and model-less presentation; they do
not replace the server's route-save and migration-owner checks.
