# Windowed Apps — File Browser (Finder) + Editor explorer/preview polish

Combined Phase 1 + Phase 2 of the windowed-apps batch (both done on one branch
`feat/apps-finder-explorer`, atomic commits, one PR). Reuse first: existing
`<Markdown>`, `highlighter.ts`, `filePreview.ts`, `FilePicker` inline-new-folder,
`file-viewer` render primitives.

## Phase 1 — File Browser (`AppsFileBrowserPage.tsx`, Finder-like)
1. **Column sort** — Name / Size / Modified headers are buttons; clicking cycles
   asc → desc → none (none = default dirs-first, then name). Active column shows a
   direction caret. Sort persists within the app session (state, not navigation).
   Dirs always group before files within a direction (Finder-like).
2. **Inline new-folder** — replace `window.prompt` with an inline editable row at
   the top of the listing (mirror `FilePicker`'s inline new-folder): Enter creates,
   Esc/blur cancels, name validated via `isPlainEntryName`, dup → inline error.
3. **New File → Editor at cwd** — instead of prompt-create, open the Editor rooted
   at the current dir with a fresh untitled buffer (creation + editing happen in the
   editor; first save lands in cwd). Pass `params: { newFileDir: cwd }`.
4. **Refresh** — already present (toolbar `RefreshCw`); keep.
5. **FloatingApps off in fullscreen** — `AppShell` must not float the Apps launcher
   button over a maximized window (Dock redesign is later). Hide `FloatingApps` when
   a window is maximized.

## Phase 2 — Editor explorer + Preview (`EditorApp.tsx`, `FileTree.tsx`)
6. **Explorer context menu** — right-click: blank → New File / New Folder; file →
   Open / Rename / Delete; folder → New File / New Folder / Rename / Delete. Reuse
   backend `makeDir` / `writeFile(create-only)` / `rename` / `delete`; a small reusable
   context-menu primitive (or `ui/dropdown`).
7. **Explorer collapse + drag-resize** — toggle the tree pane via the activity-bar
   Files icon; drag its right border to resize width (min/max clamp, persisted in
   session). VS Code-like.
8. **Auto-refresh tree after save/create** — when the editor saves a new file
   (save-as) or the context menu mutates, re-list the affected folder in the tree.
9. **Reusable `<FilePreview>`** — one renderer: image (`<img>` fit/zoom), Markdown
   (existing `<Markdown>`), SVG (as image), code (Shiki via `highlighter.ts`). Used by:
   - **File Browser**: double-click a previewable file → preview overlay (not download).
   - **Editor**: open an image → preview pane (no Monaco); Markdown/SVG → a top-right
     **Preview** toggle (source ⇄ rendered), VS Code-like.
   Extend `filePreview.ts` `previewKind` with an `image` kind. Reuse `file-viewer-modal`
   render primitives, NOT the proxy-media-coupled modal itself.

## Evidence
- Build `npm run build` + vitest. Backend unchanged (Phase 1/2 are UI; context-menu
  ops reuse existing `/api/files/*`). Manual: Incus regression — sort, inline new
  folder, new-file→editor, context menu, resize, preview (image/md/svg), no float on
  maximize.

## Status
- [ ] P1: column sort · inline new-folder · new-file→editor · FloatingApps off on maximize
- [ ] P2: explorer context menu · collapse/resize · auto-refresh · reusable FilePreview
- [ ] build + deploy + verify + PR + Codex
