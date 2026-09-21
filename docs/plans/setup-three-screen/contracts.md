# Setup three-screen — frozen contracts C1–C6

Status: **binding on lanes L1/L2/L3** from the merge of this PR. Read with
`docs/plans/2026-09-21-setup-three-screen-implementation-plan.md` (why) and
`docs/plans/2026-09-21-setup-three-screen-handoff.md` (what the owner approved).

A lane that needs a contract change reports it to the orchestrator and waits. It does not
edit another lane's files and it does not invent a parallel shape — `pr-delivery-loop` §1
is the reason this file exists at all.

| Contract | Where it lives | Owner |
| --- | --- | --- |
| C1 copy | `docs/plans/setup-three-screen/copy-contract.json` | orchestrator |
| C2 flow interface | `ui/src/components/onboarding/setupFlow.ts` | orchestrator |
| C3 geometry and DOM hooks | this file §C3 | L1, consumed by L2/L3 |
| C4 entry gate | this file §C4 | L3 |
| C5 stable dialog frame | this file §C5 | L2 (add + import), L3 (route) |
| C6 data mapping | this file §C6 | L2 (providers), L3 (assistants) |

## C1 — copy

`copy-contract.json` is the whole statement: 103 leaves under `onboarding.flow`,
`onboarding.providers`, `onboarding.import`, `onboarding.setup` and `onboarding.route`,
plus six existing strings it changes and thirteen it reuses untouched. It was validated
for zh/en key parity, complete `_one`/`_other` families on every `{{count}}` string, no
nested-before-literal key collision, and identical placeholder sets per key.

Three rulings a lane must not re-litigate locally:

1. **PR0 adds no key to the bundles.** `keyCoverage.test.ts` reads the literal keys out of
   the app source, so a key lands with the component that uses it, in the lane that owns
   both. Namespaces are the ownership boundary: L1 takes `onboarding.flow.*` and the four
   changed `onboarding.welcome.*` / `onboarding.access.*` strings, L2 takes
   `onboarding.providers.*` and `onboarding.import.*`, and L3 takes the new
   `onboarding.setup.*` and `onboarding.route.*` leaves plus the two changed
   `onboarding.setup.title` / `.subtitle` headings. `_changed_existing` carries an `owner`
   per row, and that field — not this sentence — settles a dispute.
2. **Setup says 导入 / import; Settings keeps 迁移 / migrate.** They are one mechanism
   (`scanMigration` / `applyMigration`) and two vocabularies, because `settings.models`
   copy is fenced by total-scope redlines (`copyAgreement.test.ts`,
   `supplyCopyRedline.test.ts`) and renaming it is a separate product-voice decision.
   `MigrationDialog` therefore grows a copy scope: setup passes the import strings,
   Settings passes nothing and keeps what it renders today. A test asserts the Settings
   rendering is byte-identical before and after.
3. **The plural-family rule is local to the new keys.** 66 existing `{{count}}` strings
   outside `settings.models` ship without families, so a bundle-wide redline would fail
   today; this round holds the new keys to it instead of retrofitting the app.

## C2 — flow interface

`setupFlow.ts` declares the screen ids, the handoff target, the action feed
(`SetupAction`), the imperative handle (`SetupScreenHandle.activate`), the screen props
and the shell-owned `SetupFlowState`. Two rules are the point of the file:

- **The shell renders the primary action and the Back action.** A screen describes the
  button through `onActionChange` and receives the click through `activate()`. Today
  `Welcome.tsx` and `AgentDetection.tsx` each render their own
  `.onboarding-setup-footer`; after L1 neither does.
- **All screens stay mounted.** An inactive screen is `hidden` + `inert`, never unmounted,
  so its state survives navigation without being lifted. Anything the server owns
  (installed CLIs, enabled backends, persisted sources) is read, not mirrored;
  `SetupFlowState` holds only what no server does — including the route dialog's unconfirmed
draft and its `routeOrderDirty` flag, so an edit survives a round trip to screen 2.

