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
   both. Namespaces are the ownership boundary: L1 takes `onboarding.flow.*` and the six
   changed welcome/access strings, L2 takes `onboarding.providers.*` and
   `onboarding.import.*`, L3 takes the new `onboarding.setup.*` and `onboarding.route.*`
   leaves and the two changed setup headings.
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
  `SetupFlowState` holds only what no server does.

`setupScreenSequence(modelHubEnabled)` encodes the capability degradation: three screens
normally, `intro → assistants` when a deployment explicitly disables Model Hub, and the
full sequence while the capability is still unread (`null`), because an unread capability
is not an absent one.

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

With Model Hub available, `进入工作台` is enabled when all three hold:

| Input | Producer | Predicate |
| --- | --- | --- |
| a ready model source | `modelsApi.listSources()` | at least one source whose `state.status` is a healthy status (not `cooldown`, `needs_action` or `error`) |
| a default route | `SetupFlowState.routeOrder` plus the persisted per-backend order | the order is non-empty AND every enabled backend reads it back through `getAgentSources(backend)` |
| a usable assistant | `api.detectCli` / `AgentSupply.cli_present`, and the enable write the identity Switch already owns | at least one of Claude Code, Codex, OpenCode is installed AND enabled |

Unchanged from today's `complete()`: a start is issued only from a confirmed `stopped`
application state; a usable selected Agent is preserved and only re-pointed when the
current default is not in the available set; `setup_completed` is written last so a failed
start leaves the AuthGuard's gate intact; `SetupPlatformRecovery` and `SetupModelRecovery`
keep their recovery paths.

Removed: the per-backend `getBackendConnection(...).ready` / `entry_eligible` requirement
and the OpenCode-only `readOpencodeSetupRoutes` filter, because onboarding no longer opens
a per-assistant connection dialog (handoff §13.5). Readiness is now a property of the
gateway supply and the route, which is what the screen shows.

Degradation: when `capabilities.model_hub.enabled` is explicitly false, the gate is the
predicate above minus its first two rows, evaluated against today's per-backend connection
readiness, and screen 3 keeps the per-assistant connection actions. One documented
degradation, not a third flow shape.

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
| gateway engine | `modelsApi.getRuntimeStatus()` → `installAndStartStep()` → `installRuntimeUntilSettled()` | entering screen 2 reads the status; `not_installed`/`not_started` runs the existing install-and-start path with progress inside the gateway card (D3); `manifest.resolution === 'unsupported'` surfaces the failure instead of retrying; a degraded engine still routes, so it does not block continuing |
| first-entry sequence | local | cards → inbound wires → gateway → outbound wires → destinations, about 1.1s, skipped under the C3 bail-out. The reference's reset/replay control is preview-only and does not ship |

### Screen 3 — assistants

| Element | Source of truth | Rule |
| --- | --- | --- |
| installed | `api.detectCli(path)`, corroborated by `AgentSupply.cli_present` | detection runs on the transition from screen 1 and is re-verified when the screen becomes active |
| install | `api.installAgent(name)` then re-detect with the returned path | one loading indicator per card; a failure keeps its message and output |
| enabled | the identity Switch's existing write path | disabling preserves configuration and says so (`onboarding.setup.disabledNotice`) |
| update available / update | `BackendLifecycleChip` with `onVisual` | the chip still owns the probe and the write; the card only draws the reported visual, and an update coexists with the enabled state |
| default-model chip | `getAgentSources(backend)` → `selected_model_id`, `model_supply` | rendered only when the assistant is enabled and a route resolves |
| route dialog list | the shared `SetupFlowState.routeOrder`, projected to rows | row = provider mark + model name + `服务名 · 首选/备用 N`; up/down disabled at the ends; a single route shows the "already preferred" note |
| route write | `putAgentSources(backend, { order })` for every enabled backend, with `setAgentMode(backend, 'hub')` where the backend is still Direct | one shared order, written per backend, because the contract has no global route object. A guard response (`would_interrupt`, `would_remove_hops`) is surfaced for confirmation and never auto-forced; a backend that refuses keeps the dialog open with the reason |
| 添加模型来源 | `onNavigate('providers')` | the footer's other exit is 完成, which closes and returns focus to the chip |
| all-uninstalled case | the three rows above | three install actions, primary action disabled, and going back to screen 2 stays available |
