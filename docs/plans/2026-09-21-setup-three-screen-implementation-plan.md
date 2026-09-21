# Setup three-screen rebuild — implementation plan

Status: **approved and in execution** (2026-09-21). The owner merged #2065 through this
session at 09:31 and authorized the remaining work; D1 is resolved and D2–D8 in §10 are
adopted as recommended. Binding interface detail lives in
`docs/plans/setup-three-screen/contracts.md` (C3–C6) and
`docs/plans/setup-three-screen/copy-contract.json` (C1);
`ui/src/components/onboarding/setupFlow.ts` is C2.

Sources, all read at the SHAs named here:

- Handoff spec: `docs/plans/2026-09-21-setup-three-screen-handoff.md` (currently
  untracked in the primary checkout; PR0 commits it).
- Approved contract: `docs/plans/2026-09-19-desktop-connection-settings-alignment.md`
  (worktree `desktop-alignment-contract-20260919`).
- Interactive reference: Show session `ses36vg559de2` — `pages/index.tsx`,
  `GatewaySetup.tsx`, `KeyImport.tsx`, `SetupHandoff.ts`, `setup-flow.css`,
  `gateway-flow.css`.
- Design source: `../avibe-docs/design_desktop.pen`, board index in the handoff §12.
- Product baseline: `origin/master` at `31c4e831b`, which is `a07acee02` plus the merged
  #2065. Every claim below was read from `origin/master` rather than from the primary
  checkout, which sits at an older `2791092d8`.

## 1. Goal

Replace today's two-step `/setup` (welcome → assistant connection) with the approved
three-screen flow — collaboration highlights → model providers → assistant enablement —
inside one sidebar-free shell whose primary action never moves, and whose readiness gate
is expressed in Model Hub terms rather than per-assistant connection terms.

The design is not a new surface bolted onto the old one: screen 1 already ships, screen 3
is a restructure of what ships, and screen 2 is a new composition of Model Hub APIs that
already ship. The work is mostly re-owning existing behavior, which is why the reuse
inventory in §3 is the first thing a lane reads.

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
  capability projection (`capabilities.model_hub.enabled`) still exists and still needs
  a defined degradation (D5).
- Acceptance runs on the owner's designated cloud target
  <https://avibe-cloud-e2e-app.avibe.bot> (workspace `AGENTS.md`, 2026-09-18). It answers
  `401 remote_access_login_required` unauthenticated, so its capability flag, gateway
  runtime health and backend modes must be read with credentials at acceptance time, and
  deploying to it needs explicit owner authority.

## 3. Reuse inventory (read before writing anything)

