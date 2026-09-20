# Reapply the approved sidebar and home presentation after PR #2058

The owner approved this work in two steps. The sidebar contract below was given
first; the Workbench home was added to the same change at 13:03:52 and is
specified in "Owner amendment: restore the Workbench home". Both surfaces ship
in one branch and one PR.

## Owner decision and precedence

On 2026-09-20 at 12:53 Asia/Shanghai, the owner explicitly requested reapplying
PR #2061's sidebar presentation and search position to current master while
preserving PR #2058's queue fixes and other improvements. The attached reference
at `/Users/max/.avibe/attachments/avibe/sesqn7etypwey/a3387de5-8137-491f-9313-2653ced80d92_image.png`
is the approved visual target. The owner also accepted the immediately preceding
proposal to retain current collapse behavior while restoring the presentation.
No new design decision is pending.

This supersedes the September 20 10:30 choice of the complete #2058 sidebar only
for the presentation named below. It does not restore #2061's localStorage
persistence or revert #2058 as a whole. The historical v3.1.0 and Workbench plans
remain as provenance; this contract governs the new change.

Base: actual master `b821f22f55b2230de176ede3a1183acbae81b528` (merged #2058).
Reference implementation: #2061 commit `05904ee80595e14f1d0fd44398ff4798bec9a597`.
Use the relevant presentation hunks as evidence, not a wholesale file replacement.
Branch: `fix/sidebar-2061-presentation-20260920`.
PM: `sesqn7etypwey`. One implementation lane owns the isolated worktree.

## Outcome and visual contract

- Restore the #2061 sidebar brand chip treatment: square mark with mint hairline,
  rounded corners and the existing subdued glow; Avibe title and the existing
  translated `Agent OS · Workbench` subtitle. Reuse the current logo asset.
- Restore the full-width outlined Inbox entry, its active/hover/unread treatment,
  and spacing matching #2061. Keep the real unread counter, zero-badge absence,
  hover popover, mark-read behavior, navigation and suspension unchanged.
- Restore the Capabilities heading's label-left/chevron-right composition and
  the #2061 Agents/Skills/Harness/Vaults row treatment: compact rounded rows,
  16px icons, 13px labels, original default/hover/mint selected states.
- Move Search from the standalone row below the brand to an icon button on the
  right of PROJECTS, immediately before New Project. Keep existing search
  callback, keyboard shortcuts, accessibility name and authorization behavior.
  Search remains available even if project creation is not permitted.
- Reuse #2061 sidebar grouping/spacing needed for the screenshot, while keeping
  current sidebar sizing, project scrolling and footer ownership.

## Behavior that remains current

Capabilities start expanded, can collapse, and retain state through Settings
suspension while their owner is mounted. Do not add localStorage reads/writes or
change behavior on fresh mount. The reference image depicts the collapsed state,
not a requirement to change the default. Keep current permission filtering,
aria-expanded/aria-controls linkage, routes, focus behavior and inactive-surface
admission guards. Preserve all merged queue snapshot/invocation repairs, Composer
spacing/voice controls, real project/session data, drag/reorder, sidebar resize,
Apps/Settings footer and VersionBadge. No global or mobile logo changes.

## File boundaries

Primary implementation: `ui/src/components/workbench/WorkbenchSidebar.tsx`.
Update the existing `WorkbenchSidebar.inbox.test.tsx` consumer as needed.
Existing browser coverage in `ui/e2e/workbench-general/sidebar-capabilities.spec.ts`
and `sidebar-floating.spec.ts` may receive narrowly justified assertions for this
contract; do not rewrite unrelated scenarios or relax guards. Test-owned visual
probes and evidence can live outside the tracked tree.

Documentation: this plan, plus a short precedence notice in
`docs/plans/2026-09-18-workbench-general-settings.md` (prepared by PM).
Do not alter the historical #2061 implementation record.

No ChatPage, ChatQueueRow, Composer, AppShell, project drag production/spec,
shared primitive/hook/provider, backend/API, translation, global CSS/token,
logo asset, dependency or workflow edits. If a real compile/accessibility gap
requires another path, report evidence to PM before expanding the scope.

## Verification and delivery

Use existing tests for affected behavior; avoid tests that merely duplicate
class strings. Hold Search callback/location, authorized capability navigation,
collapse/restore through Settings, unread and inactive behavior to their current
contracts. Run focused sidebar consumers and relevant adjacent consumers, UI
typecheck, baseline lint and production build. Run the existing project-order
suite once because the available project height changes; preserve exact ordering
and session assertions, declared platform skips and retries=0.

Inspect hermetic real-App renders against the supplied image and #2061 in one
bounded batch (light/dark and EN/ZH, normal/narrow sidebar, expanded/collapsed,
selected/hover states), then at most one targeted correction/confirmation batch.
Keep screenshot/probe limitations explicit; no real credentials, cloud/native
interaction, production user data, service restart or shared deployment.

The implementation lane commits and hands back a clean local candidate with its
evidence, then ends its turn; it performs no remote writes. PM independently
checks the bounded diff and opens the new non-draft PR to master, reading back
title/body/base/head with --body-file. PM owns the delivery loop and one durable
combined PR/CI Watch once the PR exists; this local-only lane needs no Watch.
Retain exact-head Codex review, all expected CI and zero unresolved whole-PR
threads; no manual review trigger or CI rerun. Never touch retired #2058 or
unrelated Watches. No merge authority.

## Owner amendment: restore the Workbench home (2026-09-20 13:03)

At 13:03:52 Asia/Shanghai the owner authorized restoring the Workbench home to
the attached reference
`/Users/max/.avibe/attachments/avibe/sesqn7etypwey/0fc9e661-a496-4183-903f-9f76a6270ad4_image.png`
and asked for it in the same change as the sidebar, not a second PR.

Precedence: this amendment supersedes only the sidebar-only outcome above and
the earlier "no home, no translation" boundary. It does not replace the sidebar
contract and authorizes no other revert of #2058. Presentation reference is
v3.1.0 commit `0e5a672ad183ac56f5ee8df7bcb770754840b3d9` at the then-current
`ui/src/components/Workbench.tsx` — evidence for layout and copy, never a file
to restore wholesale over the current home.

### Outcome and visual contract

- A centered, bounded welcome card: rounded corners, a fine border over the
  raised surface, a mint outlined Sparkles tile, the heading "What should we
  build today?" and the body "Describe what you want and an agent will run it.
  Or start with one of the suggestions below." Chinese is recovered from the
  v3.1.0 locale (or translated accurately where it does not exist) and lives in
  the existing `workbench.home` keys — no new namespace, no hardcoded copy.
- Three rounded outline pills inside the card: Open project (FolderPlus),
  Manage agents (Bot), Schedule background work (Activity). Their owners stay
  the current ones: the directory/new-project path, the capability-gated agents
  route, and the current `CreateViaChatDialog` task initiation. The historical
  `/harness` navigation is not restored and no permission check is dropped.
- Below the card, in order: a visible PROJECT section of real selectable
  horizontal project chips through the existing `ProjectPicker`; an AGENT label
  with a separate, normally bordered `AgentRoutePicker`; then the existing
  `Composer` in its supported single-row mode — no embedded action row, the
  component itself unchanged.
- The old reference's column is 640 CSS px, and the screenshot's proportions
  correspond to it. Its pixel dimensions are not a spec and imply no capture
  device pixel ratio; no 1544px fixed geometry. Responsive renders, not capture
  metadata, decide whether the column is right.
- Narrow widths stay in normal flow: wrapping, horizontal chip scrolling, no
  page overflow and nothing obstructed by the mobile tab bar.
- The composer placeholder is "Tell the agent what you want…" through the
  existing home key. Project names, Agent, model and reasoning effort are
  whatever the instance actually has; nothing in the screenshot is faked.
- The current ReadyBanner/setup handoff and the conditional owner continuation
  links are retained, fitted unobtrusively outside the hero.

### Behavior that remains current

`useNewSession` keeps ownership of projects, Agent route and send. Errors and
the uncertain-send inspect link, `stageMedia` with staged attachments passed
through to `ns.send`, and deferred navigation that only runs while the surface
is active all stay as they are — the old `initialMessage` replay is NOT revived.
The authorization gate, setup readiness and its one-time handoff, ordinary
departure semantics, retained Settings drafts and pickers, real folder
find/create/select, and the empty and permission-restricted project flows are
unchanged. No queue or data-model rewrite.

### Additional file scope

Primary: `ui/src/components/Workbench.tsx`, its existing consumer
`ui/src/components/Workbench.test.tsx`, and existing `workbench.home` leaf
values in `ui/src/i18n/en.json` and `ui/src/i18n/zh.json`.

Focused locator migrations only, where an existing case names home copy or an
affordance this change moves: `ui/e2e/workbench-general/setup-handoff.spec.ts`,
`mobile-continuation.spec.ts` and `ui/e2e/home-media/media.spec.ts`; the same
allowance covers `geometry.spec.ts` and `standalone-settings.spec.ts` if they
turn out to name one. Keep each scenario's claim; migrate the locator, not the
assertion. `Composer.tsx`, `AgentRoutePicker.tsx`, `ProjectPicker.tsx`,
`useNewSession.ts` and `ChatPage.tsx` stay unchanged unless a specific gap is
reproduced and PM rules on it.

### Acceptance

Both surfaces are judged together against their two approved references in one
bounded hermetic visual batch (EN/ZH, light/dark, desktop and narrow), plus at
most one targeted confirmation batch. Home acceptance additionally requires:
real project chips selectable and reflected in the send target, the Agent picker
readable and separate, a single-row composer that still sends with staged
attachments, no horizontal overflow at narrow widths, and the readiness banner
and continuation links still behaving as they do today. Validation extends to
the home's own consumers — focused Vitest, UI typecheck, test typecheck, lint,
production build, and the browser cases whose locators moved.

## Owner resolution: the screenshots govern (2026-09-20 13:20)

Between 13:14 and 13:20 the contract was clarified twice, and the second
clarification settles it.

At 13:14:59 the owner asked whether the home was a revert or a reimplementation
and answered it themselves: it is a restoration. The old presentation hunks —
layout structure, tokens and classes, card proportions and spacing, the Sparkles
treatment, pill shape and icons, project chips with a standalone Agent picker,
the single-row composer and the old localized copy — are reapplied onto the
current home. No new visual abstraction, component or approximation from
scratch.

At 13:16:18 the owner was unsure which version to revert to, and work on the
home paused while that was resolved. At 13:20:14 the owner resolved it:
"以截图为准，sidebar 和首页回退放到一个 PR 里". **That resolution supersedes the
13:16 hold in full**, and this section supersedes any earlier wording here that
reads as a release identification.

What the resolution fixes:

- The two attached screenshots are the authoritative visual targets. #2061
  `05904ee80595e14f1d0fd44398ff4798bec9a597` and v3.1.0
  `0e5a672ad183ac56f5ee8df7bcb770754840b3d9` are matching source references for
  restoring existing presentation hunks. Neither identifies the release the
  screenshots came from, and neither authorizes resetting a release.
- No release selection is needed or pending. There is no open version question.
- Both surfaces ship in one branch and one PR. The sidebar is already committed;
  the home lands on the same branch.
- Screenshot appearance governs appearance only. It is not a licence to roll
  back business logic: background work still starts through
  `CreateViaChatDialog`, permission filters, retained Settings drafts and
  pickers, readiness and continuations, staged media and voice, send-exactly-once
  and the merged queue/invocation repairs all stay as master has them.
- The capability group still defaults to expanded with no localStorage. The
  collapsed sidebar in the screenshot is a render state, not a new default.
- `ProjectPicker`, the standalone `AgentRoutePicker` and `Composer`'s single-row
  mode are reused unchanged.

### Sidebar colour ruling (PM, 13:22)

The first sidebar commit kept `--nav-selected-bg` / `--nav-selected-border` /
`--nav-hover-bg` / `--shadow-glow-nav-mint` for the capability rows,
`--logo-well-background` for the brand chip and the nav hover token for the Inbox
row. Those are not equivalent to the reference: the dark selected fill differs in
alpha, the light selected border differs in alpha, and the logo well is grey
instead of mint. `bg-mint/[0.08]`, `border-mint/30` and `hover:bg-foreground/[0.04]`
are themed mint and foreground at a deliberate opacity, not hard-coded colours,
so "the reference hard-codes values" never justified substituting them.

Ruled: restore the reference's capability default/hover/selected classes and the
mint logo well exactly, and give the Inbox row the same `foreground/[0.04]` hover.
State, accessibility, permission and lifecycle behavior stay current; differences
from the reference in those dimensions are intended, colour differences without a
reference are not. No `index.css` or token cleanup is in scope. Both themes are
checked once in the final visual batch.

## Implementation and evidence

Delivered on `fix/sidebar-2061-presentation-20260920`, local only, awaiting
independent PM verification.

### What changed

`WorkbenchSidebar.tsx` carries the reference's capability row classes, the
`foreground/[0.04]` Inbox hover and the mint brand well. `Workbench.tsx` is the
restored home: a bounded welcome card with the mint Sparkles tile, the heading
and body copy, and the three outline pills, over a 640px input column holding
the existing `ProjectPicker`, a labelled standalone `AgentRoutePicker` and the
`Composer` in single-row mode. The pills keep their current owners — the
directory/new-project path, the capability-gated agents route and
`CreateViaChatDialog`. The old copy comes back through the existing
`workbench.home` keys, with the Chinese recovered from the v3.1.0 locale;
`chooseWorkspace` and `workspaceAria` had no consumer left and were dropped.
`Composer.tsx`, `AgentRoutePicker.tsx`, `ProjectPicker.tsx`, `useNewSession.ts`
and `ChatPage.tsx` are untouched.

### Consumer migrations

Each moved locator keeps its scenario's claim. The home surfaces no folder path
now, so `Workspace: <path>` accessible-name assertions became display-name chip
locators plus the picker's own `bg-mint-soft` selection fill; `ProjectPicker`
exposes no `aria-current`, and adding one would edit a shared component that is
out of scope. `home-media`'s attach-target assertion moved from 28x28 to 28x36,
which is what the shared `Composer` renders in single-row mode — the mode the
contract mandates — so the claim (a fixed target that does not stretch with the
viewport, language or theme) is unchanged. `mobile-continuation`'s `pickClaude`
now dismisses the Agent popover, because in the restored layout it is anchored
over the project row.

### Pre-existing failures repaired in passing

The build-config suite was already red before this branch: a probe of the
unmodified base head ran 6 failed / 2 passed. `NewProjectDialog` began passing
`initialPath` in #2047, after the browse fixture landed in #2033, and
`directory-browser` has no fallback when the opening listing misses — so the
four mobile-continuation cases could never reach a folder. One fixture entry for
the recent workspace's own path fixes it with no assertion changed. Two
setup-handoff locators were also stale: a heading string that no longer exists,
and a "New chat" link whose i18n key has no consumer anywhere in `src`; the
latter now leaves and returns through the sidebar brand row.

### Validation

Vitest 4765 passed across 332 files; lint, `tsc -b`, test typecheck, both e2e
project typechecks and the production build all pass. Browser suites:
workbench-general 67 passed, workbench-general-build 8 passed (base head: 6
failed / 2 passed), home-media 55 passed (before: 20 failed / 35 passed),
project-order 16 passed. No Playwright suite runs in CI; only lint does.

The visual batch used the suites' own captures — `home-{en,zh}-{light,dark}-
{desktop,narrow}` under `ui/e2e/.artifacts/workbench-general/shots/` and the 20
`home-media` layout renders. Both surfaces match their references in both
themes: the brand chip sits in a mint well, the Inbox pill and capability rows
carry the reference's fills, and the home reproduces the card, tile, copy, pill
order and icons, the mint-filled selected project chip, the bordered Agent
picker and the single-row composer. The remaining differences are fixture data
(one project instead of five, an Agent with no model or effort to show) and the
capability group rendering expanded, which the owner already ruled is a render
state rather than a new default. No targeted confirmation batch was needed.
