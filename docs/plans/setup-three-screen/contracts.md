# Setup three-screen — contracts C1–C6

Status: **PR0 repaired interface proposal**, audited against head
`616a0b539dd4e25cc3f42976277ef7a2a218098d`. The handoff remains unchanged user source.
The approved `model-hub-native-takeover.md` supplies runtime/custody precedent; the
orchestrator adopts its automatic runtime preparation and the routine decisions in plan §10.
There is no owner response adopting D2–D9, and #2065 merge authority does not authorize
#2082 merge or feature implementation. Current repair authorization covers PR0 only.

C2–C6 specify concrete technical mappings; C1's migration entries and C6's setup apply path
remain **provisional on D10**, the conflict between the handoff's copy-only promise and
approved complete-group takeover. D4/D9 is orchestrator-ratified as a bounded technical interpretation, not owner feature
approval. D11 is also orchestrator-ratified after separate frontend/backend verification of the
corrected transport boundary; it is not a new product question. No whole-program
freeze or feature dispatch is claimed before that reconciliation and the requested owner
confirmation. A lane reports gaps instead of inventing a parallel contract.

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
2. **Import wording is provisional on D10.** The fixture contains one coherent proposed
   takeover version, marked by `_provisional_copy`; it removes copy-only and unchanged-state
   claims. The original handoff is preserved in its own file. These marked strings cannot
   ship until the owner chooses custody behavior. The confirmation uses the exact approved
   Chinese consequence sentence and **Not now / Start migration**. `MigrationDialog` is the
   takeover owner; setup copy scope and controlled selection are required L2 adaptations,
   not existing props. Settings keeps its current rendering. Counts describe migration items;
   an `applied` receipt alone does not establish assistant readiness.
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
  consent groups; `routeOrder` holds `(source_id, model_id)` rows, never source IDs alone.
  Neither is a second source of truth for persisted readiness.
- The shell renders the single primary and Back actions. Each `onActionChange` and
  `onNavigate` callback is bound to the emitting screen and activation; the shell rejects
  inactive/stale publications and clicks only the current `SetupScreenHandle.activate()`.
  Clear the previous action on navigation; disable it during capability reads, handoffs,
  and pending mutations. `busy` also disables clicking. `activate()` must reject re-entry.
- All registered screens remain mounted, `hidden` + `inert` when inactive. This preserves
  drafts but does NOT suspend effects. Screens gate polling, authorization, scans and
  animations on `active` (and dialog open state). A hidden screen must not start installs,
  mode changes or other mutations. Already-started operations retain one owner; their
  completion may reconcile state with functional updates, but cannot navigate, steal focus
  or publish the active action. Ignore stale read completions using an activation/request
  generation, not unmount cleanup alone. Leaving and returning never replays a mutation.
- Server facts (CLI presence, backend enablement, sources and routes) are re-read on active
  entry and after mutations. Preserve unconfirmed drafts across those reads; C6 defines scan
  reconciliation, route targets and preservation of dirty target baselines. `setupFlow.test.ts` consumes the actual
  props with React state and checks navigation plus a late update, not just initial values.

`setupCapability(boolean | null)` retains the deployment decision as
`'pending' | 'enabled' | 'disabled'`. Use the shell's validated fresh config boolean, never
`useModelHubCapability()` (it collapses failures to false) or an unvalidated missing field
passed to `modelHubEnabledFromConfig`. Host install support is a **separate fact**, learned
only after D11 controller bootstrap in active providers. Do not block that entry on support.

The shell owns a `RegionRead<RuntimeDependency>` and passes its single derived
`policy = setupPolicy(capability, runtimeRead)` through **`SetupScreenProps.policy`** to L2/L3.
Use `readyRegion` only for a successful, validated runtime response; null, malformed payloads,
transport failure and non-2xx/5xx become loading/unread, never unsupported. Retry uses
`beginRegionRead`/`failRegionRead` so old observations may preserve layout without becoming
fresh again. Ignore obsolete request/activation completions. `SetupPolicy` holds capability,
`runtimeRead` (`pending`/`retry`/`ready`), `installSupport` (`unknown`/`admitted`/`unsupported`)
and `hubRunning`. These are observations, not saved mode or a readiness verdict.