| Need | Existing owner on `origin/master` | State for this round |
| --- | --- | --- |
| Setup shell, brand, language | `ui/src/components/Wizard.tsx` (`.onboarding-shell`), `visual/BrandLogo.tsx`, `LanguageSwitcher.tsx` | shipped; design lockup refined by #2065 |
| Screen 1 story, cards, wires, loop caption | `steps/Welcome.tsx`, `onboarding/CollaborationStory.tsx`, `onboarding/collaborationTimeline.ts`, `onboarding/motion.ts` | shipped (#2048/#2052/#2065); adapts to the new shell |
| Six access entries | `onboarding/AccessTiles.tsx` | shipped; needs `hidden`/`inert` instead of unmount |
| Assistant cards, install/detect/enable | `steps/AgentDetection.tsx` (606 lines), `onboarding/AssistantRow.tsx`, `settings/BackendLifecycleChip.tsx` | shipped; restructured by screen 3 |
| Per-assistant connection dialog | `onboarding/BackendConnectionDialog.tsx` | shipped; **leaves onboarding** (handoff §13.5), stays for Settings |
| Discovery capsule + dismissal memory | `onboarding/ImportKeysNotice.tsx`, `lib/modelHubMigrationDismiss.ts` | shipped with the approved copy; moves to screen 2, gains a reserved slot |
| Import batch dialog | `settings/models/MigrationDialog.tsx`, `migrationScan.ts` (`importableKeys`, `isImportableKey`, `scanMigrationWhenEnabled`) | shipped; reused as screen 2's import dialog |
| Add API key: vendor picker, observe → create | `settings/models/AddApiKeyDialog.tsx` (41 KB impl, 40 KB tests), `apiKeyVendors.ts`, `vendorMarks.ts`, `vendorGlyph.tsx` | shipped; needs its form extracted so a tabbed stable frame can host it (D2) |
| Subscription sign-in | `modelsApi.startOAuth/getOAuthStatus/submitOAuth/cancelOAuth`, `settings/models/OAuthConnectDialog.tsx`, `settings/oauth/OAuthFlowParts.tsx` | shipped; subscription tab drives it |
| Added providers, masked keys, vendor identity | `modelsApi.listSources()`, `Source{vendor, display_name, masked_credential, account_label, state, models}` | shipped; screen 2's connected cards |
| Detected candidates with vendor, mask and source label | `modelsApi.scanMigration()`, `MigrationItem{vendor, display_name, masked_credential, backend, notes_key, proposed_action}` | shipped; screen 2's detected slots and import rows |
| Per-backend route read/write | `modelsApi.getAgentSources/putAgentSources/reorderAgentChains/getAgentChains/previewAgentChain`, `settings/models/RouteChainDialog.tsx`, `routeChainDraft.ts` | shipped; **there is no global route object** — see C6 and D4 |
| Gateway engine lifecycle | `settings/models/gatewayAdoption.ts` (`resumeGatewayAdoption`) over `runtimeLifecycle.ts` (`resumeInstallAndStartRuntime`, `installRuntimeUntilSettled`, `installAndStartStep`, `runtimeCanAttemptInstall`), plus `RuntimeNotStartedAction`, `InstallGatewayDialog`, `EnableGatewayDialog` | shipped, Settings-only today; onboarding never installs the engine — see D3 and C6 |
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
  which makes state preservation structural rather than a per-screen effort;
- renders **one** primary action and **one** Back button itself, so the "coordinates and
  size never change" invariant is a property of the tree instead of an agreement between
  two stylesheets. The active screen feeds `{label, disabled, busy}` and exposes
  `activate()` through a ref — the reference's `onActionChange` + `GatewayHandle` shape;
- owns the flow state that must survive navigation (pending selection, imported count,
  route order, per-assistant install/enable state);
- moves focus to the screen's `h1` (`tabIndex={-1}`) on every screen change and scrolls
  to top on phones;
- keeps the existing recovery surfaces (`SetupPlatformRecovery`, `SetupModelRecovery`)
  and the `setup_completed` write.

### 4.2 Screen 1 — collaboration highlights

Mostly shipped. Delta: the CTA leaves `Welcome.tsx` for the shell; the CLI detection that
`Welcome.start()` runs today (`api.detectCli` per assistant) stays on the transition and
is re-verified when screen 3 mounts; `AccessTiles` becomes `hidden`/`inert` off screen 1
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
  mask. Data comes from `Source.masked_credential` / `MigrationItem.masked_credential`,
  never from client-side masking.
- **Card state.** added/selected → mint wash + mint border + restrained shadow + one
  check on the right (desktop: vertically centred; phone: top-right 6px/14px); not added
  → ordinary border + plus. No "已添加/Added" text on a card. Detected candidates start
  selected; clicking toggles `aria-pressed`, and selection means "in this import batch".
- **Wires and gateway card.** 1.5px accent, round caps, r2.5 endpoint dots, pulse on
  arrival; geometry re-measured from real card centres on resize. Gateway card is one
  provider-card wide, 72 high (phone: min 72), two centred lines, no logo, and takes a
  connected treatment once a provider is ready.
- **Summary line** (`aria-live`): pending selection / added / error / none, with the
  exact copy in handoff §4.
- **Capsule.** `ImportKeysNotice` moves here, keeping its persisted non-nagging
  dismissal and its real candidate count, and gets a reserved slot so dismissing it
  cannot move the primary action.
- **Import dialog.** `MigrationDialog` with `eligible={isImportableKey}`: checkbox rows,
  all selected by default, empty selection disables the action, one atomic
  `applyMigration(ids)` batch, cumulative completed count, remainder re-entry, failure
  keeps selection and offers retry, focus returns to the trigger. Import completion is
  not assistant readiness.
- **Add-source dialog.** One stable frame (C5) with three methods: Detected (only when
  unlisted candidates exist; multi-select; already-added rows disabled and marked),
  Subscription (OpenAI/ChatGPT and Anthropic/Claude through the hub OAuth flow, driven by
  real capability rather than a fixture list), API Key (the extracted vendor form:
  eight primaries in catalog order, `更多服务商` collapsed holding the catalog's
  remainder, empty input disables the action). The key field keeps the extracted shared
  form's own treatment; the prototype's `sk-demo-example` placeholder and its
  "do not enter real credentials" line are preview-only and ship nowhere (C1's rules,
  handoff §15). Two-phase progress copy in production wording; failure keeps input and
  selection and turns the action into Retry.
- **Primary action states.** `导入 N 项并继续` / `重试导入 N 项并继续` / `继续，选择 AI 助手`
  / `添加订阅或 API Key` (opens the add dialog) / `正在连接…` `正在检查连接…` disabled.
- **First-entry sequence** (~1.1s): cards → inbound wires → gateway → outbound wires →
  destinations. The reference's reset/replay control is preview-only and does not ship.

### 4.4 Screen 3 — choose and enable assistants

Card becomes: identity row (logo 30, name 17/600, role 11/700 trailing, enable Switch
only when installed) → divider → status pill → description → action row.

- Status pill: 已启用 / 未启用 / 未安装 / 安装中 / 升级中, with dot or spinner — exactly
  one loading indicator per card while an operation runs.
- Description follows the state: not installed / enabled / installed-not-enabled.
- Actions: `立即安装` (outline + Download, spinner `正在安装…`) → `api.installAgent` then
  re-detect; default-model chip (`默认模型` + model name + ChevronRight) → route dialog;
  `已安装，未启用` static row (enabling belongs to the identity Switch); `立即升级`
  (outline + ArrowUpToLine) through `BackendLifecycleChip`'s `onVisual`, coexisting with
  the enabled state.
- Enabled card wears the same mint treatment as screen 2's added cards.
- **Default model route dialog**: shared ordered list, `提供商 logo + 模型名 + 服务名 ·
  首选/备用 N`, up/down with first/last disabled, single-route note, footer
  `添加模型来源` (→ screen 2) + `完成`, focus back to the chip.
- **Per-assistant connection dialogs are removed from onboarding.** `BackendConnectionDialog`
  stays for Settings; setup no longer opens it. This is what forces the gate rewrite below.
- All-uninstalled case: three `立即安装` cards, primary action disabled, and the user can
  go back to screen 2 to add sources first.

### 4.5 Readiness gate and completion

`Wizard.complete()` today requires a per-backend `entry_eligible` connection, starts the
service only from a confirmed stopped state, filters OpenCode agents by
`readOpencodeSetupRoutes`, preserves a usable default Agent, and writes `setup_completed`
last. The new gate keeps that skeleton and replaces the readiness input: at least one
ready hub source, a default route set, and at least one assistant installed **and**
enabled. `WizardCompletion.test.tsx` invariants are re-pinned deliberately, not silently
(C4). The recovery paths and the "only a confirmed stopped state may start the service"
rule are unchanged.

### 4.6 Dialogs and the stable frame

Contract §7 applies to all three screen-2 dialogs: one frame per dialog across its tabs
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

Extend #2065's tier system to all three screens: ≥1600×950 (column 1200, h1 50, gateway
80, entries 830), compact `max-height:1000 && ≥761`, 761–1020 (`calc(100% - 48px)`),
≤760 (stacked cards, no circuit or loop caption, narrow gateway column, two-row capsule,
full-width stacked CTA), ≤360 (tighter capsule and cards), `max-height:800` (action 52,
entry circle 48), and elastic top whitespace on tall screens.