`setupCapability(boolean | null)` maps an authoritative capability read onto
`'pending' | 'enabled' | 'disabled'`, and `setupScreenSequence(capability)` encodes the
degradation: three screens normally, `intro → assistants` when a deployment explicitly
disables Model Hub. The producer matters as much as the mapping: `disabled` means an
authoritative read that says the deployment turned Model Hub off, never a request that
failed, so it is derived from the config the Wizard has already loaded
(`modelHubEnabledFromConfig`) rather than from `useModelHubCapability()`, which catches its
own error and resolves to `false` — collapsing a transient failure into a screen the
deployment never asked to lose. A `pending` capability returns the full sequence, and that return is not
the wait — `setupNavigationReady(capability)` is. The shell must hold the user on the
introduction with the primary action disabled until it is true, because entering the
providers screen on a guess and then removing it when the read resolves to `disabled`
recreates the very jump the shorter sequence exists to prevent.

## C3 — geometry and DOM hooks

**Anchor pair.** One `.onboarding-setup-footer` rendered by the shell, containing one
`.onboarding-action-w.onboarding-primary-action` and, below it, one
`.onboarding-action-w.onboarding-back-action`. Their boxes are identical on every screen,
in both languages, at every viewport; `geometry.spec.ts` already asserts that across two
screens and L1 extends the same assertion to three. The Back button keeps its box on
screen 1 by staying invisible (`visibility: hidden`), which is how the reference holds the
anchor without a layout shift.

**Shared reservation.** `.onboarding-stage` remains the box both the story and the screens
below it occupy, so the anchor's vertical position does not depend on what a screen draws.
The import capsule lives in a reserved slot inside screen 2's stage: dismissing it leaves
the slot, so the primary action cannot move (handoff §4, and the assertion #2065 already
writes for the two-step flow).

**Snapshot hooks.** The handoff reads live DOM across screens, so these class names are a
contract, not styling. L1's `setupHandoff.ts` queries them; L2 and L3 must render them.

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
`validate:theme` / `glowScale.test.mjs` keep that true.

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

With Model Hub available, `进入工作台` is enabled when at least one of Claude Code, Codex and
OpenCode is simultaneously:

| Condition | Authoritative read |
| --- | --- |
| installed | `api.detectCli(cli_path)`, corroborated by `AgentSupply.cli_present` |
| enabled | the identity Switch's existing write path, read back through `getBackendConnection(backend).enabled` |
| routed | the candidate's own supply, read per named Agent: `listVibeAgents()` candidates correlated with their entry in that backend's `AgentSupply.named_agents` (`effective_model_id`, `supply_status`) | the correlation `Wizard.complete()` already performs between candidate Agents and the backend supply projection, generalized from OpenCode-only to the three backends. It must NOT read the backend-level `selected_model_id`: that field describes only the route named by `selected_by_agent`, so it is null whenever the global default Agent belongs to another backend, and a predicate built on it rejects a runnable candidate on a different backend before the re-pointing rule below can select it |

and that assistant is selectable as the default Vibe Agent, which keeps today's rule that a
usable selected Agent is preserved and re-pointed only when the current default is not in the
available set.

Source health is not a separate condition: the per-Agent supply status is the server's own
answer about whether that Agent's next turn can run, and a healthy source that no route uses
does not make an assistant usable. `SetupFlowState.routeOrder` is the route dialog's working
preference and never gates entry — a gate resting on client draft state would block a
stateful installation that already has valid persisted routes (see C6's hydration row).

Unchanged from today's `complete()`: a start is issued only from a confirmed `stopped`
application state; a usable selected Agent is preserved and only re-pointed when the
current default is not in the available set; `setup_completed` is written last so a failed
start leaves the AuthGuard's gate intact; `SetupPlatformRecovery` and `SetupModelRecovery`
keep their recovery paths.

Removed: `entry_eligible` / per-backend connection `ready` as the entry condition, and the
OpenCode-only scope of the route filter, because onboarding no longer opens a per-assistant
connection dialog (handoff §13.5). `getBackendConnection` stays the owner of the enable read
and of the confirmed-stopped service start. Readiness becomes a property of the assistant
that will actually run, which is what screen 3 shows.

Degradation: when `capabilities.model_hub.enabled` is explicitly false there is no hub route
to read, so the routed condition is today's per-backend connection readiness and screen 3
keeps the per-assistant connection actions. One documented degradation, not a third flow
shape.

Test impact, stated openly rather than absorbed: the invariants that named per-backend
connection readiness as the entry condition are re-pinned to the table above; the
invariants about service start, default-Agent preservation, recovery surfaces and the
`setup_completed` write order stay exactly as they are.

## C5 — stable dialog frame

Applies to all three screen-2 dialogs and to screen 3's route dialog.

- One frame per dialog across every tab and state: 568 wide, 24 padding, 16 radius, 20
  gaps, and a common height taken from that dialog's longer ordinary form (not from an
  unusually long error), bounded by the viewport.
