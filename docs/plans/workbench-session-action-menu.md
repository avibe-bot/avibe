# Workbench: one session action menu (⋯ everywhere) + ⌘⇧D archive

## Why

The session action set exists in **three places, implemented three times, discovered three
different ways**:

| Surface | Affordance | Items |
|---|---|---|
| Desktop sidebar (`WorkbenchSidebar.tsx` `SessionRow`) | hover **pin** button + **right-click** popover | pin, reference, fork (disabled + tooltip), rename, hide, archive |
| Mobile projects (`ProjectsPage.tsx` `MobileSessionRow`) | always-visible **⋯** popover | pin, rename, fork (**hidden**), hide, archive — no reference |
| Chat header (`ChatPage.tsx` `ChatHeaderBar`) | **nothing** | you must leave the chat, find the tree row, and right-click it |

Consequences the user hit directly:

- One arbitrary action (pin) got promoted to a permanent hover button; the other five hide
  behind right-click, which has no touch equivalent and no visual hint.
- The surface where you spend all your time (the chat) can't rename, fork, pin, hide or
  archive the session you are looking at.
- The two existing menus have already drifted (fork disabled vs. hidden; reference missing),
  which is exactly the reuse-ladder failure mode CLAUDE.md §6 calls out — third repeat →
  extract.

## Goal

1. One session-action model, rendered by every surface — sidebar ⋯, sidebar right-click,
   mobile ⋯, chat header ⋯ — so a new action lands everywhere at once.
2. Sidebar: pin button → ⋯ menu, without losing the at-a-glance pinned signal.
3. Chat header: the full menu in the top-right corner.
4. `⌘⇧D` / `Ctrl+Shift+D` archives the chat session you're reading (via the existing confirm
   dialog — never a silent destructive keystroke).

## Design

### A. `useSessionActions.tsx` + `sessionActions.tsx` — one model, four render sites

Split in two by the fast-refresh lint policy (a `.tsx` module may export components *or*
hooks, not both): `useSessionActions.tsx` owns the model and the writes, `sessionActions.tsx`
owns the descriptor type plus the two presentational pieces (`SessionActionsTrigger`,
`SessionActionMenu`).

`useSessionActions({ session, projectId, onRenameStart, onOpenSession, onArchived,
onSessionPatched, archiveHint })` returns:

- `actions: SessionActionDescriptor[]` — ordered, grouped `{ id, group, icon, label, hint,
  title, disabled, pending, danger, onSelect }`
- `archiveDialog` — the wired `<ArchiveSessionDialog>` element (open state + confirm live in
  the hook, so no surface re-wires it)
- `requestArchive()` — opens that dialog; the keyboard shortcut calls it

Writes go through the existing `useWorkbenchProjectsTree()` provider (cache patching stays in
one place) and `api.setSessionVisibility` / `hideSessionToBackground`. Pending state (pin,
fork) is owned by the hook.

Surface-specific behavior stays in callbacks, not in copies of the list:

- `onRenameStart` — sidebar/mobile open their inline `<Input>`; the chat focuses the header's
  existing click-to-edit `TitleField` (via an imperative handle) instead of adding a second
  editor.
- `onOpenSession` — the sidebar routes through `useUnsavedChangesActionGuard()`; mobile and
  chat navigate plainly.
- `onArchived` — the chat leaves for `/inbox`; the rows just drop.
- `onSessionPatched` — the chat syncs its own `session` copy (the provider cache is the
  sidebar's, not the chat's).

`session: null` (or a read-only session) yields `actions: []`, no dialog, and a no-op
`requestArchive()` — so the hook can be called unconditionally at the top of `ChatPage`.

`<SessionActionMenu actions={...} />` renders `role="menu"` items with a hairline divider
between groups, danger styling for archive, `hint` right-aligned in mono (the ⌘⇧D badge),
`title` for the disabled-fork explanation, and roving arrow-key focus.
`<SessionActionsTrigger>` is the shared ⋯ button (hover-revealed in rows, always visible on
coarse pointers and while the menu is open).

Harmonized semantics (drift fixed while unifying):

- **Fork**: disabled + `sessionForkUnavailable` tooltip on *every* surface (teaches why);
  the mobile hide is dropped.