The executable `setupCanAttemptInstall(policy)` requires enabled capability, a fresh ready
read and admitted support; L2 uses it before any potentially installing operation.
The projection reuses **`runtimeCanAttemptInstall`** and **`runtimeIsRunning`**. Only manifest
`resolution === 'unsupported'` refuses installation; `unresolved` is admitted and must not be
confused with an unread request. Running means health `ok` or `degraded`, independently of
install support. Neither browser platform, missing assets guessed by the UI nor a 5xx is an
unsupported-host producer. The shell owns policy calculation; screens consume it rather than
inventing independent capability/support gates.

| Effective policy | Sequence / candidate configuration and readiness owner |
| --- | --- |
| capability pending | Intro primary held; support is not guessed |
| capability enabled, runtime unread/error | Intro can enter providers for D11/read. Keep current screen on read failure; downstream Continue/entry held, Retry and Back available |
| capability enabled, install admitted | Three screens; Hub setup and correlated Hub readiness (C4). Admitted does not itself mean running |
| capability enabled, authoritative unsupported, engine not running | `intro → assistants`; existing Direct candidates use Direct connections/readiness. Persisted Hub candidates keep Hub recovery and cannot pass as Direct |
| capability enabled, authoritative unsupported, engine running | Keep providers and existing Hub reads/routes; allow existing Direct candidates too. Installation and potentially installing mutations remain blocked; a working Hub is not disabled |
| capability explicitly disabled | `intro → assistants`; keep shipped connection actions/readiness. Persisted Hub mode still means Hub recovery, never presumed native credentials |

`setupScreenSequence(policy)` and `setupCurrentScreen(policy, requested)` are consumed together
in the shell's **same state update/render**, not corrected in a later effect. On a late
unsupported/not-running result while providers is current, commit assistants as current,
cancel a pending handoff, clear the obsolete action/ref publication and render exactly one
active screen with the new sequence. Preserve `SetupFlowState` and mounted dialog drafts.
If current is already intro/assistants, keep it. Map every navigation request (including an
old Add-source request) through `setupCurrentScreen`; removed providers cannot be re-entered.
Back uses `setupBackTarget` on the effective sequence, so assistants goes to intro. Advancing
again stays on that sequence rather than bouncing into providers. Re-read support in place
on an explicit retry through **`SetupScreenProps.onRetryRuntime`**, owned by the shell's
existing bootstrap/read path; no new dialog or parallel state machine. If new evidence
restores providers, keep the current valid screen; Back or Add source can reach it normally.

`setupNavigationReady(policy,current)` permits intro to reach bootstrap after capability
settles; later Continue is held unless runtime is freshly read or capability is explicitly
off. This helper does not replace action-specific busy/readiness rules. Back and the active
owner's Retry remain operable on errors (the shell may publish `common.retry` as its shared
CTA). A stale unsupported snapshot can hold the reduced layout during retry but cannot
permit completion until refreshed. A blocked Hub engine does not block an otherwise ready
Direct candidate once fresh unsupported evidence establishes that path. The consumer calls
`setupCandidatePath(policy, connection.supply_mode)` to choose the C4 owner; it never changes
saved modes or credentials. `pending` cannot pass entry; `hub-recovery` keeps recovery access.

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
An inactive `hidden` root has no measurable box. L1 must temporarily lay out the incoming
root invisibly and inertly for measurement, without activating its effects or action feed,
then switch activity at handoff completion. Restore `hidden` after cancellation; the settled
state still has exactly one active screen. L1's geometry test must exercise this real path,
not measurements taken with every screen already visible.

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

L3 first uses the shell policy and each candidate's fresh persisted `supply_mode` to choose
`setupCandidatePath`. For the **Hub** path, build the available set from fresh
`listVibeAgents({ cache: false })`: enabled, non-archived named Agents, joined by **backend
and Agent name** to backend connection/CLI reads and `AgentSupply.named_agents`.

| Condition for the same candidate | Authoritative read / rule |
| --- | --- |
| installed and backend enabled | `api.detectCli(cli_path)`, `AgentSupply.cli_present`, `getBackendConnection(backend).enabled`; stale or failed reads cannot prove readiness |
| Hub runtime running | `getRuntimeStatus()` with health `ok` or `degraded`; mode alone is not runtime readiness |
| model and route runnable | backend mode `hub`; that candidate's `named_agents` row has nonempty `effective_model_id`, `supply_status` in `ok` / `degraded`, and no `route_unconfigured` reason. `waiting`, `interrupted`, null or absent rows do not pass |
| application and custody | `getBackendConnection.ready === true` (including permission and matching credential ownership) and `application === 'applied'` are required alongside this candidate's route. Confirmed `stopped` permits bootstrap/recovery, never workspace readiness; draining/failed/unknown cannot enable entry |

