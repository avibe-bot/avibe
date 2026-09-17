# Shared Web and Desktop design implementation

## Owner decisions and delivery boundary

Owner session: `sestqz5wvu5ty`. Approved 2026-09-17.

- Implement the common React/Python application from latest `origin/master` first.
- Then bring the shared work into the existing `desktop` integration branch and implement native-only behavior there. The existing Tauri shell is the host; native menus/window controls are not HTML product UI.
- One installed assistant with one successfully applied subscription or API-key connection is sufficient to enter the workspace. Existing valid connections count. The user explicitly clicks Enter workspace; successful authorization alone never completes setup.
- Reuse Settings -> Backends authorization and save semantics. Reuse the existing AgentRoutePicker and DirectoryBrowser interactions.
- Keep existing product logo assets. Logo exploration boards are out of scope.
- Latest Chinese Dark screens in `design_desktop.pen` are the visual baseline. Owner will supply updated Light/English designs. Preserve working Light/System and English, add proper i18n now, and reconcile final visuals/copy when those designs arrive. Pending design polish does not block shared behavior.
- Implementation is authorized; publishing releases, changing the local running service, and merging PRs are not authorized by this instruction.

Initial shared source: `7c028d74ebc4eb82b39a09991fcb09c8400253b0`.
Design source: `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen`.
Read design via native Pencil MCP (the document is already open); never edit/save the owner's working design. Use shared `design.pen` for referenced existing pickers.
Related handoff: `/Users/max/workspace/ai/avibe/avibe-docs/docs/plans/2026-09-15-desktop-first-task-design.md`. Latest owner decisions and latest Dark root annotations supersede older conflicting onboarding notes.

## User outcome and acceptance invariants

1. A new user reaches a usable workspace through Welcome -> three fixed assistant rows -> Enter workspace, without configuring IM first. Settings remains the optional destination for platform setup.
2. Readiness is based on installed executable plus effective applied authentication, never executable presence, a selected radio, a rendered device code, or a locally optimistic success flag. Preserve existing valid native authentication and all saved product state. One incomplete backend cannot block another ready backend.
3. Every connection stays targeted to its originating backend. OpenCode provider selection uses the installed runtime provider-auth catalog. Active flows lock method changes. Close/Escape/cancel share cancellation and preserve previously saved credentials. Errors preserve editable input. Save success refreshes effective state before rendering connected.
4. Config mutation is narrow and concurrent-safe; setup completion retains the existing `setup_completed` ownership and controller reconciliation. Neither render nor detection writes credentials, enables remote access, or marks setup complete. No second service restart after accepted backend reconciliation.
5. Existing installations, Members, remote sessions, and recovery routes retain their current authorization and setup access rules. Setup is a one-time completion gate, not a new continuing requirement that locks existing users out after credential expiry.
6. Task suggestions populate an editable draft and focus it. Agent and directory selection preserve draft and existing session semantics. No selection sends a message. Directory belongs to the current instance and respects permissions; existing project find-or-create behavior owns directory identity.
7. Ordinary Settings entry opens General; explicit backend/platform/remote links retain destinations. Leaving Settings restores the underlying conversation/project, draft, and selection.
8. Shared UI remains usable at mobile widths and with keyboard/reduced-motion, and in English/Chinese and existing light/dark/system modes. 1200x800 is the desktop design reference, not a fixed browser viewport.
9. Existing project/session/inbox data, authorization gates, Apps, Memory, and permissions remain reachable. Mock-only example data and omitted settings are not product migrations.

## Frozen cross-lane contracts

### Existing routes and config