- **Reference this session**: stays contextual — visible only when another chat's composer is
  mounted. The chat header therefore hides it automatically (you can't reference yourself).
- **Archive**: last, pink, divider above, confirm dialog unchanged.

### B. Sidebar row

```
  ● What's nex                     ⋯      ← hover / focus-within / coarse pointer / menu open
  ● Release checklist        📌     3     ← pinned glyph inline, always visible
```

- `SessionPinAction` (the interactive hover pin) is deleted; the ⋯ takes the right rail and
  right-click keeps opening the same menu through `PopoverAnchor`.
- The pinned signal survives as the existing non-interactive `SessionPinIndicator` (cyan pin),
  moved inline before the unread badge — always visible, no layout jump, and it frees the row
  padding helper to reserve the rail on hover only (`sessionPinLayout.ts` →
  `sessionRowLayout.ts`, `sessionRowActionPaddingClass`).
- Trade-off accepted: pin/unpin is 2 clicks instead of 1. Pin is a rare deliberate act; ⋯
  makes six actions discoverable instead of one. Pin stays the *first* menu item.

### C. Chat header

```
← │ What's nex ✏️ │ [CLAUDE ▾] │            🖊 Visualize  ⋯
```

Rightmost control, present in chat mode and Show Page mode. Items: Pin to top · Rename ·
Fork session · Hide to background · Archive session (⌘⇧D).

**Read-only sessions withdraw the ⋯ entirely.** An archived or runtime-owned (`visibility:
system`) session refuses every one of these server-side (409 `archived` / 403
`reserved_session`), so offering a menu of guaranteed failures is worse than offering none —
the same reasoning `showPageControlActions` already applies to Visualize/Share/annotate. The
`Archived` / `System session` badge remains the explanation.

### D. `⌘⇧D` — archive the current chat session

`chatShortcuts.ts`, following the existing pure-chord-module pattern
(`apps/dockShortcuts.ts`, `apps/windowChords.ts`) so the matcher is unit-testable without a DOM:

- `isArchiveSessionChord(e)` — exact `(meta|ctrl) + shift + KeyD`, no alt; matched on
  `e.code` so it survives keyboard layouts.
- `archiveSessionShortcutLabel()` — `⇧⌘D` on Apple, `Ctrl+Shift+D` elsewhere; rendered as the
  menu item's `hint` so the shortcut is *discoverable* instead of folklore.
  `isApplePlatform()` joins `lib/platform.ts` (a third local copy of the UA sniff would be the
  same drift this plan removes).

Registered by `ChatPage` on `window` (same shape as the shell's ⌘K):

- It **opens the confirm dialog**, never archives directly — a destructive keystroke needs a
  guard, and the dialog already exists.
- `preventDefault()` so Chrome/Firefox's own ⌘⇧D (bookmark-all-tabs) doesn't fire.
- Deliberately wins from inside the composer, like ⌘K — it's a command, not text entry.
- Inert on a read-only session (`requestArchive` is a no-op) and while the dialog is already
  open.

## Files

- new `ui/src/components/workbench/useSessionActions.tsx` (model + writes)
- new `ui/src/components/workbench/sessionActions.tsx` (descriptor type + ⋯ trigger + menu)
- new `ui/src/components/workbench/chatShortcuts.ts`
- new `ui/src/components/workbench/sessionRowLayout.ts` (replaces `sessionPinLayout.ts`)
- `SessionPinAction.tsx` → `SessionPinIndicator.tsx` (interactive pin deleted, indicator kept)
- `WorkbenchSidebar.tsx`, `ProjectsPage.tsx`, `ChatPage.tsx` — consume the shared model
- `lib/platform.ts` — `isApplePlatform()`
- `i18n/en.json` + `zh.json` — reuse existing `workbench.session*` keys; new keys only for the
  chat menu's aria-label if needed

## Tests

- `sessionActions.test.tsx` — menu markup (grouping/divider, danger archive, disabled fork with
  tooltip, hint badge, `role="menu"`/`menuitem`, empty list) and the ⋯ trigger's
  `aria-haspopup`/`aria-expanded` + reveal classes per variant.
- `SessionPinIndicator.test.tsx` — the passive pin glyph and `sessionRowActionPaddingClass`;
  together with the above it replaces `SessionPinAction.test.tsx`.
- `chatShortcuts.test.ts` — exact chord matching (rejects plain ⌘D, ⌥⌘⇧D, KeyD alone) + both
  platform labels.
- `ChatArchivedReadOnly.test.tsx` — new case: a read-only header passed a non-empty
  `sessionActions` still renders no ⋯ (`ChatHeaderBar` re-states the withdrawal itself, so the
  guarantee doesn't rest on the hook alone).
- `npm run build` in `ui/`; `ruff check` is a no-op here (frontend-only change).

## Todo

1. [x] plan
2. [x] `sessionActions.tsx` + `sessionRowLayout.ts` + `SessionPinIndicator.tsx`
3. [x] sidebar `SessionRow` → ⋯ + inline pin indicator
4. [x] mobile `MobileSessionRow` → shared model
5. [x] chat header ⋯ + `TitleField` imperative rename
6. [x] `chatShortcuts.ts` + ⌘⇧D wiring in `ChatPage`
7. [x] i18n en/zh
8. [x] tests + `npm run build`