Do not read backend-level `selected_model_id` to reject a different candidate: it describes
only `selected_by_agent`. The runnable-hop owner is the server resolver; neither an unrelated
healthy source nor a client `routeOrder` proves readiness. Revalidate on the completion click.

D11 establishes controller availability **before** Hub-dependent screen-2 work (C6).
`ModelHubRemoteService` uses the controller socket; `engine_down` on a missing socket is
neither an unsupported manifest nor proof of a bad model route. If the controller stops
later, retain a reachable start/retry action using confirmed application `stopped`, bootstrap
it and re-read runtime, connection and candidate supply before allowing completion. Startup
has no assistant/source readiness prerequisite. Do not restart after an enable/config write.

Preserve `default_agent_name` if it remains in the available set, including a usable custom
Agent outside the route editor's setup targets. Otherwise prefer a runnable setup target in
C6's stable order, then an available named Agent in stable backend/name order; call
`setDefaultVibeAgent` and verify a fresh Agent read. Write `setup_completed` last through
`api.mutateConfig` with an explicit field mutation, then validate a fresh uncached
`apiFetch('/api/config', {cache:'no-store'})` readback before navigating. Use that same
read/parse path after an unknown/failed write; cached pre-write data cannot settle it.
Unknown writes are reconciled by reads, not treated as success or blindly retried. Keep `SetupPlatformRecovery` for
existing invalid IM configuration. `SetupModelRecovery` is the shipped **Direct OpenCode**
recovery; it does not repair Hub models. C6 specifies Hub catalog/chain repair.

The Hub predicate applies only to Hub candidates. It supplements connection custody,
application and permission evidence with the named Agent's own runnable route. Today's `Wizard.complete()` only
filters OpenCode routes; the all-backend named-Agent join is new L3 orchestration, not a
helper that already exists. Unrouted enabled assistants retain a configuration action (C6).

**Direct fallback (current thread `PRRT_kwDOPbFPYs6kO76r`, comment `4059240601`).** When
`setupCandidatePath` returns `direct` (explicit capability-off or fresh unsupported install
admission), reuse `getBackendConnection` and the shipped Direct connection dialogs/model
recovery. The same installed/enabled backend must have `supply_mode === 'direct'`, no pending
config write/read error, valid native auth/permission and `entry_eligible`. The selected
named Agent must be enabled/non-archived on that backend; Direct OpenCode additionally uses
`readOpencodeSetupRoutes`/`SetupModelRecovery` for its own selected model. A Direct candidate
requires **no Hub runtime, Hub mode, Hub supply row or source-order proof**. This preserves
the shipped Direct credential/readiness owner; it does not delegate Agent model selection
to a backend-native default.

`entry_eligible` with confirmed `application === 'stopped'` keeps the shared start-and-enter
CTA usable. On activation, retain the existing start guard, call `control('start')`, then
re-read connection and require `ready === true`/`application === 'applied'` before default
selection and completion. Failed/draining/unknown cannot start or complete by inference.
Correlate every read with the same candidate, preserve a usable default across Direct and
Hub candidates, and perform the same last `setup_completed` write/readback above. An
unsupported host with neither a usable Direct candidate nor a usable running Hub candidate
remains configurable with entry disabled; unsupported is not readiness.

Never PATCH Hub to Direct or invoke native credential flows for a persisted Hub candidate.
Its `hub-recovery` path retains Models recovery and runtime retry; a stopped/missing engine
cannot pass entry. Conversely a fresh `ok`/`degraded` Hub can satisfy the Hub predicate even
if installation is unsupported. Keep its source/route reads and allowed existing-route
controls; do not hide or reset a working Hub. Pure setup policy tests prove branch selection
and navigation, not credential/backend readiness; existing `test_backend_connection.py`
covers the reused owner, including stopped versus broken IPC, permissions and Hub custody.

## C5 — stable dialog frame

