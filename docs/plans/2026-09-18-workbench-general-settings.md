# Shared Workbench and General Settings — issue #2012

Owner authorized parallel implementation on 2026-09-18 02:36 Asia/Shanghai. PM session: sestqz5wvu5ty. Branch feat/workbench-general-settings starts at GitHub-verified master 4019b704c99afe16223d475fccd9e1cb94a109d7 in its own worktree. #2011 is a separate active lane; #2013 remains deferred. Historical #2010-only or sequential-dispatch language is superseded by this explicit instruction.

## Outcome

Implement the approved shared Workbench shell, editable first-task home and General Settings from final design_desktop.pen. Preserve real projects, sessions, messages, inbox and permission-aware routes, one shared Composer and existing picker interactions. This is UI recomposition, not replacement of runtime/auth/data ownership.

## Accepted design and behavior

- Sidebar248: existing brand row, search/inbox with real unread badge hidden at zero, always-expanded main navigation, real project/session area or true empty state, bottom Apps/Settings, proper selected/hover/focus/status presentation.
- Home: Explore project / Research question / Make plan cards seed and focus an editable draft and never send. Recompose shared Composer with attachment, existing AgentRoutePicker, direct DirectoryBrowser and send. Preserve project find/create, user/default selection and permission rules. No second editor.
- One-time dismissible post-onboarding banner is conditional on the producer completion event and actual available Agent/readiness; ordinary visits have no banner. See exact cross-lane contract below.
- Continuation links keep `/settings/remote-access` and `/settings/platforms` destinations.
- General is the ordinary `/settings` entry (`/settings/general`), including return to app, General/AGENT/Connections/System groups, existing language ownership/autosave and explicit System/Light/Dark selection via existing ThemeProvider. Explicit deep links survive. Settings close preserves originating surface/draft/selection.
- Preserve approved themes/locales, actual product logos, smaller-screen access, keyboard/focus. No native titlebar/menu or fake native chrome. Owner-approved native canvas governs; no new design invention. Empty, error, permission and loading UI must express actual state.

## Native source and inspection

Read/export `/Users/max/workspace/ai/avibe/avibe-docs/design_desktop.pen` through Pencil; never mutate/save/switch active documents. Candidate nodes n0PzOn (Workspace), DkNTK (sidebar), r6G6P (General), uifbM (existing selector reference). A read-only observer supplies `/tmp/issue2012/design/design-index.md`; implementation writer must inspect actual exported images and current component ownership before visible edits. Discover final Light/Dark/EN/ZH variants, do not assume candidate IDs cover all frames. 1200x800 is a reference window, not fixed browser dimensions. Another project owns active Pencil document; content-identity check is mandatory because unavailable filePaths can fall back silently. If design.pen is inaccessible, use confirmed desktop source plus existing picker code without changing another canvas.

## Scope, testing, delivery

The copied parallel contract below is the exact producer/consumer/file division. Consolidate changes into this plan without editing #2011's working copy. No backend/schema/auth code, native desktop, brand replacement, or unrelated settings internals. No new dependency expected.

Use meaningful consuming tests: suggestions don't submit and preserve editability; Composer/picker/directory actions preserve draft and prior choice on cancel/Escape; route overlay restores state; real unread state; retained permission visibility; System follows OS while explicit preference and locale persist. Hermetic browser batch comparing actual renders against native exports in EN/ZH and both themes on desktop/narrow widths; fix observed defects once then targeted confirmation, no endless full matrix reruns. Run focused existing tests, UI build/lint/theme/i18n/type/catalog gates; never invent scenario IDs. Real auth/install stays #2011/manual acceptance, not claimed by screenshots.

No primary checkout/runtime/service/production stores/Incus changes. Test-owned fixtures and processes only; no local Avibe restart. Pre-push concrete diff/head/tests/visuals go to PM for independent spot-check. Then non-draft PR to master, Closes #2012, explicit #2011 dependency where applicable. No manual Codex trigger; cyhhao automatically triggers. Verify full exact-head review/all lint runs/zero unresolved threads and deliver final report before Watch cleanup. No merge without owner instruction.

## Progress

- [x] Owner parallel start, latest master and isolated workspace verified.
- [ ] Native design packet and existing route/state ownership inventory.
- [ ] Independent shell, home/Composer, General implementation.
- [ ] Producer #2011 contract integration and cross-lane consumer verification.
- [ ] Focused/browser verification and PM pre-push spot-check.
- [ ] Non-draft PR, exact-head Codex/CI and zero unresolved threads.
- [ ] Owner acceptance and separately authorized merge.

# #2011 / #2012 parallel implementation contract

PM: sestqz5wvu5ty. Owner authorized #2011 on 2026-09-18 02:16 and #2012 parallel start at 02:36 Asia/Shanghai. Both start from master 4019b704c99afe16223d475fccd9e1cb94a109d7. This explicit decision supersedes historical sequential-dispatch restrictions. Each uses its own default-branch worktree; no stacked PR and no base-branch direct commit is authorized. This outside-checkout document is authoritative until consolidated into the separate issue plans. Each lane commits its contract copy before feature edits. Do not overwrite a peer's changing plan.

