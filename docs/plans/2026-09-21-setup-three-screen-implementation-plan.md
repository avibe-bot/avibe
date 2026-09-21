# Setup three-screen rebuild — implementation plan

Status: **PR0 bounded repair; feature implementation not dispatched** (2026-09-21).
The owner's explicit merge authority covered #2065 only. Current orchestrator requests
from `ses8dhcc2zq62` authorize PR0 repair, not #2082 merge or feature implementation.
The owner reaffirmed on 2026-09-21 12:56 +08 that setup uses Model Hub and disabling it
belongs to Settings afterward. No unsupported/error/explicitly-disabled Direct setup branch
is part of the contract. That decision does not
approve D10 or feature/merge authority. Routine orchestrator choices remain distinct from
the **D10 custody choice** in §10. D4/D9
routing is orchestrator-ratified as a bounded technical interpretation; the corrected D11
transport sequence is also ratified after separate frontend/backend proofs and an independent
10-test transport rerun; no unresolved API design is delegated to the owner.

Shared detail lives in `docs/plans/setup-three-screen/contracts.md` (C3–C6),
`copy-contract.json` (C1, one takeover proposal with explicitly provisional migration entries),
and `ui/src/components/onboarding/setupFlow.ts` (C2). The handoff remains unchanged user
source. C1 migration copy and C6 setup apply cannot bind until D10 is decided; this is not
a claim that the entire program is frozen or authorized for dispatch.

Sources and provenance:

- Handoff spec: `docs/plans/2026-09-21-setup-three-screen-handoff.md` (already tracked
  by PR0; preserved byte-for-byte against the dispatched head).
- Approved runtime/custody precedent: `docs/plans/model-hub-native-takeover.md`, especially
  Approved user flow (default runtime, no enable-confirmation, separate takeover consent).
- Approved contract: `docs/plans/2026-09-19-desktop-connection-settings-alignment.md`
  (worktree `desktop-alignment-contract-20260919`).
- Interactive reference: Show session `ses36vg559de2` — `pages/index.tsx`,
  `GatewaySetup.tsx`, `KeyImport.tsx`, `SetupHandoff.ts`, `setup-flow.css`,
  `gateway-flow.css`.
- Design source: `../avibe-docs/design_desktop.pen`, board index in the handoff §12.
- Product baseline: `origin/master` at `31c4e831b`, which is `a07acee02` plus the merged
  #2065. This is the initial planning baseline, not a claim about today's primary checkout.
  This repair re-read shipped owners from the assigned head `890014421f`; primary dirty
  files were not edited.

## 1. Goal

Replace today's two-step `/setup` (welcome → assistant connection) with the handoff's
three-screen flow — collaboration highlights → model providers → assistant enablement —
inside one sidebar-free shell whose primary action never moves. Default setup requires a
usable Model Hub candidate. Unsupported installation without a usable running Hub is a
recoverable environmental failure on the same flow, not an automatic Direct alternative.
Explicitly disabled configuration is a setup prerequisite failure, preserved without a
Direct completion or silent rewrite. Post-setup Settings remains separately owned.

The design is not a new surface bolted onto the old one: screen 1 already ships, screen 3
is a restructure of what ships, and screen 2 is a new composition of Model Hub APIs that
already ship. C6 defines explicit model-chain projection and controller/runtime ordering
against those owners. The handoff's copy-only import promise conflicts with existing custody
policy; D10 must resolve that product choice before implementing setup migration.

## 2. Baseline and preconditions

- Fork every lane from a refreshed `origin/master` in its own worktree under
  `.worktrees/avibe/<branch>`, per `pr-delivery-loop` §0. No stacked PRs: each PR bases
  on `master` and declares unmerged dependencies in its body.
- Contracts (§5) are committed to `master` **before** any lane forks. A contract that
  lands after the fork is two divergent copies. This PR is that commit.
- **#2065 is merged** (squash `31c4e831b`, owner-authorized). Before merging it was
  verified at head `25de7e7d6`: a Codex pass comment naming that sha, the bot's `+1` on
  the PR body, 16/16 review threads resolved, CI 18/18 success, `mergeStateStatus: CLEAN`.
  It supplies exactly the surfaces this plan builds on: the 44px brand lockup and round
  language button, the `--ob-*` card-height/headline tier system in `onboarding.css`, the
  switch-in-identity-header card shape, the lifecycle chip's `onVisual` callback, and the
  action-anchor geometry specs.
- Model Hub is **default-on** since `06f444c13 feat(model-hub): enable Model Hub by
  default (#1917)` — `is_model_hub_enabled()` defaults to `"1"` with an explicit
  deployment override. Screen 2 is therefore a default path, not a flagged one. The
  capability projection (`capabilities.model_hub.enabled`) still exists. A disabled value
  holds setup at its configuration/recovery boundary; it does not select another journey (D5).
- Acceptance runs on the owner's designated cloud target
  <https://avibe-cloud-e2e-app.avibe.bot> (workspace `AGENTS.md`, 2026-09-18). The initial plan recorded
  `401 remote_access_login_required` unauthenticated; this repair made no remote probe. Its capability flag, gateway
  runtime health and backend modes must be read with credentials at acceptance time, and
  deploying to it needs explicit owner authority.

## 3. Reuse inventory (read before writing anything)