- Anchored: title, description, method tabs, close control, action footer. Scrolling: the
  middle region only — long errors, provider lists and authorization notes scroll inside
  it, and every action stays reachable at browser zoom and on a short screen.
- Loading, tab switching, authorization progress and error all reuse the same frame; a tab
- switch preserves drafts and focus. A hidden pane keeps no focusable control and starts no
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

A row that describes shipped behavior names the module that owns it and says reuse
unchanged; the mechanics live in that owner's code and tests, not here, and a second copy of
them is a second place to be wrong. Where a row states a trap, it states only the trap.
Anything setup does that no shipped module owns is written out in full, and a lane that finds
a claim here contradicting the implementation reports it instead of following it.

### Screen 2 — providers

| Element | Source of truth | Rule |
| --- | --- | --- |
| detected candidates | `modelsApi.scanMigration()` filtered by `isImportableKey` | API-key rows only; `proposed_action === 'reauth'` and OAuth/subscription rows are excluded, so the capsule count and the import list can never disagree |
| candidate identity | `MigrationItem.vendor`, `.display_name`, `.masked_credential`, `.backend`, `.notes_key` | the card's logo comes from `vendor` through `vendorMarks`; the key line from `masked_credential`; the row's origin line from `migration.source.*` / `notes_key`. Nothing is masked client-side |
| slots 1–2 | detected candidates, then already-added sources | fill to two with OpenAI then Anthropic when fewer are known; a detected candidate starts selected |
| slot 3 | always "Add more" | its badge counts `SetupFlowState.addedThroughMore` entries that are ready |
| added/connected card | `modelsApi.listSources()` | a card is connected when a source with that `vendor` exists and its `state.status` is healthy; connected wears one check, never an "已添加" label |
| selection | `SetupFlowState.providerSelection` | membership means "in the next import batch"; toggling never changes a connected card's fill |
| summary line | the three above | four states, `aria-live`: pending selection (with 仅复制，保留原配置), added, error, none |
| capsule | `ImportKeysNotice` | real candidate count from the same filtered scan; dismissal keeps the existing non-nagging signature in `modelHubMigrationDismiss`; moves to screen 2 and gains the reserved slot |
| import batch | `modelsApi.applyMigration(itemIds)` | one atomic batch: all succeed or all fail with retry, selection and input preserved. No partial-success branch, no simulated percentage. The completion count is cumulative for this flow; imported rows drop out of the next scan; the remainder stays re-enterable. Import completion is not assistant readiness |
| add — API Key tab | `modelsApi.observeApiKeySource(draft)` then `createApiKeySource(draft)` | the detect-then-confirm flow #1831 shipped; the form body is the extracted shared one (D2), so Settings and setup cannot drift |
| add — Subscription tab | `modelsApi.startOAuth(vendor, channel)` → `getOAuthStatus(flowId)` → `submitOAuth` when a value is required | the offered vendors come from the backend's real OAuth capability, never from a fixture list; the design's two are OpenAI/ChatGPT and Anthropic/Claude |
| add — Detected tab | the same filtered scan, minus the candidates already on the cards | already-added rows are disabled and marked; the tab appears only when unlisted candidates exist |
| vendor picker order | `vibe/data/api_key_vendors.json` through `apiKeyVendors.ts` | the first eight are the primary grid in file order; `更多服务商` holds the catalog's remainder (Z.AI, Mistral, Groq, Together, Fireworks) plus 自定义. The UI partitions by position and never re-sorts, and it does not edit the backend-owned catalog |
| gateway engine | `resumeGatewayAdoption(modelsApi, agentReads, backend)` in `settings/models/gatewayAdoption.ts` | the shipped owner of "prepare this backend for the gateway", and the only sequence screen 2 uses: it reads the agents, returns early when the backend is already `hub`, and otherwise ensures the engine through `resumeInstallAndStartRuntime` — install AND start, because `installAndStartStep` is only a classifier and `installRuntimeUntilSettled` only installs — then scans that backend's migration candidates. Progress and the classified `failure`/`failedStep` render inside the gateway card (D3); `runtimeCanAttemptInstall` (a `manifest.resolution` other than `unsupported`) decides whether an install may be attempted at all, and an unsupported host surfaces the failure instead of retrying. A degraded but running engine still routes, so it does not block continuing |
| first-entry sequence | local | cards → inbound wires → gateway → outbound wires → destinations, about 1.1s, skipped under the C3 bail-out. The reference's reset/replay control is preview-only and does not ship |