## Exact ownership

#2011 session ses6652kpzsns owns Python/auth/install/runtime/readiness, ApiContext additions, Wizard/steps/onboarding, settings/{BackendOAuthPanel,oauth,providers,shared,BackendLifecycleChip}, wizardConfigMutations, auth_setup scenarios and direct tests. It also owns the historical shared-web-desktop-design plan consolidation, its own active plan, and onboarding fidelity tests. Its owner amendment restores the existing BrandLogo on the Web header and identical top-anchored heading/subtitle positions between both steps. Desktop behavior remains #2013.

#2012 owns AppShell, Workbench, workbench/{WorkbenchSidebar,Composer,new home components}, necessary existing project/folder presentation glue and useNewSession, SettingsLayout/SettingsOverlay*/SettingsPageShell, a new SettingsGeneralPage, adminNavigation/settingsRoutes/settingsOverlay, LanguageSwitcher presentation/shared language behavior if necessary, and relevant tests. App.tsx: ONLY General import/route and ordinary Settings landing wiring; no AuthGuard changes. ThemeProvider stays the single state owner and its external contract is preserved; consume mode/resolvedTheme/setMode, do not create another theme store. Existing AgentRoutePicker/DirectoryBrowser and MentionEditor behavior is reused, not replaced. Existing providers, inbox, search, Apps and status are reused. Do not redesign internal Model Hub/Agents/Skills/Harness/Vaults/Memory pages.

#2012 owns global index.css and shared primitive additions but must preserve existing values and public APIs; add scoped variants or new semantic tokens only. #2011 requests missing global tokens from PM, not unilateral global edits. Neither replaces brand assets. Shared i18n JSON overlap is limited to disjoint key ownership: #2011 onboarding/auth/provider keys; #2012 sharedWorkspace.*, settings.general.*, and necessary workbench/nav/settings-layout keys. No full-file format churn. PM checks merged key inventory and both build/test consumers after integration.

## Behavior boundary

Producer #2011: after explicit Enter, fresh authoritative readiness, successful required start/apply and narrow setup_completed save, navigate to `/` with React Router state `{ onboardingCompleted: true }`.
Consumer #2012: only that transient navigation permits the one-time dismissible banner. It is not authorization or Agent data. Consume it once without dropping other router state or the underlying Settings return location; dismissal/ordinary reload/return must not recreate it. Never persist a new onboarding database or auth flag.

Agent labels/default resolution come from existing `useNewSession` / `listVibeAgents({includeDisabled:false})`: actual `agents[]` with `name,display_name,backend,enabled,archived`, `default_agent_name` and effective project/user selection. Enabled alone is NOT applied authentication. Do not invent readiness fields on VibeAgentBrief, manufacture an Agent or pin a backend default.

#2011 produces `GET /api/backend/{backend}/connection` with `{ok, backend, installed, enabled, auth: subscription|api_key|none|unknown, application: applied|draining|failed|stopped|unknown, ready, entry_eligible, permission_required?, message?}` and the canonical ApiContext type/method. #2012 may consume the producer's published API to corroborate banner readiness; only `ready === true` corroborates a ready claim. False/pending/error must not render a successful banner or block existing Workbench access. No auth/start/save in the banner. Exact exported TS method/type names are to be delivered by #2011 before this dependent consumer is coded. Meanwhile implement all independent shell/General/composer work. Do not write a second fetch wrapper, type owner or dummy product fallback merely to compile against unmerged code. Final #2012 integration rebases after #2011 merge; dependent readiness verification must be completed before delivery, and the PR names that dependency.

Ordinary Settings opens `/settings/general` irrespective of last visited settings subsection. Explicit `/settings/backends`, `/settings/backends/{claude,codex,opencode}`, `/settings/platforms`, `/settings/remote-access` remain destinations. Overlay close preserves originating project/session/unsent draft/Agent selection and history semantics. Non-owner permissions remain intact. General language uses existing local update and permitted config autosave; theme System follows OS, explicit modes persist through current setter. No native host detection/header suppression/menu work.

## Integration acceptance

PM will verify the actual setup->Workbench event with producer and consumer together, non-ASCII draft/directory, Settings open/close preserving draft/project/Agent, permission-aware visibility, empty-send, suggestion-seed-without-send, theme/System persistence. Separate mocked suites cannot certify the cross-lane seam. Each lane first delivers a concrete diff/tests/native-source versus render to PM, then pushes a non-draft PR after PM spot-check. No merge without owner instruction. Never manually trigger Codex; cyhhao triggers it. Maintain lane and independent PM combined PR/CI Watches when a PR exists, forever and both timeout layers zero, durable cursor unchanged between heads. Paginate reviews/threads and apply repeated-class two-head breaker with PM diagnosis before another patch.