Applies to all three screen-2 dialogs and to screen 3's route dialog.

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
where its actual inputs and outputs fit; controlled state, support preflight and shared model
mapping cannot be attributed to helpers that lack them. A lane reports contradictions instead
of silently changing either the handoff or another owner.

### Screen 2 — providers

| Element | Producer → consumer and rule |
| --- | --- |
| scan and discovery | `scanMigration().items` → full `providerSelection.scan`; `isImportableKey` identifies entry-point keys only. Keep every row for grouping. Compute group closure/blockers before the actionable capsule count; a blocked detected key remains visible with its reason but is not advertised as importable |
| candidate identity | `MigrationItem.id` is selection identity, never vendor alone. Use `providerLabel` / `providerVendorId` as `MigrationDialog.ItemRow` does. Optional vendor/name/mask may be absent: generic mark plus `masked_detail`, never infer vendor from backend or fabricate a mask. Show every `source_paths` locator; only when absent use `migration.source.<backend>`. `notes_key` is an explanation, not origin |
| cards | slots 1–2 show detected/provider identities then existing sources, with OpenAI/Anthropic placeholders if needed; slot 3 is Add more. Vendor grouping is presentation only: multiple keys from one vendor retain separate IDs and may require whole backend selection |
| selected and connected | selected means complete consent group selected; a blocked group is disabled with its reason. Added/connected treatment follows confirmed source IDs from `listSources` and status `active` / `standby`; `verification_pending` is disclosed as unverified, not rejected as unusable (the shipped resolver accepts standby). One vendor's healthy source does not prove every key/card connected or a runnable assistant |
| counts | CTA count = deduplicated appliable item IDs in selected complete groups; summary provider count = distinct displayed providers of those items. Capsule count = distinct key IDs in complete, unblocked, key-only groups, independent of selection. `importedCount` = confirmed `result.applied`, once per successful submitted batch, not number of sources or guessed key count. `addedThroughMore` = unique IDs returned by successful manual creation, badge filtered against current usable sources |
| capsule | retain dismissal signature owner `modelHubMigrationDismiss` and the reserved slot. Existing `ImportKeysNotice` scans/owns a dialog internally: L2 must adapt it to the shared scan/selection owner before claiming consistency; it is not a drop-in consumer of C2 |
| import / Detected tab | requires admitted install support; use the complete-group rule below. Hiding card duplicates or out-of-scope rows must never shrink consent. Already-added status follows fresh scan/source evidence, not a vendor-name match. D10 blocks wiring apply under copy-only promises |
| add — API key | shared `AddApiKeyDialog` form: observe then create with the confirmed observation, preserve its existing unknown-write recovery. Read `SourceCreated.source` and placement tails (`added_to`, `adopted_by`); refresh sources and affected supplies before reporting ready |
| add — subscription | reuse `subscriptionOptions.ts` / `OAuthConnectDialog` / `OAuthFlowParts` for the offered product vendors, custody choice and flow ownership. Setup limits the shipped vendor list to OpenAI/Anthropic per handoff; there is no callable vendor-capability-list API in `ModelsApi`, and no such producer should be invented. `getOAuthStatus` / `submitOAuth` return `OAuthResult`; terminal create carries `created.source` and placement tails. Do not create the source a second time or equate terminal OAuth with assistant readiness |
| picker order | `apiKeyVendors.ts` reads `vibe/data/api_key_vendors.json`; first eight in file order, remainder plus custom. No browser re-sort or prototype Cohere |
| runtime | active providers bootstraps controller then reads runtime into the shell policy (D3/D11). Ensure only with admitted support. Unsupported follows C2 navigation/C4 Direct fallback; an existing running Hub remains usable. Retry delegates to the same shell owner in the current screen |
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
backend list is the union of complete unblocked groups. Derive apply IDs from the **same scan**:
all `proposed_action === 'import'` rows whose backend is selected, once per ID. Validate closure
and blockers again before apply. With setup's API-key-only requirement, an otherwise importable
OAuth member also blocks the group for this entry point; show it and explain that the full
migration is available in Settings. Never silently import OAuth or drop it from a batch.

L2 must reuse/extract the dialog's group calculation and adapt it to controlled selection;
current `MigrationDialog` has only `open/onClose/onApplied/eligible` props and local `items`
selection. Do not bolt on an independent item-ID selection engine for cards or the Detected
tab. Rescans invalidate old item IDs: preserve selection only for unchanged complete groups;
changed membership/custody requires renewed review, and failed rescans disable apply rather
than submitting stale IDs. Default-selection applies only to fully importable, unblocked
initial groups, respecting the server's `selected` flags.

