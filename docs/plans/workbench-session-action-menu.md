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

`<SessionActionMenu actions={...} />` renders a labelled `role="group"` of buttons with a
hairline divider between groups, danger styling for archive, `hint` right-aligned in mono (the
⌘⇧D badge), the disabled-fork explanation as an on-screen second line (not `title` alone), and
arrow-key/Home/End roving focus. `<SessionActionMenu…Content>` wraps it in the `PopoverContent`
every surface shares, so the popover's width, alignment and close-focus handling are also
written once. `<SessionActionsTrigger>` is the shared ⋯ button (hover-revealed in rows, always
visible on coarse pointers and while the menu is open).

Semantics are the popover's, not a fake menu's: the container is a Radix `Popover`, which
injects `aria-haspopup="dialog"` + `aria-expanded` into the trigger and owns focus containment.
Claiming `role="menu"` / `role="menuitem"` on top of that would promise AT behavior (type-ahead, single
tab stop, `aria-haspopup="menu"`) that the popover does not implement — see the review round
below.

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

- `inForegroundSurface(target)` / `isArchiveSessionKeydown(event, target)` — the ownership half
  of the decision: the chat stays *mounted* under app windows and dialogs, so "ChatPage is
  rendered" is not "chat owns the keyboard".

Registered by `ChatPage` on `window` (same shape as the shell's ⌘K):

- It **opens the confirm dialog**, never archives directly — a destructive keystroke needs a
  guard, and the dialog already exists.
- Bound **only while `canArchive`** — a read-only or still-loading chat never attaches the
  listener, so it can't `preventDefault()` a chord it won't act on.
- `preventDefault()` (once the chord is ours) so Chrome/Firefox's own ⌘⇧D (bookmark-all-tabs)
  doesn't fire.
- Deliberately wins from inside the composer, like ⌘K — it's a command, not text entry — but
  yields to any app window (`[data-window-id]`, `[data-window-owner-id]`) or dialog stacked over
  the chat.
- Also bound **inside the Show Page iframe** via `bindFrameChord`: an iframe keydown never
  reaches the parent window, so without it the chord silently dies whenever focus is in the
  visualized page.

## Files

- new `ui/src/components/workbench/useSessionActions.tsx` (model + writes)
- new `ui/src/components/workbench/sessionActions.tsx` (descriptor type + ⋯ trigger + menu)
- new `ui/src/components/workbench/chatShortcuts.ts`
- new `ui/src/components/workbench/sessionRowLayout.ts` (replaces `sessionPinLayout.ts`)
- `SessionPinAction.tsx` → `SessionPinIndicator.tsx` (interactive pin deleted, indicator kept)
- `WorkbenchSidebar.tsx`, `ProjectsPage.tsx`, `ChatPage.tsx` — consume the shared model
- `lib/platform.ts` — `isApplePlatform()`
- `context/WorkbenchProjectsContext.ts` + `Provider.tsx` — pin/fork/archive take a **nullable**
  project id (review round, finding 1)
- `components/apps/windowChords.ts` — `bindFrameChord()` generalized out of
  `bindShowPageFrameCloseShortcut()` (review round, finding 6)
- `i18n/en.json` + `zh.json` — reuse existing `workbench.session*` keys; new keys only for the
  chat menu's aria-label if needed

## Tests

- `sessionActions.test.tsx` — menu markup (labelled `role="group"`, grouping/divider, danger
  archive, focusable disabled fork with its visible reason, hint badge, empty list) and the ⋯
  trigger: rendered *inside* a `Popover` it must expose `aria-haspopup="dialog"` +
  `aria-expanded`, and rendered bare it must write neither (asserting them on a bare trigger is
  what made the first version of this test pass while the shipped markup said something else).
- `useSessionActions.test.tsx` — the writes, with the contexts mocked: a project-less session
  reaches `setSessionPinned(null, …)` / `forkSession(null, …)` / `archiveSession(null, …)`, an
  explicit `projectId` overrides the row's own, cancelling the unsaved-changes prompt forks
  **nothing**, a granted authorization wraps the navigation in `runNavigation`, a double click
  forks once, `onSessionPatched` names the initiating session, and a read-only session yields no
  actions, no dialog and `canArchive: false`.
- `SessionPinIndicator.test.tsx` — the passive pin glyph and `sessionRowActionPaddingClass`;
  together with the above it replaces `SessionPinAction.test.tsx`.
- `chatShortcuts.test.ts` — exact chord matching (rejects plain ⌘D, ⌥⌘⇧D, KeyD alone), the
  foreground-surface ownership check, and both platform labels.
- `windowChords.test.ts` — `bindFrameChord` runs an arbitrary chord with the *frame's*
  `activeElement`, steals nothing on a non-match, and detaches on cleanup.
- `ChatArchivedReadOnly.test.tsx` — new case: a read-only header passed a non-empty
  `sessionActions` still renders no ⋯ (`ChatHeaderBar` re-states the withdrawal itself, so the
  guarantee doesn't rest on the hook alone).
- `npm run build` in `ui/`; `ruff check` is a no-op here (frontend-only change).

## Review round — Codex (`gpt-5.6-sol`) on the full diff

Nine findings, all fixed in place. The three blockers were all "the happy path was the only
path":

| # | Finding | Fix |
|---|---|---|
| 1 | **BLOCKER** `project_id` is `null` for every session outside a project (the server derives it from `scope_id`), so pin and fork no-oped and **archive confirmed without archiving** on standalone sessions | provider mutations take `projectId: string \| null`; the API call always runs, only the *cache placement* is skipped. The project id is a cache address, not a permission. |
| 2 | **BLOCKER** fork ran before the unsaved-changes prompt (the guard lived in `onOpenSession`), so cancelling left an **orphan forked session** | `authorizeNavigation()` is a pre-flight — the prompt is synchronous, so it runs before `forkSession`, and the returned authorization is carried into the post-await navigation via `runNavigation`. |
| 3 | **BLOCKER** the window-level ⌘⇧D was scoped to "ChatPage mounted", so it fired for keystrokes owned by app windows/dialogs and `preventDefault()`ed the browser chord even on read-only chats | listener attached only while `canArchive`; `isArchiveSessionKeydown` yields to `[data-window-id]` / `[data-window-owner-id]` / `[role=dialog\|alertdialog]`. |
| 4 | async completions weren't tied to the initiating session | `onSessionPatched(changes, sessionId)`; `ChatPage` patches only if `session.id` still matches. |
| 5 | closing the popover *before* rename/reference let Radix's close-autofocus overwrite the focus the action had just requested | `onCloseAutoFocus` is prevented for the two focus-transferring actions only. |
| 6 | the chord died while focus was inside the Show Page iframe | `bindFrameChord()` generalized out of the ⌥W bridge and bound on the frame too. |
| 7 | the trigger's hand-written `aria-haspopup="menu"` was silently overridden by Radix's `"dialog"` (`asChild` spreads its props last) — and the test asserted the *unwrapped* markup, so it passed | trigger stops writing popover state it doesn't own; the test renders it inside a `Popover` and separately pins the bare-render contract. |
| 8 | `End` focused the wrong item | `Home`/`End` → `focusItem(0)` / `focusItem(actions.length - 1)`. |
| 9 | disabled fork used `disabled`, so it couldn't be focused and its explanation was unreachable by keyboard | `aria-disabled` + `tabIndex` kept, with the reason rendered on screen under the label. |

Round 2 on `0f230780` returned one P2, also valid: the archive confirm dialog kept a bare
`open` boolean, but the hook owning it outlives any one session (`ChatPage` is reused across
session ids), so a request made for A was **inherited** by B — dialog re-appears open and
re-pointed, one Enter from archiving the wrong session. Fixed by storing the requested session
id and deriving `open` from `archiveRequestIsLive(requestedId, targetId)` in
`sessionArchived.ts`, plus forgetting a stale request so it cannot resurrect later.

Round 3 on `cd302fa4` returned one P2, also valid: the chat header forked **before** the
unsaved-changes prompt. The blocker is mounted on the *router* (`UnsavedChangesProvider`'s
`useBlocker`, reached from `RouterRoot`), so any plain `navigate()` prompts only after the write
has already happened — cancelling left an orphan forked session. The sidebar row avoided it by
passing an `authorizeNavigation` option; the chat header simply never passed one, and
`ProjectsPage`'s `MobileSessionRow` had the same hole (a third occurrence the review did not
flag). Fixed at the layer that owns the write instead of per call site: `useSessionActions` now
calls `useUnsavedChangesActionGuard()` itself, gates `fork` on `authorizeNavigation()` returning
non-`null` before touching the API, and navigates through `authorization.runNavigation(...)` so
the router does not prompt twice for one action. The option is gone, so a new surface inherits
the pre-flight rather than having to remember it.

Deliberately not changed: no `@radix-ui/react-dropdown-menu` dependency (not installed; a true
menu role would be the honest fix for #7's *other* half, but the popover semantics are now
truthful about what they are).

## Todo

1. [x] plan
2. [x] `sessionActions.tsx` + `sessionRowLayout.ts` + `SessionPinIndicator.tsx`
3. [x] sidebar `SessionRow` → ⋯ + inline pin indicator
4. [x] mobile `MobileSessionRow` → shared model
5. [x] chat header ⋯ + `TitleField` imperative rename
6. [x] `chatShortcuts.ts` + ⌘⇧D wiring in `ChatPage`
7. [x] i18n en/zh
8. [x] tests + `npm run build`
9. [x] Codex review round — 9 findings fixed + `useSessionActions.test.tsx`
10. [x] round 2 — per-session archive request (`archiveRequestIsLive`)
11. [x] round 3 — unsaved-changes pre-flight owned by `useSessionActions`
