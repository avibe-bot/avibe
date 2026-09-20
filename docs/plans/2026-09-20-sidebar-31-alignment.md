# Sidebar v3.1.0 alignment

## Background

The desktop Workbench sidebar was redesigned after the published v3.1.0 UI. The owner wants a focused new PR that restores the v3.1.0 presentation for the sidebar areas shown in the approved screenshot, while keeping the current Settings suspension and navigation/data lifecycle fixes.

The published reference is the remote `v3.1.0` tag at commit `0e5a672ad183ac56f5ee8df7bcb770754840b3d9`. The current base is `master` after PR #2051 (`66fc91dbef41b387e3b55152fbe9b5c1b35b9396`).

## Contract

- Restore a collapsible desktop Capabilities section containing the currently authorized Agents, Skills, Harness, and Vaults rows.
- Restore the v3.1.0 visual states for those four rows: indentation, spacing, radius, icon sizing, default, hover, and selected treatment. Keep current capability filtering and route guards.
- Restore the v3.1.0 desktop brand treatment inside the retained Workbench sidebar owner. Keep the current Settings activity boundary and adjustable sidebar width.
- Restore the v3.1.0 Inbox row treatment while retaining the current real unread counter, hover popover, mark-read behavior, route navigation, and inactive-surface feed withdrawal.
- Put the Search icon button in the Projects header immediately before the add-project button. Preserve the current search callback and access behavior; do not copy the old permission restriction.
- Keep project loading, expansion, reorder, session actions, resize behavior, Apps/Settings footer, VersionBadge, and mobile header/global logo assets unchanged.

## Scope

Expected implementation paths:

- `ui/src/components/workbench/WorkbenchSidebar.tsx`
- existing focused sidebar tests and adjacent browser coverage only when required by the presentation contract
- this plan document

`ui/src/components/AppShell.tsx`, translations, shared primitives, backend files, dependency files, Composer, ChatPage, and global/mobile logo assets are out of scope unless an evidence-backed compile or accessibility issue requires a narrowly documented adjacent change.

## Acceptance invariants

- A user can collapse and expand Capabilities, and the choice survives remount through the existing local-storage convention.
- Every capability row that the current authorization admits remains reachable and receives the correct active state on its route; unauthorized rows remain absent.
- Search is a keyboard/focusable icon control in the Projects header and invokes the existing search callback.
- Inbox styling reflects active, inactive, and unread states without changing the counter or lifecycle behavior.
- Settings opening withdraws sidebar data consumers and interactions exactly as before.
- Focused tests pass, the UI typecheck/build passes, and repository lint introduces no new violations.
- The diff contains no unrelated product, backend, dependency, or PR #2058 changes.