On confirmed apply, clear the applied group selection, increment the confirmed batch count
once, then refresh scan, sources and supplies. `applied` is a receipt count, not assistant
readiness. On unknown transport outcome, reconcile the submitted batch before retrying or
showing “nothing changed”; the same IDs can resume/replay a receipt. No fabricated partial
success UI, but no promise of rollback either: takeover recovery can retain changed custody.

#### Import custody choice — D10 (only product blocker)

`migration_apply` → `apply_native_migration` provisions sources, removes replaced native
credentials/direct-routing fields, commits Hub mode/placement and journals recovery.
`set_agent_mode(..., 'hub')` re-scans under the migration/native-writer guard and rejects
**any** remaining native row for that backend with `mode_switch_blocked`. A copy endpoint
alone therefore cannot preserve native credentials and still admit that backend to Hub.
Tests prove global/project keys are removed, unrelated MCP/name data survives and
post-exposure failures retain the current credential owner for recovery.

Two alternatives for the owner, prepared as one coherent C1 proposal:

- **Recommended: reuse approved complete-group takeover.** Cards and Detected actions open
  the same review; no apply on selection, navigation or runtime startup. Show every linked
  backend/file, the exact consequence `迁移后，CLI的认证信息将完全交由模型网关管理`, and
  **Not now / Start migration**. Confirm invokes the single server apply owner, which owns
  mode and cleanup together. Not now leaves native state/mode unchanged. Preserve unrelated
  settings; keep setup API-key-only, blocking mixed OAuth groups. Completion means takeover
  and cleanup completed; failure/unknown state requires reconciliation/recovery, never a
  blanket atomic rollback or unchanged assistant-connections promise. C1 marks this proposed
  setup behavior/copy provisional; existing product approval does not silently amend handoff.
- **Preserve the literal handoff:** separately design server-side credential copying **and**
  native coexistence/Hub-mode admission, including writer ownership, refresh and precedence.
  The scan's opaque IDs/masks cannot feed `createApiKeySource` as secret material. This is a
  backend/custody expansion outside PR0 and the planned reuse lane, not merely a copy endpoint.
  It remains unapproved; no partial implementation is presented as a complete solution.

#### Controller bootstrap — D11 (orchestrator-ratified technical sequence)

L1's existing setup shell owns one bootstrap operation on active provider entry, before Hub
RPCs. A hidden screen cannot start it. **Do not call `api.mutateConfig([])`:**
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
`capabilities.model_hub.enabled`, string `platforms.primary`, string-array
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
current request/activation. Feed capability, platform recovery and bootstrap decisions from
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
application and controller RPC availability before Hub engine operations. StatusProvider's
`refreshStatus()` only refreshes service status and may return null; neither it nor a cached
status alone proves config persistence or backend readiness. Missing CLI, sources, routes
and incomplete setup do not forbid controller startup. Bad existing platform configuration
uses `SetupPlatformRecovery`; do not disable configured transports or restart after an
already-reconciled config write.

Evidence is separated deliberately. The temporary frontend consumer imports the **actual
ApiProvider, serializer and apiFetch** with mocked HTTP: empty mutations reject before POST;
setup POST sends `{}` with JSON/CSRF; fresh GET bypasses both the primed ApiProvider cache and
HTTP cache; direct POST leaves cached config and convergence subscribers untouched; refusal,
unknown POST and invalid GET stay distinguishable. Separate hermetic backend fixtures drive
real config POST/GET, persistence and connection/control handlers with a fake process launch
and the existing real-loop readiness harness. They prove fresh seed, existing-state
preservation and controller startup without sources or completed setup. These are separate
transport/server proofs, **not browser/server E2E**, full Controller construction or a shipped
setup bootstrap implementation.

#### Ensure runtime, then prepare adoption (support fallback: `PRRT_kwDOPbFPYs6kO76r`)

0. D11 establishes controller availability before these RPC reads; a stopped application
   is distinct from an installed-but-stopped Hub engine. A missing socket is not an
   unsupported manifest.