| Need | Existing owner on `origin/master` | State for this round |
| --- | --- | --- |
| Setup shell, brand, language | `ui/src/components/Wizard.tsx` (`.onboarding-shell`), `visual/BrandLogo.tsx`, `LanguageSwitcher.tsx` | shipped; design lockup refined by #2065 |
| Screen 1 story, cards, wires, loop caption | `steps/Welcome.tsx`, `onboarding/CollaborationStory.tsx`, `onboarding/collaborationTimeline.ts`, `onboarding/motion.ts` | shipped (#2048/#2052/#2065); adapts to the new shell |
| Six access entries | `onboarding/AccessTiles.tsx` | shipped; needs `hidden`/`inert` instead of unmount |
| Assistant cards, install/detect/enable | `steps/AgentDetection.tsx` (606 lines), `onboarding/AssistantRow.tsx`, `settings/BackendLifecycleChip.tsx` | shipped; restructured by screen 3 |
| Per-assistant connection dialog | `onboarding/BackendConnectionDialog.tsx` | leaves setup; existing Settings/backend Direct support remains unchanged |
| Discovery capsule + dismissal memory | `onboarding/ImportKeysNotice.tsx`, `lib/modelHubMigrationDismiss.ts` | shipped; C6 identifies copy and scan-ownership gaps before moving it to screen 2 |
| Import batch dialog | `settings/models/MigrationDialog.tsx`, `migrationScan.ts` (`importableKeys`, `isImportableKey`, `scanMigrationWhenEnabled`) | shipped takeover/grouping owner; needs controlled state adaptation, and D10 withholds setup takeover adoption |
| Add API key: vendor picker, observe → create | `settings/models/AddApiKeyDialog.tsx` (41 KB impl, 40 KB tests), `apiKeyVendors.ts`, `vendorMarks.ts`, `vendorGlyph.tsx` | shipped; needs its form extracted so a tabbed stable frame can host it (D2) |
| Subscription sign-in | `modelsApi.startOAuth/getOAuthStatus/submitOAuth/cancelOAuth`, `settings/models/OAuthConnectDialog.tsx`, `settings/oauth/OAuthFlowParts.tsx` | shipped; subscription tab drives it |
| Added providers, masked keys, vendor identity | `modelsApi.listSources()`, `Source{vendor, display_name, masked_credential, account_label, state, models}` | shipped; screen 2's connected cards |
| Detected candidates with vendor, mask and source label | `modelsApi.scanMigration()`, `MigrationItem{id, vendor?, display_name?, masked_credential?, masked_detail, backend, source_paths?, required_backends?, notes_key?, proposed_action}` | shipped; screen 2's detected slots and import rows |
| Per-backend route read/write | `modelsApi.getAgentSources/putAgentSources/getAgentChain/putAgentChain/previewAgentChain`, `settings/models/RouteChainDialog.tsx`, `routeChainDraft.ts` | shipped; **there is no global route object** — see C6 and D4 |
| Gateway engine lifecycle | `settings/models/gatewayAdoption.ts` (`resumeGatewayAdoption`) over `runtimeLifecycle.ts` (`resumeInstallAndStartRuntime`, `installRuntimeUntilSettled`, `installAndStartStep`, `runtimeCanAttemptInstall`), plus `RuntimeNotStartedAction`, `InstallGatewayDialog`, `EnableGatewayDialog` | shipped primitives; neither helper preflights support, adoption skips already-Hub runtime checks and never changes mode. C6 defines required composition; D3 reuses approved automatic active-entry setup |
| Capability gate | `settings/models/featureFlags.ts`, `useModelHubCapability.ts`, `ModelHubCapabilityGate.tsx` | shipped |
| Wire drawing pattern | `settings/models/SupplyGraph.tsx` (measures `[data-source-id]` → `[data-agent-backend]`, `xl:block` only) | shipped; screen 2 needs vertical fan-in/fan-out at every width, so a new component reusing the same wire tokens rather than a bent `SupplyGraph` |
| Geometry and behavior harness | `ui/e2e/onboarding-fidelity/{fixture.tsx, support.ts, geometry.spec.ts, connections.spec.ts, loop.spec.ts, hub-ownership.spec.ts}` | shipped; `serveModelHub` already mocks scan/apply and must grow sources/runtime/agents routes |
| Theme tokens and guards | `ui/src/index.css` token scale, `ui/scripts/glowScale.test.mjs`, `npm run validate:theme` | shipped; no new literal colors |
| Onboarding behavior scenarios | `tests/scenarios/auth_setup/catalog.yaml` + `test_auth_setup_scenarios.py` | shipped; must be updated with the flow change (repo `AGENTS.md` §7) |
| Vendor logo artwork | `settings/models/vendorMarks.ts` — openai, anthropic, xai, gemini, deepseek, qwen, kimi, openrouter, mistral (+ monogram fallback) | shipped as inline paths; **no binary assets and no new dependency** |
| Vendor catalog and its order | `vibe/data/api_key_vendors.json`, tracked jointly with `vibe/model_hub_runtime/api_key_vendors.py`: `openai, anthropic, xai, gemini, deepseek, qwen, kimi, openrouter, zhipuai, mistral, groq, together, fireworks` | shipped; the first eight are exactly the contract's primary row. Cohere exists only as a prototype fixture (D7) |

## 4. Delta by area

### 4.1 Flow shell and state machine (new)

`Wizard.tsx` today holds `step: 'welcome' | 'agents'` and renders one step at a time, so
leaving a screen destroys its state — the class of bug the handoff §5 already records
("09-20 修复过回退丢状态"). The new shell:

- keeps all three screens mounted and gates them with `hidden` + `inert` (handoff §10),
  which retains local drafts. Hidden/inert does not suspend effects: C2 gates screen activity,
  polling and authorization; only the active screen may publish actions or navigate;
- renders **one** primary action and **one** Back button itself, so the "coordinates and
  size never change" invariant is a property of the tree instead of an agreement between
  two stylesheets. The active screen feeds `SetupAction` (`labelKey`, optional `labelArgs`, `disabled`, `busy`, `icon`) and exposes
  `activate()` through a ref — the reference's `onActionChange` + `GatewayHandle` shape;
- owns D11 config/bootstrap on active provider entry with enabled capability and saved runtime intent. Intro mount
  is read-only; config/readback, confirmed controller start and runtime preparation remain
  distinct outcomes. No bootstrap expansion for disabled/Direct setup;
- passes capability, saved `model_hub.enabled` and the existing runtime `RegionRead` through C2 with the shared retry
  callback. Runtime errors/support never change page sequence or saved modes;
- owns the flow state that must survive navigation (pending selection, imported count,
  model-ranked route draft). Server-owned install/enable facts are re-read, not copied into
  `SetupFlowState`; pass `flowState` and React `setFlowState` through C2, using functional
  updates so asynchronous completions preserve other screens' edits;
- moves focus to the screen's `h1` (`tabIndex={-1}`) on every screen change and scrolls
  to top on phones;
- keeps platform recovery (`SetupPlatformRecovery`) and the final `setup_completed` write.
  Hub model recovery follows C6; `SetupModelRecovery` remains its shipped Direct owner and
  is not repurposed to bypass Hub setup.

### 4.2 Screen 1 — collaboration highlights

Mostly shipped. Delta: the CTA leaves `Welcome.tsx` for the shell; the CLI detection that
`Welcome.start()` runs today (`api.detectCli` per assistant) stays on the transition and
is re-verified when screen 3 becomes active; `AccessTiles` becomes `hidden`/`inert` off screen 1
instead of unmounting; the three story cards become the handoff snapshot source.

### 4.3 Screen 2 — model providers (new, the bulk of the work)

Composition, top to bottom: three provider cards → inbound wires → gateway card →
outbound wires → three assistant destinations → summary line → reserved capsule slot.

- **Slots.** 1–2 are providers detected on this machine or already added; when fewer than
  two are known, fill with OpenAI then Anthropic. Slot 3 is always "Add more" (dashed
  frame, plus glyph, `订阅 / API Key`), and carries a count badge of what was added
  through it.
- **Card content.** logo + name + key line. Key line: added → `API Key sk••••34d8`;
  detected-not-added → `检测到 API Key sk••••xxxx`; phones drop the label and keep the
  mask. Data comes from `Source.masked_credential` / optional `MigrationItem.masked_credential`;
  use `masked_detail` for legacy scan rows as C6 specifies. These are render examples, not
  fixture values to ship; never mask or fabricate credentials client-side.
- **Card state.** added/selected → mint wash + mint border + restrained shadow + one
  check on the right (desktop: vertically centred; phone: top-right 6px/14px); not added
  → ordinary border + plus. No "已添加/Added" text on a card. Detected unblocked consent groups start
  selected according to server flags; clicking toggles the complete linked group through C6,
  never one key of a backend. Provider identity alone is not key/source identity.
- **Wires and gateway card.** 1.5px accent, round caps, r2.5 endpoint dots, pulse on
  arrival; geometry re-measured from real card centres on resize. Gateway card is one
  provider-card wide, 72 high (phone: min 72), two centred lines, no logo, and takes a
  connected treatment once a provider is ready.
- **Summary line** (`aria-live`): pending selection / added / error / none, with the
  C1 text subject to D10 provisional migration entries; no copy-only guarantee with takeover apply.
- **Capsule.** `ImportKeysNotice` moves here, keeping its persisted non-nagging
  dismissal. Count only keys in complete unblocked key-only groups (C6), with blocked
  detections still visible for explanation; give it a reserved slot so dismissing it
  cannot move the primary action.
- **Import dialog.** Follow C6's full-scan, transitive backend consent grouping. Cards,
  capsule and Detected tab share `providerSelection`; L2 must adapt/extract the existing
  `MigrationDialog` owner because its shipped props keep local state. Preserve blockers and
  linked rows outside the entry filter. API-key-only setup cannot submit mixed OAuth groups.
  Setup adoption of apply is provisional on D10. The proposed primary/Detected action opens
  review, displays the exact approved consequence and Not now / Start migration; only that
  confirmation applies. Runtime preparation and selecting a group do not consent to takeover. On confirmed
  success, count `result.applied` once per batch and refresh scan/sources/supplies; do not
  count it as keys without proof or equate completion with assistant readiness.
- **Add-source dialog.** One stable frame (C5) with three methods: Detected (only when
  unlisted candidates exist; complete-group selection; required rows/blockers stay visible),
  Subscription (OpenAI/ChatGPT and Anthropic/Claude, using the shipped subscription
  vendor/custody options; C6 names the actual producer, not an absent capability-list API), API Key (the extracted vendor form:
  eight primaries in catalog order, `更多服务商` collapsed holding the catalog's
  remainder, empty input disables the action). The key field keeps the extracted shared
  form's own treatment; the prototype's `sk-demo-example` placeholder and its
  "do not enter real credentials" line are preview-only and ship nowhere (C1's rules,
  handoff §15). Progress follows real stages; failure keeps drafts, and unknown writes are reconciled
  before retrying. Read `SourceCreated` / `OAuthResult.created` and refresh server facts as C6
  requires; never create a source twice after OAuth success.
- **Primary action states.** Proposed `查看 N 项并继续` / `查看 N 项迁移状态` open the
  shared confirmation/recovery; `继续，选择 AI 助手` continues only when no pending apply
  needs review; `添加订阅或 API Key` opens Add; connecting/checking states disable it.
  C1 migration actions remain provisional on D10. Selecting cards never applies credentials.
- **Bootstrap/runtime.** With capability and saved runtime intent enabled, the shell's active provider-entry owner
  uses D11's CSRF-aware JSON POST `{}`, HTTP/body validation and uncached GET/readback before
  connection/start calls. Unknown writes need persistence reconciliation, not a cached GET
  or empty `mutateConfig([])`. Fresh config is installed in shell state; direct fetch does
  not clear ApiContext cache or emit convergence. Reuse a running controller or start only
  a confirmed stopped one, then read runtime. D3 preflights every
  potentially installing path, ensures admitted runtime and reads back running health.
  Unsupported/non-running keeps providers, drafts, Back, recheck and C1's installation-guide
  action. Errors stay errors; successful retry resumes the same flow. Healthy running Hub
  reads/routes and Continue remain usable despite unsupported install admission. No extra
  install-confirmation dialog, automatic Direct bypass or credential takeover.
- **First-entry sequence** (~1.1s): cards → inbound wires → gateway → outbound wires →
  destinations. The reference's reset/replay control is preview-only and does not ship.

### 4.4 Screen 3 — choose and enable assistants

Card becomes: identity row (logo 30, name 17/600, role 11/700 trailing, enable Switch
only when installed) → divider → status pill → description → action row.

- Status pill: 已启用 / 未启用 / 未安装 / 安装中 / 升级中, with dot or spinner — exactly
  one loading indicator per card while an operation runs.
- Description follows the state: not installed / enabled / installed-not-enabled.
- Actions: `立即安装` (outline + Download, spinner `正在安装…`) → `api.installAgent` then
  re-detect; routed candidate chip (`默认模型` + model name + ChevronRight) → route dialog;
  installed/enabled but unrouted (including Direct) → configure-route action, equally reachable;
  `已安装，未启用` static row (enabling belongs to the identity Switch); `立即升级`
  (outline + ArrowUpToLine) through `BackendLifecycleChip`'s `onVisual`, coexisting with
  the enabled state.
- Enabled card wears the same mint treatment as screen 2's added cards.
- **Default model route dialog**: shared ordered list, `提供商 logo + 模型名 + 服务名 ·
  首选/备用 N`, up/down with first/last disabled, single-route note, footer
  `添加模型来源` (→ screen 2) + `完成`, focus back to the chip.
  The rows remain model-ranked as the handoff requests. C2 retains `(source_id, model_id)`
  draft rows; no source-order write can claim to save that list. C6/D4/D9 specifies builtin
  setup Agent targets, exact menu-model identities, per-target membership projection and
  readback/retry. Custom Agents with other menu models are untouched; same-key shared impact
  is disclosed. Fresh/empty targets use existing catalog/hop editors with explicit selection.
  Divergent routes are displayed without an opening write; a dirty shared draft and its
  target baselines survive navigation. Done without an edit is a no-op; an explicit reorder
  freezes changed automatic chains and requires the existing follows/frozen explanation.
- **Per-assistant Direct dialogs leave setup.** Keep their separate Settings/backend owner
  untouched. Setup always opens Hub configuration/recovery; disabled configuration exposes
  C2's prerequisite boundary, never native-auth completion. Routing access needs no route.
- All-uninstalled case: three `立即安装` cards, primary action disabled, and the user can
  go back to screen 2 to add sources first.

### 4.5 Readiness gate and completion

`Wizard.complete()` today requires a per-backend `entry_eligible` connection, starts the
service only from a confirmed stopped state, filters OpenCode agents by
`readOpencodeSetupRoutes`, preserves a usable default Agent, and writes `setup_completed`
last. D11 proves config persistence then confirmed controller startup on active provider
entry before Hub reads. Completion reuses confirmed-start recovery if the controller later
stops. Disabled gateway setup does not bypass providers or inherit Direct completion. Startup does not require installed assistants or sources.
The new gate preserves default/completion ordering and backend permission checks and replaces the route
input with C4's correlated named-Agent predicate: the same enabled/non-archived Agent has an
installed/enabled backend, running Hub runtime, a runnable own model/route and confirmed
application state. Source health, another backend's order and a local draft cannot satisfy
separate parts of the gate. Preserve a usable global default, otherwise select and read back
an available named Agent; write/read back `setup_completed` last. Re-pin the affected
`WizardCompletion.test.tsx` invariants deliberately. The all-backend correlation is new L3
orchestration, not behavior today's OpenCode helper already provides. Installation support
does not change this Hub requirement: no usable running Hub means enabled setup cannot
complete. Recheck/guide/Back remain available. Disabled configuration also holds setup,
preserving saved preferences/custody and existing management boundaries; it never creates
Direct completion. C4 records this owner-ratified known-by-design boundary, not a review waiver.

### 4.6 Dialogs and the stable frame

Handoff §7 / C5 applies to all three screen-2 dialogs: one frame per dialog across its tabs
and states (568 wide, 24 padding, 16 radius, 20 gaps, a common height taken from the
longer ordinary form, viewport-bounded), anchored title/description/tabs/close/footer,
scrolling middle, drafts and focus preserved across tab changes, no focusable controls
and no duplicate auth side effects in hidden panes, phone keyboard and safe-area handled.
Acceptance is the repeated-tab-switch test, not a height transition.

### 4.7 Motion and handoff

Port `SetupHandoff.ts` to `ui/src/components/onboarding/setupHandoff.ts`: snapshot the
outgoing cards, FLIP them to the incoming positions, retire the provider stage on 2→3.
1→2 shrinks the story cards into the destination row while the provider cards and gateway
press in from above and the wires draw after; 2→3 lifts the destinations into full
assistant cards. Bail out to an instant switch under `prefers-reduced-motion`, a hidden
page, or a paused preview; scroll to top on phones before switching; the primary action
stays disabled and unmoved throughout.

### 4.8 Theme tokens and responsive tiers

No new literal colors: the handoff's hex values map onto the existing token scale, and
missing tokens are added to `index.css` rather than hardcoded (`validate:theme` and
`glowScale.test.mjs` guard this). Card hover is the restrained mint border plus
`0 2px 16px -4px #5bffa038` with no translate or scale.

Extend #2065's `--ob-*` tier system to all screens under C3's existing media-query bands.
Map the handoff's desktop/phone measurements into those tokens; do not independently add
its differently numbered breakpoints in a screen stylesheet. Where exact design fidelity
requires changing a band, L1 must reconcile C3 and geometry fixtures with the orchestrator
before distributing it to L2/L3.

### 4.9 i18n

C1 (`docs/plans/setup-three-screen/copy-contract.json`) is the proposed copy table and the only
source of key names; handoff §8 is where its strings came from. New namespaces:
`onboarding.flow.*` (L1), `onboarding.providers.*` and `onboarding.import.*` (L2), and new
`onboarding.setup.*` / `onboarding.route.*` leaves (L3). Six shipped strings change: the four
welcome/access ones are L1's, the two setup headings are L3's. The capsule keeps
`settings.models.importNotice.*` for the discovery sentence, help link and dismissal label,
and the import dialog keeps `settings.models.migration.{blocked,errors,notes,source}.*` for
the explanations it renders, while setup chrome reads `onboarding.import.*`, subject to D10. `_provisional_copy`
withholds the migration entries from shipping until the custody choice is made. D8 is the
orchestrator's setup copy-scope decision; adapting the component remains future L2 work. No
display string is hardcoded in a component.

### 4.10 Accessibility

Focus to `h1` on screen change; `aria-live` on summary, status pill, capsule and progress;
`aria-pressed` on cards, tabs and provider buttons; labelled Switches and icon buttons;
tooltip via `aria-describedby` + `aria-expanded`; dialog focus trap with focus restored to
the trigger; Escape and outside click close menus, tooltips and dialogs (the import dialog
is not closable mid-progress); non-current screens `hidden`/`inert`.

## 5. Contracts proposed by PR0

PR0 carries six boundaries. They are not dispatch-ready until the explicit dependencies
in §10 are settled; no lane may reinterpret them independently:

- **C1 Copy** → `docs/plans/setup-three-screen/copy-contract.json`. Allocated
  key names, the six shipped strings they change, and the existing strings they reuse (fixture metadata);
  validated for zh/en parity, complete `_one`/`_other` families on every `{{count}}`
  string, no nested-before-literal collision and identical placeholders per key. Key names
  are allocated across lanes; migration wording/behavior remains provisional on D10.
- **C2 Flow interface** → `ui/src/components/onboarding/setupFlow.ts`, with
  `setupFlow.test.ts` covering sequence, navigation and actual React consumption of shared
  state, including a late update after another screen changes the route draft.
- **C3 Geometry and DOM hooks** → `contracts.md` §C3: the single shell-rendered anchor
  pair, the reserved capsule slot, the class names the handoff snapshots read across
  screens, and the tier rule (consume `--ob-*`, restate inside the existing bands, never
  invent a new one).
- **C4 Entry gate** → `contracts.md` §C4: the correlated candidate predicate and its producers, what stays
  unchanged in `complete()`, what is removed, the Hub-required configuration boundary, and which
  `WizardCompletion.test.tsx` invariants are deliberately re-pinned.
- **C5 Stable frame** → `contracts.md` §C5.
- **C6 Data mapping** → `contracts.md` §C6, field by field with a producer and a consumer
  per row, for both new screens.

## 6. Lanes and file ownership

| Lane | Executor | Owns | Must not touch |
| --- | --- | --- | --- |
| L0 contracts | orchestrator; current repair delegated to this executor | `docs/plans/2026-09-21-setup-three-screen-*.md`, `onboarding/setupFlow.ts`, the C1 fixture | product behavior |
| L1 shell + intro + motion | codex | `Wizard.tsx` (flow machine region), `steps/Welcome.tsx`, `onboarding/setupHandoff.ts`, `onboarding.css` shell/tier/CTA sections, `AccessTiles.tsx`, `ui/e2e/onboarding-fidelity/{fixture.tsx,support.ts,geometry.spec.ts,loop.spec.ts}`, i18n `onboarding.flow.*` and the four changed welcome/access strings | screen 2/3 internals |
| L2 providers screen | claude | new `onboarding/providers/**`, new `onboarding-providers.css`, `ImportKeysNotice.tsx`, the `AddApiKeyDialog` form extraction, `MigrationDialog`'s copy scope, new `ui/e2e/onboarding-fidelity/{provider-support.ts,providers.spec.ts}` and `hub-ownership.spec.ts`, i18n `onboarding.providers.*` / `onboarding.import.*` | `Wizard.tsx` outside the screen registry entry it adds last, screen 3 files, `onboarding.css`, L1's `fixture.tsx` / `support.ts` / `geometry.spec.ts` |
| L3 assistants + gate | codex | `AssistantRow.tsx`, `steps/AgentDetection.tsx`, new `onboarding/DefaultRouteDialog.tsx`, new `onboarding-assistants.css`, `Wizard.tsx` `complete()` region, `ui/e2e/onboarding-fidelity/connections.spec.ts`, i18n `onboarding.setup.*` / `onboarding.route.*` and the two changed setup headings | screen 2 files, `onboarding.css` shell sections |
| L4 scenarios + acceptance | codex | `tests/scenarios/auth_setup/**`, acceptance evidence, owner checklist | UI implementation |

Shared-touch files and the rule for each: `ui/src/i18n/{en,zh}.json` — key namespaces are
frozen by C1, each lane appends only inside its own namespace object, and the second lane
to merge rebases rather than resolving by hand. `Wizard.tsx` — L1 owns the flow machine,
L3 owns `complete()`, L2 owns only its registry entry. CSS — one file per screen so no two
lanes edit the same stylesheet; `onboarding.css` stays L1's.

Division of labor follows the default: claude takes the dialog-heavy, interaction-dense
provider screen; codex takes the shell/motion rigor and the readiness gate, where a wrong
predicate strands a first-run user.

## 7. Sequencing and PR plan

```
D1 (#2065, merged) ──► PR0 contracts ──┬─► PR1 shell+intro+motion (L1) ──► PR3 assistants+gate (L3) ──┐
                                       └─► PR2 providers (L2, parallel)  ─────────────────────────────┴─► PR4 scenarios+acceptance (L4)
```

- **PR0** (four documents/fixtures + type module and consuming test): commit the handoff doc, this plan, the C1 copy
  fixture, `contracts.md`, `setupFlow.ts` and its consuming test. Merging it first is what makes the parallel lanes safe, and it
  carries the planning sources in the repository; preserve any divergent primary-checkout edits.
- **PR1** (L1) is behavior-preserving infrastructure: the screen machine in the new shell,
  one shared action pair, the handoff, the tier extension, and the geometry specs that hold
  the anchor invariant. It ships without screen 2 and without changing readiness; the interim
  registry must not expose an empty providers step just because final C2 names one.
- **PR2** (L2) starts in parallel with PR1, because everything it owns — the stage, the
  dialogs, the capsule move, the shared API-key form extraction — is new files plus shared-owner
  adaptations, and it builds against C2 with its own test harness. It declares
  `requires #PR1 merged first`, and its one `Wizard.tsx` registry line plus its fixture
  integration land as a final commit after PR1 is on `master`. D10 must be decided and C6 bootstrap/runtime composition ratified first. No stacked PR: the base stays
  `master`.
- **PR3** (L3) forks after PR1 merges, using the orchestrator-ratified D4/D9 mapping. It rewrites the assistants
  screen and `complete()`, and PR1 rewrites the machine around `complete()` in the same
  file; two lanes editing one restructuring is a conflict that is not mechanical to resolve.
- **PR4** (L4) updates `tests/scenarios/auth_setup/catalog.yaml` and its harness cases for
  the new flow, and carries the acceptance evidence.
- Every PR: non-draft, `--base master` verified by reading the PR back, exact-head Codex
  review, zero unresolved threads, CI green, one durable `--forever` combined PR/CI watch
  with `--timeout 0` per `background-watch-hook`, and the review-loop circuit breaker.
  No merge without explicit owner instruction. The CI workflow name in this repository is
  `lint`; it produces every job a check list shows.

## 8. Validation

- Per lane: focused vitest suites; `npm run build`; `npm run validate:theme`;
  `typecheck:tests`; `ruff check` on any changed Python; `pre-commit run` on changed files.
- Behavior: the onboarding fixture harness (`ui/e2e/onboarding-fidelity`) extended to mock
  `/api/models/sources`, `/api/models/runtime/status` and `/api/models/agents/*` beside the
  existing scan/apply routes, asserting rules rather than copied numbers — anchor equality
  across three screens × two languages × six viewports, capsule dismissal leaving the action
  in place, stable-frame tab switching, handoff bail-out under reduced motion, phone
  scroll-to-top, no horizontal overflow.
- Cross-boundary: at least one end-to-end case that pierces shell → screen 2 → screen 3 →
  `complete()` with real (non-ASCII) data, since two mocked halves prove nothing about the
  boundary.
- Backend: `tests/scenarios/auth_setup` closed-loop cases updated in PR4, with scenario IDs
  visible in the tests and the PR bodies.
- Acceptance: the owner's cloud target, with a ~10-minute checklist derived from handoff §14
  (three-screen anchors, selection/import/badge, dialog stability, install/enable/upgrade,
  route ordering, gating, six viewports, dark ZH / dark EN / light ZH / light EN).
  Verification is behavioral — driving the deployed app — never reading version strings.

## 9. Risks

- **`AddApiKeyDialog` extraction.** 41 KB of implementation and 40 KB of tests sit behind
  the form screen 2 needs. Mitigation: extract the form without changing the Settings
  dialog's public behavior, keep its existing suite green as the regression fence, and run
  the whole models suite in L2.
- **Gate rewrite on a stateful instance.** The acceptance environment preserves state, so
  backends there may be in Direct mode with native credentials and no hub source. A
  hub-only gate could refuse entry to a working setup. C4 must state the mode handling and
  the degradation, and acceptance must cover both a fresh profile and the stateful one.
- **Shared-file integration.** i18n JSON and `Wizard.tsx` are touched by three lanes.
  Mitigation: frozen key namespaces, region ownership, per-screen stylesheets, rebase duty
  on the second merger.
- **Handoff correctness across tiers.** The snapshot reads live DOM class names owned by
  another lane; C3 makes those names a contract and `geometry.spec.ts` fails when they move.
- **Engine not installed on first run.** D3 reuses the approved automatic active-entry
  behavior. C6 requires authoritative support preflight before potentially installing paths,
  an admitted ensure-runtime phase for every mode, and separate native consent/mode adoption.
  Unsupported holds the normal flow with recovery/Back; a healthy running Hub stays usable.
  Helper `ok` alone cannot prove readiness.
- **Import custody and copy.** The handoff's preservation promise contradicts shipped takeover.
  D10 is a product decision/backend dependency; changing just the verb to “import” cannot fix it.
- **Controller not running.** D11: Hub reads are IPC-backed; a stopped application cannot
  satisfy them before bootstrap. D11's active provider-entry operation seeds/readbacks config
  before connection calls, avoiding legacy empty-Slack startup, then starts only a confirmed
  stopped controller and reads RPCs. No disabled-gateway branch needs bootstrap support. Runtime support checks must follow controller
  availability rather than converting an IPC failure to unsupported or ready.
- **Model-ranked routing.** No global route transaction exists. C6 defines exact targets,
  shared subset projection, divergent hydration, explicit catalog/hop repair and partial
  readback/retry. Existing chain writes have no CAS; concurrent external edits cannot be
  excluded. A shared reorder freezes only changed targets, preserving unrelated routes.

### 9.1 Review-loop record and current repair contract

The orchestrator independently fetched 20 threads: 19 resolved, one unresolved at dispatch.
Seven findings-bearing review heads: `d9c62aa2eb` (2), `cf3b47e8cd` (4), `5ea80ceeec` (4),
`de9c324872` (4), `f177ead2c2` (4), `616a0b539d` (1), `890014421f` (1): 20 findings total.
Counts use original review commits; review 5263128282 on published `890014421f` is terminal.
This is orchestrator-provided inventory, not a new executor GitHub read.

Diagnosis and scope decision: the repeated lifecycle/capability class became a scope error:
review inferred a second setup journey from separately existing Settings/Direct capability.
The owner explicitly resolved that boundary. Retract both automatic unsupported-to-Direct
fallback and retained capability-off setup compatibility, plus the proposed common bootstrap
expansion for that excluded path. Keep normal Hub D11 bootstrap, runtime support checks,
healthy Hub access, error/retry/Back and drafts. The five-file correction is contractual and
pure helper/test work; no runtime architecture, Settings/backend change or feature lane.

| Thread | Owner-ratified boundary / executable evidence |
| --- | --- |
| `PRRT_kwDOPbFPYs6kO76r` / `4059240601` | No Direct completion on unsupported hosts. Normal three-screen sequence and fresh Hub gates remain, with no unsupported installation; retry/Back/drafts and healthy existing Hub use are tested |
| `PRRT_kwDOPbFPYs6kPKSx` / `4059328091` | Disabled gateway is not a supported Direct setup branch. No provider skip or bootstrap expansion for it. Preserve disabled config, expose prerequisite/recovery, and leave post-setup Settings choices untouched; consumer verifies no install or completion |

Repeated classes audited together: exact Agent/menu-model/pair identity and readback (D4/D9);
enabled prerequisite → active-provider persistence → confirmed start → admission/health →
consent (D3/D11); complete scan groups and React functional state consumers (C2/C6); plan/type/
copy and authority consistency (C1–C6). D10 remains pending and unchanged. The known-by-design
ledger resolves only these product-boundary requests, not correctness/review/CI gates.

Current correction: ten `setupFlow.test.ts` tests pass, covering the fixed journey, disabled
prerequisite (deployment and saved intent separately), unsupported no-install/no-Direct entry, independent healthy Hub use, retry/Back,
drafts and late functional state updates. Changed-file ESLint, dedicated consuming-test
TypeScript validation, UI build, C1 parity/plural/placeholder checks (109 keys per locale) and
`git diff --check` pass. These consumers use synthetic runtime/connection evidence; they do
not prove feature E2E or backend authentication. The ten external frontend transport tests
also passed after adding saved-intent config validation. Prior unaffected evidence follows;
automatic-fallback tests are superseded and no shared-branch bootstrap proof is claimed.

Prior bounded-repair validation: 73 focused Vitest tests passed (`setupFlow`,
`MigrationDialog`, `gatewayAdoption`, `RuntimeNotStartedAction`); three hermetic backend
takeover tests passed (global/project cleanup, shadowed-key protection, post-exposure
recovery). UI build, changed-file ESLint, a dedicated TypeScript check of the consuming test,
C1 validation (locale/plural/placeholder parity) and `git diff --check` passed.
These are not proof of screen-2 runtime orchestration or a completed route implementation.
The technical-closure pass passed 61 UI tests (`setupFlow`, `routeChainDraft`,
`RouteChainDialog`) and five existing Agent-model prefill tests, plus seven temporary
composed backend fixtures outside the PR scope: real config POST + control startup readiness, preservation of existing
config, and the five requested routing cases. Engine/process collaborators are synthetic;
this proves backend orchestration feasibility, not the frontend call chain, a shipped feature,
live inference or visual design. The bootstrap correction passed 10 separate temporary frontend tests with a
consumer of real ApiProvider/serializer/apiFetch to verify JSON/CSRF, uncached readback,
error handling and absent cache/convergence side effects. The two layers do not claim E2E.
L2/L3 must add consuming coverage with the actual feature owners. Current Hub-gate coverage
retains the original React state consumer. Orchestrator spot-checks the repaired diff and
a consuming test before delivery continues.

## 10. Decisions, provenance and remaining binding conditions

The owner explicitly reaffirmed Model Hub setup with disabling only in Settings afterward;
this supersedes both orchestrator interpretations of Direct setup compatibility. It does not approve all D2–D9 decisions or D10.
Existing product contracts remain reusable evidence; routine technical choices belong to
the orchestrator. The user's confirmation
before feature implementation and explicit authority before #2082 merge remain outstanding.

| ID | Provenance / current choice |
| --- | --- |
| D1 — #2065 | Owner-authorized merge, squash `31c4e831b`; no implied #2082 merge/feature authority |
| D2 — shared key form | Orchestrator decision: extract existing `AddApiKeyDialog` form into setup's stable frame; preserve Settings behavior/tests |
| D3 — runtime setup | Existing approved `model-hub-native-takeover.md` flow, adopted by orchestrator: automatically prepare runtime on active entry; active-provider config/bootstrap, C6 support preflight and admitted ensure/install/start/readback for all modes; unsupported recovery preserves the default Hub requirement and healthy running Hub. No enable/install-confirmation dialog and no automatic credential takeover |
| D4 — shared model route | Orchestrator-ratified bounded technical interpretation (not owner feature approval): one designated builtin setup Agent per installed/enabled backend, explicit named target if no builtin, exact saved menu model, shared order projected through each target's reviewed exact-hop membership. Preserve other menu-model routes, default and Agent models. C6 specifies hydration, repair and partial-write retry |
| D5 — setup prerequisite | Owner-ratified Hub-only setup; unsupported/error/disabled config never creates Direct completion. Disabling belongs to Settings afterward. C4 retains correlated Hub readiness and healthy Hub independent of installation; C2 preserves config, retry/guide/Back and drafts. Specific known-by-design resolution, no gate waiver |
| D6 — handoff §13 | Concise recommendations: destinations logo/name, reference English wording, capsule restoration, dashed Add-more. No native Pencil inspection or design-source synchronization claimed |
| D7 — vendor list | Orchestrator decision: shipped catalog/order, no prototype Cohere; no backend catalog change |
| D8 — copy scope | Orchestrator decision: setup copy scope separate from Settings; migration portion remains provisional on owner D10. Controlled state/copy adaptation is future L2 work |
| D9 — row identity | Orchestrator-ratified technical interpretation with D4. Model-ranked `(source_id, model_id)` remains required. Unapproved source-ranking substitution withdrawn. Exact menu model vs upstream model, membership projection and successful manual-override readback specified with D4; no arbitrary Agent-model changes |
| D10 — import custody | **Only product choice. Recommended:** reuse approved complete-group takeover with exact consequence + Not now / Start migration, preserving unrelated settings and setup API-key-only scope; remove copy-only/unchanged-connections/atomic-rollback promises. **Literal handoff alternative:** copying plus explicit native coexistence/Hub-admission redesign; a copy-only endpoint alone cannot pass `set_agent_mode`'s guarded native-row rejection. Backend/custody expansion remains unapproved. C1 supplies one coherent provisional takeover version |
| D11 — controller bootstrap | Orchestrator-ratified corrected sequence: active provider entry with enabled capability and saved runtime intent → CSRF-aware `apiFetch` POST `{}` with JSON Content-Type and HTTP/body validation → uncached GET parse installed in shell config state → successful connection read proving persisted config → confirmed stopped/start/readiness → Hub runtime sequence. `mutateConfig([])` rejects before transport; no change to that validator. Direct fetch has no ApiContext cache/convergence side effects. Unknown writes stay pending until existence is proved; GET defaults alone cannot prove persistence. Separate frontend transport and backend fixture evidence, not E2E |

D4/D9 fixture results: exact same-source model pairs persist distinctly; disjoint native
Claude/Codex subscriptions retain subsets; editing one builtin route leaves a custom Agent's
different model/route intact; fresh Agents receive server recommendations and OpenCode can
admit its exact saved menu id before explicit upstream-hop mapping; divergent chains are not
rewritten on open; a second target's failed write leaves the first confirmed and retry skips
it. The composed fixture contains the proposed projection coordinator and calls real service
primitives; it is not production L3 code. Existing primitive tests cover model prefill, exact
route guards and unknown-write readback independently.

D10 determines only migration-specific copy, consent/application and receipt/recovery wiring.
C2's full scan + backend-group draft and functional setter remain usable for the recommended
path. If the owner chooses literal copying/coexistence, reconcile those migration interfaces
before dispatch; do not treat this draft as a frozen backend contract for that alternative.

## 11. Out of scope

Desktop shell loading page; Model Hub service semantics and any backend routing redesign;
production credential-scan behavior beyond reusing scan/apply; macOS menu bar and other
OS-layer work; IM platform configuration in onboarding (already moved to Settings —
`SetupPlatformRecovery` remains only as a recovery path); handoff §11's adjacent changes,
which landed through #2033/#2047/#2050/#2051/#2058/#2067/#2068/#2069 and are re-verified
here only where they touch setup.