### 4.9 i18n

C1 (`docs/plans/setup-three-screen/copy-contract.json`) is the frozen copy table and the only
source of key names; handoff §8 is where its strings came from. New namespaces:
`onboarding.flow.*` (L1), `onboarding.providers.*` and `onboarding.import.*` (L2), and new
`onboarding.setup.*` / `onboarding.route.*` leaves (L3). Six shipped strings change: the four
welcome/access ones are L1's, the two setup headings are L3's. The capsule keeps
`settings.models.importNotice.*` for the discovery sentence, help link and dismissal label,
and the import dialog keeps `settings.models.migration.{blocked,errors,notes,source}.*` for
the explanations it renders, while its own chrome — title, description, action, progress,
completion, remainder — reads `onboarding.import.*`. That split is D8's copy scope. No
display string is hardcoded in a component.

### 4.10 Accessibility

Focus to `h1` on screen change; `aria-live` on summary, status pill, capsule and progress;
`aria-pressed` on cards, tabs and provider buttons; labelled Switches and icon buttons;
tooltip via `aria-describedby` + `aria-expanded`; dialog focus trap with focus restored to
the trigger; Escape and outside click close menus, tooltips and dialogs (the import dialog
is not closable mid-progress); non-current screens `hidden`/`inert`.

## 5. Contracts frozen by PR0