1. An active screen/action owner obtains a fresh `getRuntimeStatus()`; a failed or stale read
   cannot authorize installation. Check `runtimeCanAttemptInstall(runtime)` **before** invoking
   any potentially installing helper or endpoint, including adoption, source observation/create,
   OAuth, migration apply and mode preparation. If false, make no install attempt or
   potentially installing mutation; publish fresh unsupported evidence to C2. If not running,
   reconcile providers → assistants and show `onboarding.setup.directFallbackNotice`; Direct
   candidates retain C4 entry/configuration. If already running, preserve existing Hub
   reads/routes and let the provider CTA continue to assistants using existing candidates,
   even when new-source actions are unavailable. Do not loop an impossible installation.
   Unknown/read failures offer retry without installs or fallback. Every retry repeats the
   authoritative read; `unresolved` remains install-admitted. The server
   remains the final platform authority if conditions change after the read.
2. With admitted support, automatically on active entry (approved D3), call
   `resumeInstallAndStartRuntime` with fresh runtime for **all backend modes**, including
   already-Hub. It installs or
   observes installation and starts when needed. `installAndStartStep` only classifies;
   `installRuntimeUntilSettled` does not start. None enforces `runtimeCanAttemptInstall`.
   Observe progress through `onRuntime`, handle `failedStep`, and re-read runtime; only
   `runtimeIsRunning` (`ok` / `degraded`) proves running. Already-Hub + stopped/missing engine
   takes this path only with admitted support; unsupported uses recovery/Direct fallback.
3. Adoption is a separate step. `resumeGatewayAdoption` reads agents, returns immediately
   for Hub (`runtime: null`, no scan), otherwise re-reads/ensures runtime and returns candidates
   filtered to one backend. It **never** sets mode or applies migration and has no support
   check. Setup should compose step 2 with a full `scanMigration()` for consent instead of
   treating this helper's `ok` as readiness or its filtered candidates as a grouping input.
4. After runtime readiness **and admitted install support**, prepare the backend needed by
   a Hub configuration action: native
   rows → full group review/apply subject to D10; no native rows →
   `setAgentMode(backend, 'hub')` directly, without another confirmation. The apply owner
   already commits mode; do not issue a second mode PATCH after migration.
   The service can still refuse `mode_switch_blocked` if native state changes. Re-read mode
   before eligibility: Direct has `sources: null`. A manual source creation does not prove
   the backend has switched. Runtime installation, scan, apply, mode and route writes are
   separate outcomes, each read back before the next dependent operation.

### Screen 3 — assistants (current thread `PRRT_kwDOPbFPYs6kOVg4`)

| Element | Producer → consumer and rule |
| --- | --- |
| installed | `api.detectCli(path)` at intro transition and active assistant entry; `AgentSupply.cli_present` corroborates. Staying mounted is not permission for hidden polling |
| install | explicit `api.installAgent(name)` then detect the returned path; retain failure/output; one loading indicator |
| enabled | existing Switch config write, followed by `getBackendConnection` and Agent reads. Saved config reconciles live backends; no second restart |
| upgrade | `BackendLifecycleChip.onVisual` still owns probe/write, activity-gated; update coexists with enabled state |
| route control | **every installed/enabled assistant has an action**. Use `setupCandidatePath`: `direct` keeps shipped connection configuration; `configure-hub` opens Hub setup; `hub` keeps its own route; `hub-recovery` retains Models recovery/retry; `pending` retains retry and prevents unsafe writes. For Hub route controls, show the candidate's own `named_agents` model when routed, otherwise `onboarding.setup.configureRoute`; opening requires no resolved route. Direct fallback uses its connection/model owner without a Hub projection. Read failures retain retry access; busy operations may temporarily disable action |
| candidate identity | use the setup target policy below; identify backend + Agent name and exact menu model, independently of the global default. C4 may preserve a runnable custom default outside these edit targets |
| add model source | offer only when policy permits provider configuration; removed providers requests are reconciled to assistants. `onNavigate('providers')` preserves the dirty route draft and closes the route dialog; reopening restores it. Done returns focus to the invoking chip/configure action |
| all uninstalled | three install actions, workspace entry disabled, Back still available |

### Model-ranked route mapping — D4/D9 orchestrator-ratified technical interpretation