### Screen 3 — assistants

| Element | Source of truth | Rule |
| --- | --- | --- |
| installed | `api.detectCli(path)`, corroborated by `AgentSupply.cli_present` | detection runs on the transition from screen 1 and is re-verified when the screen becomes active |
| install | `api.installAgent(name)` then re-detect with the returned path | one loading indicator per card; a failure keeps its message and output |
| enabled | the identity Switch's existing write path | disabling preserves configuration and says so (`onboarding.setup.disabledNotice`) |
| update available / update | `BackendLifecycleChip` with `onVisual` | the chip still owns the probe and the write; the card only draws the reported visual, and an update coexists with the enabled state |
| default-model chip | the candidate Agent's own `named_agents` entry (`effective_model_id`, `supply_status`) | rendered only when the assistant is enabled and that Agent's route resolves; it shows the model this Agent would actually ask for, not the backend-level projection, which is null whenever the default Agent belongs to another backend |
| 添加模型来源 | `onNavigate('providers')` | the footer's other exit is 完成, which closes and returns focus to the chip |
| all-uninstalled case | the install, enable and route rows above | three install actions, primary action disabled, and going back to screen 2 stays available |

### Route mapping — L3-owned, frozen in its own PR

The route dialog's data mapping is deliberately NOT specified here. It has one owner (L3), no
other lane consumes it, and three review rounds showed that prescribing hub mechanics from a
document produces a second, drift-prone copy of what the service already defines. L3 resolves
it against the implementation, pins it with tests, and records the resolved mapping in its PR
body. What this file does carry is the decision that bounds the choice and the traps already
paid for.

**D9 — the dialog ranks SOURCES, not free model choices.** Its rows are the sources that can
serve the assistant's selected menu model, each drawn with its provider mark, its display
name and the model id it would serve, ranked 首选 / 备用 N; the write is the per-backend
source order through the shipped owner (`SourceOrderDrawer.save`'s semantics, guard echo
included). Exact per-model hop editing stays where it already lives — Settings'
`RouteChainDialog` — which the dialog's own 添加模型来源 exit and Settings both reach. The
alternative, ranking arbitrary `(source, model)` pairs through `putAgentChain`, was rejected:
`putAgentSources` replaces source membership and order and names no model, so model-ranked
rows either cannot be derived unambiguously or would save a source priority while claiming to
save a model route. If L3 finds the design's rows cannot be rendered truthfully as source
identities, that is a report to the orchestrator, not a silent switch to the chain APIs.

Traps already paid for, which L3's implementation and tests must satisfy:

1. **Hydration merges, it does not pick.** With disjoint persisted orders — Claude and Codex
   each on its own native subscription — taking one backend's order drops the other's source
   id, the later projection can only filter ids already present, and that backend ends up
   skipped with a route nobody can view or reorder. Merge deterministically: the backend that
   will run establishes precedence, ids found only on other enabled backends are appended in
   their persisted order, and a write never omits a source a backend currently has enabled
   unless the user removed it in the dialog.
2. **A dirty draft survives navigation.** Reordering and then leaving through 添加模型来源 must
   not rehydrate over the edit; `SetupFlowState.routeOrderDirty` decides, and a confirmed
   write reconciles and clears it.
3. **Mode before eligibility.** In Direct mode `sources` is `null` and `eligibilityOf` marks
   every source ineligible, so a projection-first sequence skips every Direct backend and a
   fresh installation can never establish a route. Switch and re-read first, through the
   shipped adoption path.
4. **Readiness is per candidate Agent.** `AgentSupply.selected_model_id` describes only the
   route named by `selected_by_agent` and is null when the default Agent belongs to another
   backend; correlate `listVibeAgents()` candidates with their `named_agents` entry instead
   (C4).