- `Wizard` remains exported from `ui/src/components/Wizard.tsx` and mounted at `/setup`; it keeps owning setup orchestration. Shared shell lane does not change AuthGuard logic without an agreed contract amendment.
- Setup producer writes the existing `setup_completed` through current narrow config mutation APIs only on the explicit completion action. The existing AuthGuard is the consumer. Preserve mode/platform migration semantics.
- On successful setup completion, navigate to `/` with React Router state `{ onboardingCompleted: true }`. This field means only that this navigation followed completed setup. It never authorizes access or fabricates backend availability.
- Workbench consumes `onboardingCompleted` for the one-time dismissible readiness banner. Its Agent label comes from actual currently available Agent/default resolution. Ordinary returning-user navigation has no banner. Do not add a global event bus or persistent onboarding database.
- Ordinary settings landing: `/settings/general`. Explicit destinations: `/settings/backends`, `/settings/platforms`, `/settings/remote-access`; backend management `/settings/backends/claude`, `/settings/backends/codex`, `/settings/backends/opencode` (verify existing route spellings before use; a mismatch is an amendment request).
- Existing auth API types and methods in ApiContext remain canonical. No speculative API or persistence schema is preallocated. Onboarding lane owns any necessary additive Python/API field work and reports the exact change before a consumer is added outside its lane.

### Styling and localization

- Reuse existing theme variables. Shell lane owns `ui/src/index.css` and primitives; onboarding lane owns its component-scoped styles and uses existing variables so each lane builds independently.
- No new package dependency is expected; existing framer-motion, Radix, Lucide and React are sufficient.
- Shared i18n JSON files are divided by key ownership: onboarding owns only new `onboarding.*` subtree and necessary existing `welcome.*`/wizard/auth keys; shell owns only new `sharedWorkspace.*` and `settings.general.*` plus necessary workbench/navigation keys. Do not reformat whole JSON or modify sibling keys. These two files are the only permitted file overlap, with disjoint key edits; orchestrator verifies integration by key, not by trusting text merge.
- Existing brand logos remain unchanged. No new generated brand assets.

## Implementation lanes and exclusive file ownership

### O: onboarding and authentication (Codex)

Owns `ui/src/components/Wizard.tsx`, `steps/Welcome.tsx`, `steps/AgentDetection.tsx`, setup-only step/support files, a new `components/onboarding/` subtree, `settings/BackendOAuthPanel.tsx`, `settings/oauth/`, `settings/providers/`, `settings/shared/` auth/runtime helpers, `settings/BackendLifecycleChip.tsx`, `context/ApiContext.tsx` only if necessary, `lib/wizardConfigMutations*`, and directly related tests. Owns necessary minimal Python changes in `vibe/api.py`, `vibe/ui_server.py`, `config/v2_config.py`, existing backend auth/setup owners, focused Python tests and `tests/scenarios/auth_setup/`. Scope expansions are reported before editing.

No-touch: shell lane files, global CSS, App routing/AuthGuard, Workbench/Composer/pickers, native desktop tree, all Model Hub implementation under `settings/models/`, brand assets, sibling i18n keys. Reuse Model Hub icons/utilities read-only only when semantics match, never divert Backends auth into hub storage.

Deliver fixed-order Claude Code/Codex/OpenCode cards with independent install/error/retry states, compact auth dialog family and existing management for connected controls. OpenCode execution-permission policy stays owned by its existing path and may not turn an unrelated backend into a global setup blocker; if this conflicts with the approved enter criterion, report with a smallest scoped recommendation.

### S: shared shell, homepage, General and tokens (Claude)

Owns `ui/src/index.css`, relevant existing primitive/visual presentation except brand assets, `components/AppShell.tsx`, `components/Workbench.tsx`, `components/workbench/WorkbenchSidebar.tsx`, `Composer.tsx`, the existing project/folder presentation glue and `lib/useNewSession.ts` only as needed; new shared-home components; `App.tsx` General route ONLY; `settings/SettingsLayout*`, `SettingsOverlay*`, `SettingsPageShell*`, new General page, `LanguageSwitcher*`, ThemeProvider/presentation helpers, `lib/adminNavigation*`, `lib/settingsRoutes*`, `lib/settingsOverlay*`, relevant keyboard/geometry helpers and directly related tests. Preserve existing picker internals unless a bounded trigger customization is required.

No-touch: onboarding lane files and Python, auth/provider/runtime helpers, ApiContext, Wizard/steps, Model Hub implementation, native desktop tree, brand assets, sibling i18n keys. Report geometry changes needed outside this scope before editing.