This mapping applies to admitted Hub configuration and existing Hub route editing. Direct
fallback retains its own connection/model owner and never writes these chains or changes
mode merely to satisfy the dialog. C2 keeps `RouteHop[]`: ordered `(source_id, upstream model_id)` pairs. A source with two
models yields two distinct rows. `putAgentSources` changes backend source membership and
priority; it is never the model-ranked Save operation. Existing `getAgentChain`,
`previewAgentChain`, `putAgentChain`, `putAgentModels` and `RouteChainDialog` supply the
primitives. The shared projection and multi-target coordinator are new L3 orchestration.

**Targets and scope.** Each installed/enabled backend card represents its designated builtin
default Agent. Use `listVibeAgents({cache:false})` for enabled/non-archived candidates and
`getVibeAgent(name,{cache:false})` for metadata: the brief list omits it. Match the store's
builtin selection (metadata `builtin_default` or `lock_delete`; prefer name equal to backend,
then `default`, then name order). A custom Agent merely named `claude` is not a builtin.
If none exists, keep configure reachable and require an explicit existing named-Agent target
selection; do not create or mutate an arbitrary Agent. Preserve each target Agent's exact
saved `model`; join to `AgentSupply.named_agents` by backend/name, then the exact
`catalog_models` menu id. No backend-native default or prefix-stripping inference.

The shared reorder includes the displayed setup targets, deduplicated by `(backend, menu
model)`, with their names/model identities visible before Save. It does not edit Agent model
fields, other menu-model chains, source order, disabled backends or the global default.
All Agents on the same backend/menu model necessarily share that chain: disclose their names
as affected; do not promise custom Agents with that same key are unaffected. A custom Agent
on a different menu model and a runnable global custom default are preserved. Explicit target
selection is required before adding such a custom route to the edit set.

**Fresh or unrouted target.** First consult the shell candidate path, then ensure controller/runtime and mode only on
the admitted Hub path above; opening the
control itself has no route prerequisite. The server materializes recommendations for new
and legacy blank Agent models (`VibeAgentStore` creation/prefill); the UI never chooses the
first catalog row as an Agent model. Read back the actual selected model. If an anomalous
blank remains, require explicit Agent-model repair using existing Agent settings before
claiming a route target. The shipped Direct OpenCode recovery cannot perform this Hub repair.

Read the target's catalog and effective `getAgentChain`. Existing nonempty automatic or
manual chains hydrate immediately. If the exact saved menu id is absent (fresh OpenCode is
one real case), offer the existing catalog-add/manual form for **that exact id**, keeping the
current catalog as `baseline` and adding only the reviewed row through `putAgentModels`.
Use `getAgentModelCandidates` metadata for an exact matching candidate, or the existing
manual form's validated metadata/native protocol; never guess from the Agent name. If the
picker displays supplier chips, forward its `expected_suppliers` on catalog addition; a
manual row without a supplier promise omits that field. Preserve unrelated baseline rows
and read the catalog back before chain mutation. This adds a model entry, not an Agent-model
change; cancellation keeps the target unconfigured. Server admission errors remain visible.
For an empty chain, reuse the `routeCandidates`/`RouteChainDialog` exact-hop picker to select
an upstream model from an eligible source, preview it for this target, then save explicitly.
No arbitrary default hop, vendor-based cross-backend assumption or transplant from another
Agent. API-key passthrough can be an explicit manual target via the existing editor.

Example proved against the actual server: OpenCode's server-selected menu id
`openai/gpt-5.6-sol` can be admitted unchanged, then explicitly mapped to
`(source_id, 'gpt-5.6-sol')`. They are different identities. A source added on screen 2 can
supply an automatic route; it does not automatically extend a frozen manual chain. When an
existing manual route needs a new fallback, the same target-specific hop picker admits it
explicitly, then refreshes that target's membership. The shared reorder only reorders the
reviewed members; it never adds all global rows to every target.

**Initialization and reconciliation.** On the first clean open, snapshot for each target
`{ backend, modelId, agentNames, chain, manual_override }` from actual reads. Hydrate the
shared list by stable union of exact pairs: current global default's target first **only if
in the edit set**, then Claude/Codex/OpenCode and Agent name order. Preserve every target's
original membership and order as a baseline. Display the projected preferred/backup rank
for each affected target, not a claim that every row works on every backend. Divergent
existing orders remain unchanged on open/Done without an edit; show their actual orders and
the proposed outcome before an explicit shared Save. Disjoint native subscriptions retain
their disjoint subsets. A global interleaving change that alters no target is a no-op.