All six are committed by this PR, so every lane forks against one shared reference:

- **C1 Copy** → `docs/plans/setup-three-screen/copy-contract.json`. 103 leaves with exact
  key names, the six shipped strings they change, and the thirteen they reuse untouched;
  validated for zh/en parity, complete `_one`/`_other` families on every `{{count}}`
  string, no nested-before-literal collision and identical placeholders per key. Key names
  are an allocated-id namespace, which is why they are frozen rather than left to a lane.
- **C2 Flow interface** → `ui/src/components/onboarding/setupFlow.ts`, with
  `setupFlow.test.ts` holding the two decisions a screen cannot make locally: which screens
  run and where Back goes from each.
- **C3 Geometry and DOM hooks** → `contracts.md` §C3: the single shell-rendered anchor
  pair, the reserved capsule slot, the class names the handoff snapshots read across
  screens, and the tier rule (consume `--ob-*`, restate inside the existing bands, never
  invent a new one).
- **C4 Entry gate** → `contracts.md` §C4: the three inputs and their producers, what stays
  unchanged in `complete()`, what is removed, the hub-disabled degradation, and which
  `WizardCompletion.test.tsx` invariants are deliberately re-pinned.
- **C5 Stable frame** → `contracts.md` §C5.
- **C6 Data mapping** → `contracts.md` §C6, field by field with a producer and a consumer
  per row, for both new screens.

## 6. Lanes and file ownership

| Lane | Executor | Owns | Must not touch |
| --- | --- | --- | --- |
| L0 contracts | pm (this session) | `docs/plans/2026-09-21-setup-three-screen-*.md`, `onboarding/setupFlow.ts`, the C1 fixture | product behavior |
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

- **PR0** (docs + two contract files): commit the handoff doc, this plan, the C1 copy
  fixture and `setupFlow.ts`. Merging it first is what makes the parallel lanes safe, and it
  also clears the untracked-plan defect that blocks the primary checkout's fast-forward.
- **PR1** (L1) is behavior-preserving infrastructure: the screen machine in the new shell,
  one shared action pair, the handoff, the tier extension, and the geometry specs that hold
  the anchor invariant. It ships without screen 2 and without changing readiness.
