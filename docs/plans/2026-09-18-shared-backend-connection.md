# Shared backend connection and setup readiness — issue #2011

## Authorization and baseline

Owner session `sestqz5wvu5ty` explicitly requested implementation of #2011 on 2026-09-18 at 02:16:59 Asia/Shanghai. #2010 and its fidelity follow-up #2017 are merged (#2015 and #2028). This is the sole active redesign implementation slice; #2012 and #2013 remain queued. Previous #2010-only pause language in the shared plan is superseded for this issue.

Repository: avibe-bot/avibe. Branch: `feat/shared-backend-connection`. Assigned worktree: `/Users/max/workspace/ai/avibe/.worktrees/avibe/shared-backend-connection`. Starting master: `4019b704c99afe16223d475fccd9e1cb94a109d7`, verified against GitHub and containing #2028. The local clone has a narrow origin fetch refspec: `git fetch origin master` alone may leave origin/master stale. Refresh explicitly with `git fetch origin refs/heads/master:refs/remotes/origin/master` and verify the SHA before later integration.

## Outcome and preserved boundaries

A first-time user can install any supported assistant, apply one subscription or key connection through the same logic as Settings → Backends, and explicitly enter the workspace without configuring IM. A valid existing connection counts. Executable discovery, selecting a method, seeing a code, opening a browser, or an optimistic UI flag never counts as a connection. One unrelated missing/installing/upgrading assistant cannot block another usable assistant.

Use the approved 568px dialog family in `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen`. Preserve #2028's 1104px shared width, responsive geometry, assets and automatic animation with no playback controls. Use existing en/zh localization and Light/Dark/System ownership. Existing selectors retain their interaction. Do not reinterpret the design as a full Settings page squeezed into a modal.

This issue does not implement Workbench/General Settings, native Desktop presentation, a new credential store, Model Hub management, real credential onboarding on this machine, or a release. It does not change App authorization rules, member/remote eligibility, or continuing access for previously completed setup. A user's later credential expiry does not force them through setup again.

## Contract and ownership

One implementation writer owns the complete React/Python flow; a read-only design lane supplies source evidence. No parallel product writers or speculative cross-lane API types.

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

Paginate all findings/threads and count reviewed heads/root-cause classes each round. Two heads with a repeated class, or three findings-bearing heads after a model rewrite, stop blind repair for PM diagnosis; green tests do not waive the breaker. Final report precedes lane Watch cleanup. No merge, release, service restart or #2012/#2013 start.

## Progress

- [x] Owner start instruction and latest master verified; isolated worktree created.
- [ ] Native design evidence and Settings/API ownership inventory.
- [ ] Implement shared connection forms/lifecycle and authoritative readiness/completion.
- [ ] Focused/scenario/browser verification and independent PM spot-check.
- [ ] PR, exact-head Codex review, complete CI and zero unresolved threads.
- [ ] Owner acceptance / separately authorized merge.