`routeOrder` and `routeOrderDirty` live in shell state. The single shared mounted route owner
retains target baselines, pending submissions and per-target results while hidden (local
state, not extra C2 fields or a new global store). Add source/Back closes the dialog without
unmounting this owner. Dirty re-entry retains both draft and baselines; read new server facts
separately, showing added candidates without overwriting edits. Changed Agent model, mode,
source membership or target set invalidates dependent previews and requires reconciliation.
If the owner is deliberately reset, clear the draft and baselines together, never retain an
orphaned dirty list. Refresh restores persisted truth, not an unpersisted browser draft.

**Projection, write and retry.** For each target, filter the shared order by that target's
reviewed exact-pair membership. Preserve temporarily unhealthy/stale persisted hops; the
server annotates health and admission. No empty write silently resets a chain: empty means
configuration/recovery required, because `{hops:[]}` removes the override and inherits defaults.
For changed targets preview with **`{manual_override:{hops}}`**. Server validation supplies
backend eligibility/model admission; inspect runnable/current and affected route evidence.
Do not synthesize vendor/protocol compatibility, auto-force interruption guards or hide a
failed target. A pure reorder keeps membership; actual backend eligibility changes may still
block it and require a fresh review.

Save only changed targets, in the displayed stable order, using `putAgentChain(backend,
menuModel,{hops})`. Reordering an automatic chain freezes that target as a manual override:
show the existing follows/frozen route explanation; future source additions will not silently
join it. Just opening or confirming an unchanged list never freezes an automatic chain.
Before each write re-read its Agent/model, mode and chain against the baseline. After each
write, read the exact target again and verify **manual_override and pair order**, not merely
the current healthy source. Reuse `routeChainMatchesAttempt` for identity/override matching.
A second-target failure leaves earlier confirmed writes intact and the shared draft dirty.
On retry read every attempted target: confirmed desired override/order is skipped; unchanged
baseline may be retried; any third state needs reconciliation/review. Unknown response means
read first. No cross-target rollback or atomicity claim. The API has no chain baseline/CAS;
a race after the last pre-read is an existing last-writer limitation, so the UI must detect
readback differences and must not promise exclusion of concurrent Settings writers.
Clear dirty only when every changed intended target is confirmed (or a verified no-op),
then refresh candidate supply/default readiness. A locally sorted list is never C4 evidence.

Temporary composed fixture evidence (real service persistence/preview/resolver with fake
engine, outside PR files): same source/two models remain distinct; native Claude/Codex subsets
remain separate; one builtin target edit preserves a custom Agent's different model/chain;
fresh Agent model materialization and OpenCode exact catalog/hop admission work; divergent
orders cause no opening write; second-target failure preserves the first write and retries
only the outstanding one. Existing primitive tests separately cover unknown-response
reconciliation, route guards and model prefill. Neither is a shipped setup integration test;
L3 must port these cases to its real consuming coordinator.

## Audit evidence and required consuming checks

| Repeated root class | Whole boundary audited / remaining evidence |
| --- | --- |
| route identity / eligibility / readiness | action availability → named Agent+backend → mode → menu model → exact chain → default readback. C4/C6 define reachable configuration, explicit targets, exact-pair projection, partial-write/readback and default preservation; composed fixtures exercise the mapping |
| lifecycle / capability / readiness | capability + fresh runtime admission/health → atomic effective sequence → persisted-mode candidate owner → admitted ensure-runtime or Direct fallback/Hub recovery → consent/mode readback. `gatewayAdoption.test.ts` proves helper scope only; it is not a screen-2 preflight test. Future L2 tests must cover unsupported with zero writes, already-Hub missing/stopped, controller stopped (D11, bootstrap fixture available), start failure, Direct with native blockers |
| producer / consumer / state | C2 React consuming test covers cross-screen state plus a late functional update. Full scan → transitive backend groups → complete apply IDs follows `MigrationDialog` and its grouping tests; L2 still needs a consuming integration test after adapting that owner |
| duplicated instructions / copy | plan §§4/9/10, C1 metadata and C2/C4/C6 reviewed together. Automatic runtime setup reuses approved precedent; D10 alone withholds migration copy/apply binding. Source-ranking substitution and false approval/rollback claims are removed; handoff remains unchanged user source |

PR0 tests do not claim a shipped screen or an end-to-end migration/route implementation.
