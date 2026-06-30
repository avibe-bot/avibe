# Editor Search (in-file + cross-file) — implementation plan

Phase 3 of the windowed-apps polish batch. Design approved in `design.pen`
frame **`Apps · Editor Search (Dark)`** (`mIK0C`). Two surfaces:

1. **In-file find (⌘F)** — reuse Monaco's built-in find/replace widget. No new
   UI. Work: make ⌘F reach the focused editor (not swallowed by the window /
   browser) and ensure the widget follows the dark theme.
2. **Cross-file search** — new VS Code-style Search view in the editor's
   activity bar (⇧⌘F): query + replace, `Aa`/whole-word/`.*` toggles,
   include/exclude globs, results grouped by file, click-to-jump + highlight,
   replace with preview + one-click undo.

## Locked decisions (2026-06-29)

- **Caps:** truncate at **1000 matches OR 200 files**; response carries a
  `truncated` flag and the UI shows a "结果已截断" notice.
- **Regex:** ship in v1 (toggle alongside plain text).
- **Replace:** preview (inline old→new over the existing results, client-side)
  **and** one-click undo of the whole replace batch.

## Backend — `core/file_browser_service.py` (+ routes in `vibe/ui_server.py`)

Reuse the existing absolute-path safety (`resolve_safe_path` / `_resolve_existing_path`),
`FileBrowserError`, and the atomic temp+rename `write_file` path.

### `search(root, query, *, regex, case_sensitive, whole_word, include, exclude, max_matches=1000, max_files=200)`
- `root` is an existing absolute directory (the folder open in the editor).
- Walk `root` (os.walk, dirs sorted, follow no symlinks). Always skip a small
  default set of noise dirs (`.git`, `node_modules`, `.venv`, `dist`,
  `__pycache__`, etc.) **and** anything matched by `exclude` globs; if `include`
  globs are given, a file must match one. Globs are matched against the path
  relative to `root` (fnmatch, `**` supported via `Path.match`-style or manual).
- Per candidate file: skip if > `SEARCH_MAX_FILE_BYTES` (2 MiB) or binary
  (NUL byte in first 8 KiB). Read text (utf-8, errors="replace"), scan lines.
  - Literal mode: `str.find` loop (respect case + whole-word via boundary
    check). Regex mode: `re.compile` once (case flag); `whole_word` wraps in
    `\b…\b`. Invalid regex → `FileBrowserError("invalid_regex", …, 400)`.
  - Each match: `{line, col, end, text}` where `text` is the (length-capped,
    ~400 char) source line for preview; column/offsets are 0-based within the
    raw line so the UI can render + the editor can select.
- Stop cleanly at the caps; set `truncated=True` and stop walking. Return
  `{root, results:[{path, rel, matches:[…], match_count}], total_matches,
  total_files, truncated, truncated_reason}`.

### `replace(root, query, replacement, *, regex, case_sensitive, whole_word, include, exclude, paths=None)`
- Re-derives matches with the **same** matcher as `search` (single source of
  truth — one private `_compile_matcher()` / `_iter_file_matches()` helper used
  by both). `paths` optionally restricts to a chosen subset (UI "replace in this
  file"); default = all matched files.
- For each file: compute the new content (regex `pattern.sub` with backrefs, or
  literal replace honoring case/whole-word), write atomically. Snapshot the
  original bytes first.
- Returns `{changed:[{path, replacements}], total_replacements, files_changed,
  undo_token}`. Token keys an in-memory, single-use, TTL-bounded snapshot store
  (`_UNDO_STORE`, cap N tokens, ~10 min TTL).

### `undo(token)`
- Restores each snapshotted file **iff** it is unchanged since the replace
  (compare current bytes hash to the post-replace hash we recorded); skips +
  reports any file modified meanwhile. Consumes the token. Returns
  `{restored, skipped}`.

### Routes (mirror existing `/api/files/*` shape, `_dispatch_native_ui_request` + `to_thread`)
- `GET  /api/files/search`  (query params; GET so it's cancel-friendly/cacheable)
- `POST /api/files/search/replace`
- `POST /api/files/search/undo`

## Frontend — `ui/src/components/workbench/`

- **`lib/filesApi.ts`**: add `searchFiles`, `replaceInFiles`, `undoReplace`.
- **In-file find:** ensure the editor's `⌘F`/`⌘⌥F` reach Monaco
  (`editor.getAction('actions.find')`) — Monaco already renders + themes the
  widget (we set `dark` on it). Add a window-level key guard only if something
  swallows it.
- **Search view:** new `EditorSearchView.tsx` rendered in the editor's left
  panel when the activity-bar **search** icon is active (state lifted into
  `EditorApp`). Reuse `ui/src/components/ui` primitives + the dark tokens from
  the design. Results list virtualated-lite (cap already bounds size). Click a
  match → open file in a tab + reveal/select the range in Monaco.
- **Replace preview:** when a replacement is typed, render each result line as
  old (strikethrough) → new inline (client-side, no extra call). Replace-all →
  `replaceInFiles`; show a toast/inline bar "已替换 N 处,可撤销" with an Undo
  action calling `undoReplace(token)`.
- **Shortcuts:** `⌘F` in-file find; `⇧⌘F` open Search view + focus input.
- **i18n:** all strings via `ui/src/i18n/{en,zh}.json`.

## Evidence

- Unit (`tests/test_file_search.py`): matcher (literal/regex/case/whole-word),
  include/exclude + default skips, binary/large skip, caps + truncation flag,
  replace correctness + atomicity, undo restore + skip-when-modified + token
  single-use.
- Build: `npm run build`. Manual: Incus regression — search real repo, jump,
  replace + undo, ⌘F.

## Status
- [ ] Backend search + replace + undo + routes + tests
- [ ] Frontend filesApi + Search view + ⌘F/⇧⌘F + replace preview/undo + i18n
- [ ] Build + deploy + verify + Codex + PR