- **PR2** (L2) starts in parallel with PR1, because everything it owns — the stage, the
  dialogs, the capsule move, the shared API-key form extraction — is new files plus two
  Settings files, and it builds against C2 with its own test harness. It declares
  `requires #PR1 merged first`, and its one `Wizard.tsx` registry line plus its fixture
  integration land as a final commit after PR1 is on `master`. No stacked PR: the base stays
  `master`.
- **PR3** (L3) forks after PR1 merges rather than beside it. It rewrites the assistants
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
- **Engine not installed on first run.** Screen 2 assumes a gateway that a fresh machine may
  not have. D3 adopts the automatic path and C6's gateway-engine row names the shipped
  helper that performs it, so the remaining risk is a host whose manifest resolution is
  `unsupported`, which surfaces as a failure rather than a retry loop.

### 9.1 Review-loop record

PR0 tripped the circuit breaker in `AGENTS.md`: one root-cause class — Model Hub behavior
asserted in a contract from type signatures rather than from the shipped implementation —
appeared on two reviewed heads. `d9c62aa2` carried one P1 of that class (a shared route
order written verbatim to every backend, which `invalid_source_order` rejects); `cf3b47e8`
carried four more (Direct-mode eligibility read before the mode switch, an install helper
that never starts the engine, three uncorrelated readiness checks, and a route preference
with no producer). Per the standard, patching stopped and the class was diagnosed instead.

Scope decision (orchestrator, 2026-09-21): C4 and C6 stop restating shipped semantics and
name the shipped owner of each behavior — `gatewayAdoption.resumeGatewayAdoption`,
`runtimeLifecycle.resumeInstallAndStartRuntime`, `eligibility.eligibilityOf`,
`SourceOrderDrawer.save`, `BackendSupplyModeCard.setMode`, `readOpencodeSetupRoutes`'
runnable-hop read — and prescribe only what setup genuinely adds: the shared-preference
projection, the hydration rule, the correlated gate, the setup copy scope. The alternative,
stripping the behavioral detail and letting each lane rediscover it in code, was rejected:
these findings are precisely the traps a lane would otherwise hit late, and two lanes would
not rediscover the same answer. The standing rule for L1–L4 is that a contract row describing
shipped behavior cites its owner module and says "reuse unchanged" instead of paraphrasing
it, and that any new behavioral claim is read out of the implementation before it is written
into a contract.

Round 3 (head `5ea80cee`) proved the diagnosis rather than contradicting it: four more
findings, three of them members of the same class that the round-2 sweep had missed — this
document's own D3 and reuse table still named the install-only helper, §4.3 still told a lane
to ship the prototype's `sk-demo-example` placeholder against C1's explicit rule, and C4's
routed condition read the backend-level `selected_model_id`, which is null whenever the
default Agent belongs to another backend and so rejects a runnable candidate on a different
one. The fourth was C2 contradicting itself: returning the full sequence for an unread
capability is not waiting, so a fast click could enter a screen the resolved read then
removes.

The scope decision therefore tightened as pre-committed. Contracts now carry three things
only: interfaces and names, ownership boundaries, and the deltas no shipped module owns.
Everything else became an owner pointer with "reuse unchanged", stated as a rule at the head
of C6, because a paraphrase of shipped mechanics is a second place to be wrong and this PR
kept finding new ones. The two changes that are not deletions: C4's routed condition now
correlates `listVibeAgents()` candidates with their `named_agents` entry, and C2 gained
`SetupCapability` with `setupNavigationReady` as the explicit wait, so the shell cannot read
a returned sequence as permission to navigate. Both briefs were swept for the same members.

## 10. Decisions

D1 was settled by the owner on 2026-09-21 09:30 and executed at 09:31. D2–D8 are adopted
as recommended by the orchestrator under the same authorization; each is reversible and
contract-preserving, and any of them can still be overruled before its lane merges.

- **D1 — merge #2065 first. Resolved: merged** as `31c4e831b`, after verifying the
  exact-head Codex pass, zero unresolved threads and 18/18 CI at head `25de7e7d6`.
