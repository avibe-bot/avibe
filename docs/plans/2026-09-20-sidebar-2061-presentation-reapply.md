# Reapply the approved sidebar presentation after PR #2058

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

## Implementation and evidence

Pending implementation and independent PM verification.