Deliver sidebar248 with header search/inbox and actual count; fixed navigation group; bottom Apps/Settings. Workbench has three draft-seeding cards, shared Composer with existing AgentRoutePicker and DirectoryBrowser, real one-time readiness banner, mobile continuation /settings/remote-access and messaging /settings/platforms. General uses existing theme/language ownership and explicit three-choice theme cards. Preserve non-owner language behavior and existing authorization rules.

## Design inventory and implementation guidance

- Welcome `bi8Au`: width1200x800 reference, no fake native titlebar in Web. Content max880; heading and three240x228 collaboration cards. Shared8.9s animation: PM0-1.5, handoff1.5-2.25, Codex2.25-3.75, handoff3.75-4.5, tests4.5-6, return6-7, PM summary7-8.9. Reduced motion shows completed. Six equal access tiles in two rows are descriptive; icon hover/focus1.18/180ms, idle emphasis every2s pauses during interaction/reduced motion. Start performs/reuses detection.
- Setup `i3vw9`, `mK2JX`, `ICidc`, `I9Q8VD`, `F8RLgR`: content max880, fixed three rows88px, radius12, logo well44, actions34. Hover changes border/glow only. Upgrade availability stays usable and is not completion gate. Compare live annotation before assigning any experimental per-frame color; shared semantic tokens remain default.
- Auth boards `V0zHJ`, `OHULW`, `MvT2k`, `oLe1U`, `FwRTK`, `laAX1`, `MtOWg`, `KWKHX`, `JtVGU`, `r2y97O`, `D6uRiy`, `MGCD0`: common568px dialog family, backend-specific fields. Settings->Backends auth/save semantics outrank old Model Hub reuse annotations. Claude auth token supported, Codex device flow, OpenCode device/browser/manual callback only when provider runtime requests that specific mode.
- Workspace `n0PzOn`: sidebar248, desktop padding28/48, heading30/600, task cards Explore project/Research question/Make plan. Composer radius16/padding18; inputs remain editable. Selector reference `uifbM` is reuse guidance, not a new page.
- Sidebar `DkNTK`: icon buttons32, navigation40 with2px gap, selected mint/border/glow, gray hover. Keep authorization-aware visibility. No collapsed capability group on desktop.
- General `r6G6P`: settings sidebar196, main padding30/40, heading27/600, language select190x40, System/Light/Dark previews, autosave, return to prior surface.
- `oc7yB` native application-menu behavior belongs to desktop follow-on only. Handoff boards are references; logo boards are excluded.

## Verification and delivery gates

Each lane works in its own worktree and opens a real non-draft PR targeting `master`, never merges. Both fork from the same locally committed contract baseline; this is an explicit orchestrator allowance to keep the contract versioned without directly committing/merging into protected master. Both PRs carry the identical plan commit; after one lands the other must rebase to remove the already-landed contract. No stacked PRs or product dependency between lanes is intended.

Load `pr-delivery-loop` and `background-watch-hook`; use exact-head Codex bot pass, zero unresolved threads across all heads, and all expected CI successful. One forever combined PR/CI watch per lane with timeout0; notify orchestrator at first PR creation so it arms its independent gate watch before subsequent pushes. Inventory findings by reviewed head and root-cause class before fixing; repeated class on two reviewed heads or three findings heads after model rewrite stops patching for orchestrator diagnosis.

Run focused behavior tests, auth_setup scenario cases for onboarding, required UI build and theme/i18n validation, and Ruff on changed Python before push. Test state must be isolated from real HOME/config/keychains/services. Never restart local Avibe. Browser validation uses sanctioned local Incus runner; no reset, remote ops, or production credential writes. At final integration check both actual boundaries together, including non-ASCII draft/directory and Settings return, and record visual evidence against exported Dark frames. Unit/mock success is not a live OAuth/network claim.

## Follow-on and pending inputs

- Owner updated Light/English design: pending, reconcile content/layout when supplied while keeping both operational now.
- Stage2 native desktop: after shared work is integrated, update from the shared master result and implement system Settings/menu/window specifics in desktop-based task worktrees. Preserve loopback/capability boundaries and existing lifecycle. No desktop->master wholesale merge is implied.
- Master merge and release: wait for explicit owner instruction after concrete PR gates and acceptance evidence.