- **D2 — how screen 2 hosts the API-key form. Adopted: extract the form body out of
  `AddApiKeyDialog`** into a shared component used by both the Settings dialog and the setup
  tab, with the existing 40 KB suite as the regression fence. Rejected: a setup-only key form
  (duplicates a heavily specified flow) and opening the Settings dialog from setup (breaks
  the tabbed stable frame).
- **D3 — gateway engine lifecycle in onboarding. Adopted: automatic, through the shipped
  combined path.** Entering screen 2 goes through `resumeGatewayAdoption`, which ensures the
  engine with `resumeInstallAndStartRuntime` — install AND start — and then scans that
  backend's candidates, reporting progress and its classified failure inside the gateway
  card. The install-only helper is not the path: a fresh runtime that classifies as installed
  but is `not_started` would leave screen 2 talking to a stopped gateway. It downloads and
  installs the managed runtime during first run, which is why it was put to the owner rather
  than assumed. Rejected: an explicit user action (a dead end on first run) and an error
  pointing at Settings.
- **D4 — the shared default route. Adopted: one shared preference, projected per backend.**
  The contract has no global route object; ordering is per-backend (`AgentSupply.sources.order`,
  `putAgentSources`) and is that backend's ELIGIBLE subset, so the dialog edits one preference
  and each write projects it through `eligibilityOf`, skipping a backend whose projection is
  empty and saying so, switching a backend to `hub` mode where it is still Direct, and
  surfacing a guard response for confirmation instead of forcing it. Writing the identical id
  list everywhere is not an option: the server rejects a foreign or ineligible source with
  `invalid_source_order`, which is exactly what a native ChatGPT or Claude subscription shared
  across three clients would do. The dialog's approved line 「所有已启用的助手共用」 stays as
  the owner wrote it and the skip notice carries the per-backend truth beside it; if that
  reading is not what the owner wants, the copy is the thing to change, not the projection.
  Rejected: per-assistant editing only (contradicts the shared-route design) and a new
  backend-level global route (a redesign the handoff puts out of scope).
- **D5 — gate rewrite and hub-disabled degradation. Adopted.** The hub-based predicate in C4
  replaces per-backend connection readiness, the affected `WizardCompletion.test.tsx`
  invariants are re-pinned openly, and an explicitly disabled capability skips screen 2 while
  screen 3 keeps today's per-assistant connection actions — one documented degradation, not a
  third flow shape.
- **D6 — handoff §13 items 1–4. Adopted as written**: destinations show logo + name only;
  English follows the reference (`Model Hub` / `Connect models once. Model Hub handles
  routing.`); the capsule is restored on screen 2; the Add-more frame is dashed. The design
  file is synced afterwards.
- **D7 — the "More providers" list. Adopted: the real shipped catalog remainder** (Z.AI,
  Mistral, Groq, Together, Fireworks) plus 自定义, partitioned by position in
  `vibe/data/api_key_vendors.json` and never re-sorted in the browser. The prototype's Cohere
  is a fixture; adding it means editing the backend-owned catalog, which this round does not.
- **D8 — 导入 versus 迁移, found while freezing C1. Adopted: setup says 导入/import, Settings
  keeps 迁移/migrate.** The shipped `settings.models` vocabulary is fenced by total-scope copy
  redlines, and the handoff's setup copy says 导入 throughout, so `MigrationDialog` gains a
  copy scope instead of one side being renamed, with a test holding the Settings rendering
  unchanged. Unifying the two words app-wide stays a separate product-voice decision,
  recorded here so it is not lost.

## 11. Out of scope

Desktop shell loading page; Model Hub service semantics and any backend routing redesign;
production credential-scan behavior beyond reusing scan/apply; macOS menu bar and other
OS-layer work; IM platform configuration in onboarding (already moved to Settings —
`SetupPlatformRecovery` remains only as a recovery path); handoff §11's adjacent changes,
which landed through #2033/#2047/#2050/#2051/#2058/#2067/#2068/#2069 and are re-verified
here only where they touch setup.
