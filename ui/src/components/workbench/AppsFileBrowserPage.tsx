import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Braces,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  ClipboardPaste,
  Copy,
  Download,
  FileCode2,
  FileSearch,
  FileText,
  File as FileIcon,
  Folder,
  FolderInput,
  FolderPlus,
  FilePlus,
  HardDrive,
  Hash,
  Home,
  Image as ImageIcon,
  Loader2,
  Monitor,
  Pencil,
  RefreshCw,
  Search,
  SquareTerminal,
  Trash2,
  Undo2,
  Upload,
  X,
  type LucideIcon,
} from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { isEditableFile, isEditableMeta, previewOverlayKind, previewWindowKind } from '../../lib/filePreview';
import {
  contentUrl,
  copyPath,
  deletePath,
  downloadFile,
  undoDelete,
  fileBrowserErrorMessage,
  fileMeta,
  FilesApiError,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  MAX_UPLOAD_BYTES,
  movePath,
  parentDir,
  pathCrumbs,
  renamePath,
  searchFiles,
  searchNames,
  systemFavorites,
  uploadFile,
  writeFile,
  type Favorite,
  type FsEntry,
  type FsListing,
  type NameHit,
  type SearchFileResult,
} from '../../lib/filesApi';
import { Button } from '../ui/button';
import { Checkbox } from '../ui/checkbox';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { ContextMenu, ContextMenuItem } from '../ui/context-menu';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { FilePreview } from '../ui/file-preview';
import { InlineNameInput } from '../ui/inline-name-input';
import { FilePicker } from './FilePicker';

// A code-file extension → its accent + glyph (mirrors design nknn2's colored type icons).
const EXT_ICON: Record<string, { Icon: LucideIcon; color: string }> = {
  ts: { Icon: FileCode2, color: 'var(--cyan)' },
  tsx: { Icon: FileCode2, color: 'var(--cyan)' },
  js: { Icon: FileCode2, color: 'var(--gold)' },
  jsx: { Icon: FileCode2, color: 'var(--gold)' },
  json: { Icon: Braces, color: 'var(--gold)' },
  css: { Icon: Hash, color: 'var(--violet)' },
  scss: { Icon: Hash, color: 'var(--violet)' },
  md: { Icon: FileText, color: 'var(--mint)' },
  markdown: { Icon: FileText, color: 'var(--mint)' },
  png: { Icon: ImageIcon, color: 'var(--muted)' },
  jpg: { Icon: ImageIcon, color: 'var(--muted)' },
  jpeg: { Icon: ImageIcon, color: 'var(--muted)' },
  svg: { Icon: ImageIcon, color: 'var(--muted)' },
};

function entryIcon(e: FsEntry): { Icon: LucideIcon; color: string } {
  if (e.kind === 'dir') return { Icon: Folder, color: 'var(--cyan)' };
  return EXT_ICON[e.ext?.toLowerCase()] ?? { Icon: FileIcon, color: 'var(--muted)' };
}

// A favorite's key → a distinct icon (mirrors the Finder rail in design nknn2:
// Home / Desktop / Downloads / Documents / drive). Unknown keys fall back to a folder.
const FAV_ICON: Record<string, LucideIcon> = {
  home: Home,
  desktop: Monitor,
  downloads: Download,
  documents: FileText,
  root: HardDrive,
};

// One row in the listing OR the recursive-search results. `full` is the absolute path; `dir` is its
// parent (where rename/delete/move resolve); `rel` (search hits only) is the path relative to the
// search root, so a nested hit can show the folder it lives in; `matchCount` (content-search hits
// only) is how many matches the file had, shown as a small chip on the row.
type RowItem = { entry: FsEntry; full: string; dir: string; rel?: string; matchCount?: number };

function formatSize(n: number | null): string {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function formatMtime(seconds: number | null): string {
  if (seconds == null) return '—';
  // The backend returns mtime in SECONDS (st_mtime_ns / 1e9); Date expects milliseconds.
  const d = new Date(seconds * 1000);
  const now = Date.now();
  const sameYear = d.getFullYear() === new Date(now).getFullYear();
  const date = d.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { year: 'numeric', month: 'short', day: 'numeric' });
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return `${date} ${time}`;
}

// The folder an entry lives in, relative to the search root (for search-result rows). Empty for a
// direct child of the search root (rel is just the name).
function relFolder(rel: string | undefined): string {
  if (!rel || !rel.includes('/')) return '';
  return rel.slice(0, rel.lastIndexOf('/'));
}

// A recursive NAME-search hit → a RowItem. `dir` (the parent, where open/rename/move resolve) is
// derived from the absolute path; `rel` (relative to the search root) drives the folder label.
function nameHitRow(h: NameHit): RowItem {
  return { entry: h, full: h.path, dir: parentDir(h.path), rel: h.rel };
}

// A CONTENT-search hit (a file whose text matched) → a RowItem. Content search returns no dir/size,
// so synthesize the entry from the path: kind is always 'file', name/ext from the basename, size
// null. `matchCount` renders as a per-row chip; opening behaves exactly like a name-search file row.
function contentHitRow(r: SearchFileResult): RowItem {
  const name = r.rel.slice(r.rel.lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot + 1) : '';
  return {
    entry: { name, kind: 'file', size: null, mtime: r.mtime, ext },
    full: r.path,
    dir: parentDir(r.path),
    rel: r.rel,
    matchCount: r.match_count,
  };
}

// Content search (searchFiles) has no show_hidden option, so when "Show hidden files" is off we ask
// the backend to EXCLUDE hidden entries via its glob `exclude` (the same mechanism the editor's
// cross-file search uses). `.*` — deliberately slash-free — matches any dotfile basename AND, because
// slash-free excludes also prune walked directory names, skips hidden dot-directories at every depth.
// Doing it in the REQUEST (not client-side after the fact) means hidden hits never consume the
// backend's file/match cap, so visible matches can't be crowded out by many hidden ones.
const HIDDEN_EXCLUDE_GLOB = '.*';

// Keep-both cap for the move name-clash dialog: after this many same-named copies we stop retrying
// and report failure rather than spin. A destination holding ~100 identical names is pathological.
const MAX_KEEP_BOTH = 99;

// Build a de-duplicated entry name for "Keep both": `report.txt` → `report (2).txt`. Splits at the
// LAST dot so a compound extension keeps its tail (`a.tar.gz` → `a.tar (2).gz`); a dotfile /
// extensionless name (`.env`, `Makefile`) gets the counter appended whole.
function dedupeName(name: string, n: number): string {
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return `${name} (${n})`;
  return `${name.slice(0, dot)} (${n})${name.slice(dot)}`;
}

// Upload parallelism cap: a big multi-select / drop uploads at most this many files at once so a
// large batch doesn't flood the endpoint (mirrors the chat composer's bounded upload pool).
const UPLOAD_CONCURRENCY = 3;

// Browsers increasingly gate or throttle bursts of automatic downloads. Keep a batch intentional
// and bounded; the UI reports how many selected files were left for a follow-up batch.
const MAX_BATCH_DOWNLOADS = 10;

const LONG_PRESS_MS = 500;
const LONG_PRESS_MOVE_PX = 8;

// Whole-machine Finder: favorites/projects rail + a Name/Size/Modified list + a toolbar (breadcrumb,
// search, New File/Folder) + a status bar. Right-click a row for Open/Download/Rename/Delete, or
// blank space for New File/Folder; drag a row onto a folder (row, rail, or breadcrumb) to move it;
// the search box does a recursive file/folder NAME search under the current folder. Double-clicking a
// text/code file opens it in the Editor window. Backend contract: ui/src/lib/filesApi.ts →
// /api/files/*. Design: design.pen `nknn2`.
export const AppsFileBrowserPage: React.FC<{ windowed?: boolean; windowId?: string }> = ({ windowed = false }) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const routerNavigate = useNavigate();
  const { projects } = useWorkbenchProjectsTree();
  const [cwd, setCwd] = useState('');
  const [listing, setListing] = useState<FsListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [sysFavs, setSysFavs] = useState<Favorite[]>([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [touchSelectionMode, setTouchSelectionMode] = useState(false);
  const selectionAnchorRef = useRef<string | null>(null);
  const clearSelection = useCallback(() => {
    setSelected(new Set());
    setTouchSelectionMode(false);
    selectionAnchorRef.current = null;
  }, []);

  // Escape is the universal way out of a selection. Preview/dialog handlers still own their own
  // Escape behavior; clearing a background selection at the same time is harmless and predictable.
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') clearSelection();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [clearSelection]);

  // Long-press is only a selection gesture on coarse pointers. Movement beyond a small slop cancels
  // it so scrolling the list never unexpectedly enters selection mode.
  const longPressRef = useRef<{ path: string; x: number; y: number; timer: number } | null>(null);
  const suppressTouchClickRef = useRef<string | null>(null);
  const cancelLongPress = useCallback(() => {
    if (longPressRef.current) window.clearTimeout(longPressRef.current.timer);
    longPressRef.current = null;
  }, []);
  useEffect(() => cancelLongPress, [cancelLongPress]);
  // Column sort, Finder-like: click a header to cycle asc → desc → none (none = default
  // dirs-first then name). Persists within the app session; folders always group before files.
  const [sort, setSort] = useState<{ col: 'name' | 'size' | 'modified'; dir: 'asc' | 'desc' } | null>(null);
  const cycleSort = useCallback(
    (col: 'name' | 'size' | 'modified') =>
      setSort((s) => (s?.col !== col ? { col, dir: 'asc' } : s.dir === 'asc' ? { col, dir: 'desc' } : null)),
    [],
  );
  // Quick-look image preview: a raster image opens in an in-window overlay (Finder-style) instead
  // of downloading. Kept in-window (not a portaled Dialog) so it stays inside the window's dark
  // data-theme scope and bounds.
  const [preview, setPreview] = useState<{ path: string; name: string } | null>(null);
  useEffect(() => {
    if (!preview) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') {
        ev.preventDefault();
        setPreview(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [preview]);

  const navSeq = useRef(0);
  const navigate = useCallback(
    (path: string) => {
      const seq = ++navSeq.current;
      setLoading(true);
      setError(null);
      clearSelection();
      listDir(path, showHidden)
        .then((r) => {
          if (seq !== navSeq.current) return;
          setCwd(r.path);
          setListing(r);
        })
        .catch((e: unknown) => {
          if (seq === navSeq.current) setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.listFailed')));
        })
        .finally(() => {
          if (seq === navSeq.current) setLoading(false);
        });
    },
    [showHidden, t, clearSelection],
  );

  useEffect(() => {
    systemFavorites().then(setSysFavs).catch(() => {});
  }, []);

  useEffect(() => {
    if (cwd) return;
    if (projects === null) return;
    const initial = projects?.[0]?.folder_path || sysFavs.find((f) => f.key === 'home')?.path;
    if (initial) navigate(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects, sysFavs]);

  useEffect(() => {
    if (cwd) navigate(cwd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showHidden]);

  // ---- Recursive search: file/folder NAME (default) or file CONTENT ------------------------------
  // A non-empty query switches the listing to recursive results under `cwd`. The toggle in the search
  // box picks NAME search (backend: /api/files/search_names) or CONTENT search (/api/files/search —
  // the same grep the editor's cross-file search uses). Both are debounced and abortable so fast
  // typing doesn't pile up stale requests, and both normalize to RowItem so the listing renders them
  // identically; content hits carry a `matchCount` chip.
  const [searchMode, setSearchMode] = useState<'name' | 'content'>('name');
  const [searchRows, setSearchRows] = useState<RowItem[] | null>(null);
  const [searchTruncated, setSearchTruncated] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const searchSeq = useRef(0);
  const searchAbort = useRef<AbortController | null>(null);
  const inSearch = query.trim().length > 0;

  // Search results are a different row model from the current-folder listing. Clear selection when
  // the query or Name/Content mode changes so invisible/stale paths can never be batch-mutated.
  useEffect(() => {
    clearSelection();
  }, [query, searchMode, clearSelection]);

  const runSearch = useCallback(
    (raw: string) => {
      const q = raw.trim();
      searchAbort.current?.abort();
      if (!q || !cwd) {
        setSearchRows(null);
        setSearchBusy(false);
        return;
      }
      const ac = new AbortController();
      searchAbort.current = ac;
      const seq = ++searchSeq.current;
      setSearchBusy(true);
      const search =
        searchMode === 'content'
          ? searchFiles(cwd, q, showHidden ? {} : { exclude: HIDDEN_EXCLUDE_GLOB }, ac.signal).then((r) => ({
              rows: r.results.map(contentHitRow),
              truncated: r.truncated,
            }))
          : searchNames(cwd, q, showHidden, ac.signal).then((r) => ({ rows: r.results.map(nameHitRow), truncated: r.truncated }));
      search
        .then(({ rows: hitRows, truncated }) => {
          if (seq !== searchSeq.current) return;
          setSearchRows(hitRows);
          setSearchTruncated(truncated);
        })
        .catch((e: unknown) => {
          if (seq !== searchSeq.current || (e as { name?: string })?.name === 'AbortError') return;
          setSearchRows([]);
          setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.searchFailed')));
        })
        .finally(() => {
          if (seq === searchSeq.current) setSearchBusy(false);
        });
    },
    [cwd, showHidden, searchMode, t],
  );

  // Debounce the search as the user types; also re-fires when cwd/showHidden change (runSearch
  // identity changes), so switching folders re-scopes an active search.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      searchAbort.current?.abort();
      setSearchRows(null);
      setSearchBusy(false);
      return;
    }
    const id = window.setTimeout(() => runSearch(query), 220);
    return () => window.clearTimeout(id);
  }, [query, runSearch]);

  useEffect(() => () => searchAbort.current?.abort(), []);

  // After a mutation: re-list the current folder, and re-run the search if one is active.
  const refreshAll = useCallback(() => {
    if (cwd) navigate(cwd);
    if (query.trim()) runSearch(query);
  }, [cwd, navigate, query, runSearch]);

  // Open an editable text/code file in the Editor: desktop opens a resizable Editor window; phone —
  // which has no window layer — navigates to the full-page editor route. The launch file rides in
  // router state, mirroring the window's `params`. Re-checks the breakpoint (like onNewFile) so it
  // routes correctly regardless of where it's called from.
  const openInEditor = useCallback(
    (path: string, filename: string, mtime: number | null) => {
      if (window.matchMedia('(min-width: 768px)').matches) {
        wm.openApp('editor', { title: filename, params: { path, filename, mtime } });
      } else {
        routerNavigate('/apps/editor', { state: { path, filename, mtime } });
      }
    },
    [wm, routerNavigate],
  );

  // Open a terminal rooted at a folder ("Open Terminal Here"): desktop opens a Terminal window
  // whose first tab starts in `dir`; mobile — which has no window layer — navigates to the terminal
  // route with the dir in router state. Mirrors openInEditor.
  const openTerminalHere = useCallback(
    (dir: string) => {
      if (window.matchMedia('(min-width: 768px)').matches) {
        wm.openApp('terminal', { params: { cwd: dir } });
      } else {
        routerNavigate('/apps/terminal', { state: { cwd: dir } });
      }
    },
    [wm, routerNavigate],
  );

  // Open: dir → navigate (and leave search); image / PDF / Office / Markdown → the standalone Preview
  // window (desktop) or the in-page overlay (mobile, which has no window layer); editable text/code
  // file (within the size cap) → Editor (desktop window / mobile route); anything else → download.
  const openItem = async (item: RowItem) => {
    if (item.entry.kind === 'dir') {
      setQuery('');
      navigate(item.full);
      return;
    }
    const desktop = window.matchMedia('(min-width: 768px)').matches;
    // Content-search hits carry no size (the search API omits it), so the synchronous size gates in
    // previewWindowKind / previewOverlayKind would treat them as unbounded and could route an
    // oversized file (e.g. a >1 MB Markdown) into a preview the listing path would refuse. For those,
    // fetch metadata up front and route with the REAL size + text sniff — so a content hit opens
    // exactly like the same file from a listing/name row.
    if (item.matchCount != null) {
      try {
        const m = await fileMeta(item.full);
        const sized: FsEntry = { ...item.entry, size: m.size };
        if (desktop && previewWindowKind(sized)) {
          wm.openApp('preview', { title: sized.name, params: { path: item.full, name: sized.name } });
        } else if (!desktop && previewOverlayKind(sized)) {
          setPreview({ path: item.full, name: sized.name });
        } else if (isEditableMeta(m)) {
          openInEditor(item.full, sized.name, m.mtime);
        } else {
          downloadFile(item.full);
        }
      } catch {
        // Meta unavailable: best-effort by name, mirroring the fallback in the listing path below.
        if (isEditableFile(item.entry)) openInEditor(item.full, item.entry.name, item.entry.mtime);
        else downloadFile(item.full);
      }
      return;
    }
    if (desktop) {
      // Desktop: image / PDF / Office / Markdown open the dedicated, resizable Preview window.
      if (previewWindowKind(item.entry)) {
        wm.openApp('preview', { title: item.entry.name, params: { path: item.full, name: item.entry.name } });
        return;
      }
    } else if (previewOverlayKind(item.entry)) {
      // Mobile has no window layer: only NON-editable rich files (image / PDF / Office) open the
      // in-page overlay. Markdown/SVG are previewable too but ALSO editable, so they fall through to
      // the editor below (it has its own Source⇄Preview toggle) instead of a read-only overlay.
      setPreview({ path: item.full, name: item.entry.name });
      return;
    }
    // Fetch CURRENT metadata (content-sniffs `text`) and decide by CONTENT, not just the extension —
    // so an extensionless TEXT file opens in the editor while a true binary downloads. downloadFile
    // uses an anchor (not a popup), so it survives this awaited recheck without losing the tap's user
    // activation on Safari/iOS.
    try {
      const m = await fileMeta(item.full);
      if (isEditableMeta(m)) {
        openInEditor(item.full, item.entry.name, m.mtime);
      } else {
        downloadFile(item.full);
      }
    } catch {
      if (isEditableFile(item.entry)) {
        openInEditor(item.full, item.entry.name, item.entry.mtime);
      } else {
        downloadFile(item.full);
      }
    }
  };

  // New File / New Folder: an inline editable row in the listing (mirrors FilePicker). Starting one
  // clears any active search so the create row is visible in the current folder.
  const [newEntry, setNewEntry] = useState<{ kind: 'file' | 'folder' } | null>(null);
  const startNewEntry = useCallback((kind: 'file' | 'folder') => {
    setError(null);
    setQuery('');
    setNewEntry({ kind });
  }, []);
  const commitNewEntry = useCallback(
    async (kind: 'file' | 'folder', value: string) => {
      const name = value.trim();
      if (name === '') {
        setNewEntry(null);
        return;
      }
      if (!isPlainEntryName(name)) {
        setError(t('apps.fileBrowser.errors.invalid_name'));
        return;
      }
      try {
        // create-only on files: the backend atomically refuses a name clash, so a typo can't clobber.
        if (kind === 'folder') await makeDir(joinPath(cwd, name));
        else await writeFile(joinPath(cwd, name), '', undefined, true);
        setNewEntry(null);
        refreshAll();
      } catch (e: unknown) {
        setError(
          fileBrowserErrorMessage(e, t, t(kind === 'folder' ? 'apps.fileBrowser.errors.createFolderFailed' : 'apps.fileBrowser.errors.saveFailed')),
        );
      }
    },
    [cwd, refreshAll, t],
  );

  // New File: on DESKTOP open the Editor rooted at the current dir with a fresh untitled buffer
  // (richer creation + editing flow; first save lands in cwd). On mobile the editor window layer is
  // hidden, so fall back to the inline create row so a file can still be made.
  const onNewFile = useCallback(() => {
    if (!cwd) return;
    if (window.matchMedia('(min-width: 768px)').matches) {
      wm.openApp('editor', { title: t('apps.fileBrowser.newFile'), params: { newFileDir: cwd } });
    } else {
      startNewEntry('file');
    }
  }, [cwd, wm, t, startNewEntry]);

  // ---- Rename (inline) + Delete ----------------------------------------------------------------
  const [rename, setRename] = useState<{ full: string } | null>(null);
  const startRename = useCallback((item: RowItem) => {
    setError(null);
    setRename({ full: item.full });
  }, []);
  const commitRename = useCallback(
    async (item: RowItem, value: string) => {
      const name = value.trim();
      if (name === '' || name === item.entry.name) {
        setRename(null);
        return;
      }
      if (!isPlainEntryName(name)) {
        setError(t('apps.fileBrowser.errors.invalid_name'));
        return;
      }
      try {
        await renamePath(item.full, name);
        setRename(null);
        setError(null);
        refreshAll();
      } catch (e: unknown) {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.renameFailed')));
      }
    },
    [refreshAll, t],
  );

  // ---- Undo bar (delete + drag-move) -------------------------------------------------------------
  // One entry at a time: a new undoable action replaces the bar (the previous action simply ages out
  // of its window — matching the backend's bounded staging). Delete reverts via the backend token;
  // move reverts with a plain reverse move. The bar auto-dismisses; dismissal ≠ token expiry (the
  // backend keeps staged deletes far longer), it just keeps the UI calm.
  type DeleteUndoItem = { label: string; token: string };
  type MoveUndoItem = { label: string; from: string; to: string };
  type UndoEntry =
    | { kind: 'delete'; label: string; count: number; items: DeleteUndoItem[] }
    | { kind: 'move'; label: string; count: number; items: MoveUndoItem[] };
  const [undoEntry, setUndoEntry] = useState<UndoEntry | null>(null);
  const [undoBusy, setUndoBusy] = useState(false);
  const undoTimerRef = useRef<number | null>(null);
  const showUndo = useCallback((entry: UndoEntry) => {
    if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
    setUndoEntry(entry);
    undoTimerRef.current = window.setTimeout(() => setUndoEntry(null), 8000);
  }, []);
  useEffect(
    () => () => {
      if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
    },
    [],
  );
  const performUndo = useCallback(async () => {
    const entry = undoEntry;
    if (!entry || undoBusy) return;
    if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
    setUndoBusy(true);
    if (entry.kind === 'delete') {
      const failures: { item: DeleteUndoItem; error: unknown }[] = [];
      for (const item of entry.items) {
        try {
          await undoDelete(item.token);
        } catch (error) {
          failures.push({ item, error });
        }
      }
      refreshAll();
      if (failures.length === 0) {
        setError(null);
        setUndoEntry((cur) => (cur === entry ? null : cur));
      } else {
        setError(
          failures.length === 1 && entry.items.length === 1
            ? fileBrowserErrorMessage(failures[0].error, t, t('apps.fileBrowser.errors.undoFailed'))
            : failures
                .map(({ item, error }) =>
                  t('apps.fileBrowser.operationError', {
                    name: item.label,
                    message: fileBrowserErrorMessage(error, t, t('apps.fileBrowser.errors.undoFailed')),
                  }),
                )
                .join(' · '),
        );
        // Restored tokens are consumed. Keep only retryable failures; exists/expired can never
        // succeed later and must not leave a dead Undo button behind.
        const retryable = failures
          .filter(({ error }) => !(error instanceof FilesApiError && (error.code === 'exists' || error.code === 'expired')))
          .map(({ item }) => item);
        setUndoEntry((cur) => {
          if (cur !== entry) return cur;
          if (retryable.length === 0) return null;
          undoTimerRef.current = window.setTimeout(() => setUndoEntry(null), 8000);
          return { ...entry, label: retryable[0].label, count: retryable.length, items: retryable };
        });
      }
      setUndoBusy(false);
      return;
    }
    const failures: { item: MoveUndoItem; error: unknown }[] = [];
    // Reverse the original move order so nested or otherwise dependent selections unwind safely.
    for (const item of [...entry.items].reverse()) {
      try {
        await movePath(item.from, item.to);
      } catch (error) {
        failures.push({ item, error });
      }
    }
    refreshAll();
    if (failures.length === 0) {
      setError(null);
      setUndoEntry((cur) => (cur === entry ? null : cur));
    } else {
      setError(
        failures.length === 1 && entry.items.length === 1
          ? fileBrowserErrorMessage(failures[0].error, t, t('apps.fileBrowser.errors.undoFailed'))
          : failures
              .map(({ item, error }) =>
                t('apps.fileBrowser.operationError', {
                  name: item.label,
                  message: fileBrowserErrorMessage(error, t, t('apps.fileBrowser.errors.undoFailed')),
                }),
              )
              .join(' · '),
      );
      const retryable = failures
        .filter(({ error }) => !(error instanceof FilesApiError && (error.code === 'exists' || error.code === 'expired')))
        .map(({ item }) => item);
      setUndoEntry((cur) => {
        if (cur !== entry) return cur;
        if (retryable.length === 0) return null;
        undoTimerRef.current = window.setTimeout(() => setUndoEntry(null), 8000);
        return { ...entry, label: retryable[0].label, count: retryable.length, items: retryable };
      });
    }
    setUndoBusy(false);
  }, [undoEntry, undoBusy, refreshAll, t]);

  // ---- Delete (product ConfirmDialog + post-delete Undo) ----------------------------------------
  // The context menu only REQUESTS the delete; the destructive ConfirmDialog performs it. When the
  // backend staged the entry (undo_token non-null) the undo bar offers a short revert window; a
  // permanent delete (cross-device / oversized) simply shows no bar.
  const [pendingDelete, setPendingDelete] = useState<RowItem[] | null>(null);
  const removeItems = useCallback((items: RowItem[]) => {
    setMenu(null);
    setPendingDelete(items);
  }, []);
  const performDelete = useCallback(async () => {
    const items = pendingDelete;
    if (!items?.length) return;
    const undoItems: DeleteUndoItem[] = [];
    const failures: string[] = [];
    let deletedCount = 0;
    for (const item of items) {
      try {
        const result = await deletePath(item.full, item.entry.kind === 'dir');
        deletedCount += 1;
        if (result.undo_token) undoItems.push({ label: item.entry.name, token: result.undo_token });
      } catch (e: unknown) {
        const message = fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.deleteFailed'));
        failures.push(items.length === 1 ? message : t('apps.fileBrowser.operationError', { name: item.entry.name, message }));
      }
    }
    if (undoItems.length > 0) {
      showUndo({ kind: 'delete', label: undoItems[0].label, count: undoItems.length, items: undoItems });
    } else if (deletedCount > 0) {
      // No staged delete in this action is reversible, so an older bar may now refer to a path this
      // action permanently removed. The newest mutation owns the bar and offers no false Undo.
      if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
      setUndoEntry(null);
    }
    setPendingDelete(null);
    if (deletedCount > 0) refreshAll();
    setError(failures.length ? failures.join(' · ') : null);
  }, [pendingDelete, t, refreshAll, showUndo]);

  // ---- Context menu ----------------------------------------------------------------------------
  // `item` is null for blank space (offers New File/Folder only).
  const [menu, setMenu] = useState<{ x: number; y: number; item: RowItem | null; items: RowItem[] } | null>(null);
  const openMenu = useCallback((ev: React.MouseEvent, item: RowItem | null, items: RowItem[] = []) => {
    ev.preventDefault();
    ev.stopPropagation();
    setMenu({ x: ev.clientX, y: ev.clientY, item, items });
  }, []);
  const closeMenu = useCallback(() => setMenu(null), []);

  // ---- Drag-and-drop move ----------------------------------------------------------------------
  // The dragged row is held in a ref (no re-render mid-drag); `dropTarget` (a folder path) drives the
  // hover highlight. Folders — rows, rail favorites/projects, and breadcrumbs — are drop targets.
  const dragRef = useRef<RowItem[] | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  // Move `item` into `destDir` under `name`. On success: clear the error and refresh. A name clash
  // (errors.exists) is NOT swallowed here — it throws so the caller (the drag drop, or the clash
  // dialog's retries) can react. A non-overwriting move offers a reverse-move Undo (a mis-drop is
  // easy — drop targets are everywhere — and the reverse needs no backend support, just move it
  // back); an overwrite (Replace) offers none, since the clobbered file is gone and can't be
  // losslessly restored, so we don't imply it can.
  const applyMove = useCallback(
    async (item: RowItem, destDir: string, name: string, overwrite: boolean, refreshAfter = true, manageUndo = true) => {
      const moved = joinPath(destDir, name);
      await movePath(item.full, moved, overwrite);
      if (refreshAfter) setError(null);
      const undoItem: MoveUndoItem = { label: name, from: moved, to: item.full };
      if (overwrite) {
        // Replace is a non-undoable mutation: any older move/delete undo bar now describes a stale
        // world (its paths may point at the entry we just clobbered), so clear it — mirroring the
        // permanent-delete path — rather than leaving a stale Undo that could move the wrong file.
        if (manageUndo) {
          if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
          setUndoEntry(null);
        }
      } else if (manageUndo) {
        showUndo({ kind: 'move', label: name, count: 1, items: [undoItem] });
      }
      if (refreshAfter) refreshAll();
      return overwrite ? null : undoItem;
    },
    [showUndo, refreshAll],
  );

  // ---- Move/copy name-clash dialog (Replace / Keep both / Cancel) -------------------------------
  // Batch operations are deliberately sequential. A conflicting item pauses the queue on the same
  // dialog used by the original drag-move flow; resolving it resumes with the next selected item.
  type NameClashRequest = {
    item: RowItem;
    destDir: string;
    run: (action: 'replace' | 'keep') => Promise<void>;
    resolve: (completed: boolean) => void;
    reject: (error: unknown) => void;
  };
  const [nameClash, setNameClash] = useState<NameClashRequest | null>(null);
  const [nameClashBusy, setNameClashBusy] = useState<'replace' | 'keep' | null>(null);
  const pendingNameClashRef = useRef<NameClashRequest | null>(null);
  useEffect(() => {
    pendingNameClashRef.current = nameClash;
  }, [nameClash]);

  const requestNameClash = useCallback(
    (item: RowItem, destDir: string, run: NameClashRequest['run']) =>
      new Promise<boolean>((resolve, reject) => setNameClash({ item, destDir, run, resolve, reject })),
    [],
  );
  const answerNameClash = useCallback((completed: boolean) => {
    const request = pendingNameClashRef.current;
    if (!request) return;
    request.resolve(completed);
    setNameClash(null);
  }, []);
  const runNameClash = useCallback(async (action: 'replace' | 'keep') => {
    const request = pendingNameClashRef.current;
    if (!request) return;
    setNameClashBusy(action);
    try {
      await request.run(action);
      request.resolve(true);
    } catch (error) {
      request.reject(error);
    } finally {
      setNameClash(null);
      setNameClashBusy(null);
    }
  }, []);

  const moveOne = useCallback(
    async (item: RowItem, destDir: string, refreshAfter: boolean, manageUndo: boolean) => {
      try {
        const undo = await applyMove(item, destDir, item.entry.name, false, refreshAfter, manageUndo);
        return { completed: true, undo };
      } catch (error) {
        if (!(error instanceof FilesApiError) || error.code !== 'exists') throw error;
      }
      let undo: MoveUndoItem | null = null;
      const completed = await requestNameClash(item, destDir, async (action) => {
        if (action === 'replace') {
          undo = await applyMove(item, destDir, item.entry.name, true, refreshAfter, manageUndo);
          return;
        }
        for (let n = 2; n <= MAX_KEEP_BOTH; n++) {
          try {
            undo = await applyMove(item, destDir, dedupeName(item.entry.name, n), false, refreshAfter, manageUndo);
            return;
          } catch (error) {
            if (error instanceof FilesApiError && error.code === 'exists') continue;
            throw error;
          }
        }
        throw new Error(t('apps.fileBrowser.moveClashTooMany'));
      });
      return { completed, undo };
    },
    [applyMove, requestNameClash, t],
  );

  const moveItemsInto = useCallback(
    async (items: RowItem[], destDir: string) => {
      const candidates = items.filter((item) => item.dir !== destDir && item.full !== destDir);
      if (candidates.length === 0) return;
      const batch = candidates.length > 1;
      const failures: string[] = [];
      const batchUndo: MoveUndoItem[] = [];
      let batchUndoable = true;
      let moved = 0;
      for (const item of candidates) {
        try {
          const result = await moveOne(item, destDir, !batch, !batch);
          if (result.completed) {
            moved += 1;
            if (result.undo) batchUndo.push(result.undo);
            else batchUndoable = false;
          }
        } catch (error) {
          const message = fileBrowserErrorMessage(error, t, t('apps.fileBrowser.errors.moveFailed'));
          failures.push(batch ? t('apps.fileBrowser.operationError', { name: item.entry.name, message }) : message);
        }
      }
      if (batch && moved > 0) {
        if (batchUndoable && batchUndo.length > 0) {
          showUndo({ kind: 'move', label: batchUndo[0].label, count: batchUndo.length, items: batchUndo });
        } else {
          // One Replace makes the batch only partially reversible. Offer no Undo rather than imply
          // the overwritten destination can be recovered alongside the ordinary moves.
          if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
          setUndoEntry(null);
        }
        refreshAll();
      }
      if (batch || failures.length > 0) setError(failures.length ? failures.join(' · ') : null);
    },
    [moveOne, refreshAll, showUndo, t],
  );

  const moveInto = useCallback(
    async (destDir: string) => {
      const items = dragRef.current;
      dragRef.current = null;
      setDropTarget(null);
      if (items) await moveItemsInto(items, destDir);
    },
    [moveItemsInto],
  );

  const [movePickerItems, setMovePickerItems] = useState<RowItem[] | null>(null);
  // Drop-target props for any folder (row / rail / breadcrumb). Only active while a drag is in flight
  // and never onto the dragged item itself.
  const dropProps = (destDir: string) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragRef.current || dragRef.current.some((item) => item.full === destDir)) return;
      e.preventDefault();
      setDropTarget((d) => (d === destDir ? d : destDir));
    },
    onDragLeave: () => setDropTarget((d) => (d === destDir ? null : d)),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      void moveInto(destDir);
    },
  });

  // Promise-bridged replace prompt: the upload workers are async, so the dialog answer comes back
  // as a promise. Prompts are CHAINED — with parallel workers, two simultaneous name clashes must
  // not overwrite each other's pending dialog; each waits for the previous answer.
  const [replaceAsk, setReplaceAsk] = useState<{ name: string; resolve: (ok: boolean) => void } | null>(null);
  const askChainRef = useRef<Promise<unknown>>(Promise.resolve());
  // Unmount (window closed mid-question) must not hang the upload workers: the CURRENT pending ask
  // resolves `false` from the cleanup below, and QUEUED asks short-circuit to `false` instead of
  // setState-ing an unmounted component (which would leave their promises forever pending).
  const unmountedRef = useRef(false);
  const pendingAskRef = useRef<{ resolve: (ok: boolean) => void } | null>(null);
  useEffect(() => {
    pendingAskRef.current = replaceAsk;
  }, [replaceAsk]);
  useEffect(
    () => () => {
      unmountedRef.current = true;
      pendingAskRef.current?.resolve(false);
      pendingNameClashRef.current?.resolve(false);
    },
    [],
  );
  const confirmReplace = useCallback((name: string) => {
    const next = askChainRef.current.then(() =>
      unmountedRef.current ? false : new Promise<boolean>((resolve) => setReplaceAsk({ name, resolve })),
    );
    askChainRef.current = next.catch(() => false);
    return next;
  }, []);
  const answerReplace = useCallback(
    (ok: boolean) => {
      replaceAsk?.resolve(ok);
      setReplaceAsk(null);
    },
    [replaceAsk],
  );

  // ---- Upload (toolbar button + OS drag-drop) --------------------------------------------------
  // Files land in the folder that was current when the upload STARTED (`dest`), uploaded with a
  // bounded worker pool. Progress + per-file failures surface in the toolbar/status bar and the
  // existing error strip; a name clash (409) prompts per file to replace.
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [uploadProgress, setUploadProgress] = useState<{ done: number; total: number } | null>(null);
  const [fileDragOver, setFileDragOver] = useState(false);
  // refreshAll re-lists whatever folder is current WHEN IT FIRES, so an upload that finishes after
  // the user navigated away refreshes their new folder instead of yanking them back to `dest`.
  const refreshAllRef = useRef(refreshAll);
  useEffect(() => {
    refreshAllRef.current = refreshAll;
  }, [refreshAll]);

  const uploadFiles = useCallback(
    async (files: File[], preErrors: string[] = []) => {
      const dest = cwd;
      const failures = [...preErrors];
      const valid: File[] = [];
      for (const f of files) {
        // Reject oversize files client-side (same code the backend returns) before spending a request.
        if (f.size > MAX_UPLOAD_BYTES) {
          failures.push(t('apps.fileBrowser.uploadError', { name: f.name, message: t('apps.fileBrowser.errors.too_large') }));
        } else {
          valid.push(f);
        }
      }
      if (!dest || valid.length === 0) {
        setError(failures.length ? failures.join(' · ') : null);
        return;
      }
      setError(null);
      const total = valid.length;
      let done = 0;
      setUploadProgress({ done, total });
      const queue = [...valid];
      const uploadOne = async (file: File) => {
        try {
          await uploadFile(dest, file);
          refreshAllRef.current();
        } catch (e) {
          if (e instanceof FilesApiError && e.code === 'exists') {
            // Name clash: ask via the product ConfirmDialog and retry with overwrite on yes.
            if (await confirmReplace(file.name)) {
              try {
                await uploadFile(dest, file, { overwrite: true });
                refreshAllRef.current();
              } catch (e2) {
                failures.push(t('apps.fileBrowser.uploadError', { name: file.name, message: fileBrowserErrorMessage(e2, t, t('apps.fileBrowser.errors.uploadFailed')) }));
              }
            }
            // Declined → skip this file silently.
          } else {
            failures.push(t('apps.fileBrowser.uploadError', { name: file.name, message: fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.uploadFailed')) }));
          }
        } finally {
          done += 1;
          setUploadProgress({ done, total });
        }
      };
      const worker = async () => {
        let file = queue.shift();
        while (file) {
          await uploadOne(file);
          file = queue.shift();
        }
      };
      await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, queue.length) }, worker));
      setUploadProgress(null);
      setError(failures.length ? failures.join(' · ') : null);
    },
    [cwd, t, confirmReplace],
  );

  const onUploadPick = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (e.target.files?.length) void uploadFiles(Array.from(e.target.files));
      e.target.value = ''; // reset so re-picking the same file fires onChange again
    },
    [uploadFiles],
  );

  // Distinguish an OS file drag (exposes a 'Files' type) from the internal row-move drag (which sets
  // dragRef and carries 'text/plain') so external upload and internal move never trigger each other.
  const isExternalFileDrag = useCallback((e: React.DragEvent) => !dragRef.current && e.dataTransfer.types.includes('Files'), []);
  const onListingDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!isExternalFileDrag(e)) return;
      // Always suppress the browser's default "open the dropped file" navigation over the listing.
      e.preventDefault();
      // While an upload is in flight (or before a folder loads) the drop zone is inert, mirroring the
      // disabled Upload button, so a second batch can't clobber the first's progress/error state.
      const busy = !cwd || uploadProgress !== null;
      e.dataTransfer.dropEffect = busy ? 'none' : 'copy';
      setFileDragOver(!busy);
    },
    [cwd, uploadProgress, isExternalFileDrag],
  );
  const onListingDragLeave = useCallback((e: React.DragEvent) => {
    // Ignore leaves into descendants (row → row); only clear when the pointer leaves the listing.
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    setFileDragOver(false);
  }, []);
  const onListingDrop = useCallback(
    (e: React.DragEvent) => {
      if (!isExternalFileDrag(e)) return;
      e.preventDefault();
      setFileDragOver(false);
      if (!cwd || uploadProgress !== null) return; // inert while busy (see onListingDragOver)
      // Read files + detect folders synchronously — the DataTransfer/entry APIs are only valid during
      // the drop event, not in the async upload that follows.
      const items = Array.from(e.dataTransfer.items ?? []);
      const files: File[] = [];
      let hasDir = false;
      if (items.length > 0 && items.some((it) => typeof it.webkitGetAsEntry === 'function')) {
        for (const it of items) {
          if (it.kind !== 'file') continue;
          const entry = it.webkitGetAsEntry?.();
          if (entry?.isDirectory) {
            hasDir = true; // no recursive folder upload in v1
            continue;
          }
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      } else {
        for (const f of Array.from(e.dataTransfer.files ?? [])) files.push(f);
      }
      void uploadFiles(files, hasDir ? [t('apps.fileBrowser.uploadNoFolders')] : []);
    },
    [cwd, uploadProgress, isExternalFileDrag, uploadFiles, t],
  );

  const projectFavs = useMemo(
    () => (projects || []).filter((p) => !!p.folder_path).map((p) => ({ label: p.display_name, path: p.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];

  // Rows come from the recursive search when a query is active (backend walk order: shallow first),
  // otherwise from the current-folder listing (dirs-first, then the active column sort).
  const rows = useMemo<RowItem[]>(() => {
    if (query.trim()) return searchRows ?? [];
    const all = [...(listing?.entries ?? [])].sort((a, b) => {
      // Folders always group before files, regardless of column/direction (Finder-like).
      if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
      if (!sort) return a.name.localeCompare(b.name);
      let r = 0;
      if (sort.col === 'size') r = (a.size ?? 0) - (b.size ?? 0);
      else if (sort.col === 'modified') r = (a.mtime ?? 0) - (b.mtime ?? 0);
      else r = a.name.localeCompare(b.name);
      if (r === 0) r = a.name.localeCompare(b.name);
      return sort.dir === 'asc' ? r : -r;
    });
    return all.map((e) => ({ entry: e, full: joinPath(cwd, e.name), dir: cwd }));
  }, [query, searchRows, listing, sort, cwd]);

  const selectedItems = useMemo(() => rows.filter((row) => selected.has(row.full)), [rows, selected]);
  const selectedEntry = selectedItems.length === 1 ? selectedItems[0].entry : null;
  const selectedSize = selectedItems.reduce((total, item) => total + (item.entry.size ?? 0), 0);

  const selectRow = (event: React.MouseEvent, item: RowItem) => {
    const coarse = window.matchMedia('(pointer: coarse)').matches;
    if (coarse) {
      if (suppressTouchClickRef.current === item.full) {
        suppressTouchClickRef.current = null;
        return;
      }
      if (!touchSelectionMode) {
        void openItem(item);
        return;
      }
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(item.full)) next.delete(item.full);
        else next.add(item.full);
        return next;
      });
      selectionAnchorRef.current = item.full;
      return;
    }

    if (event.shiftKey && selectionAnchorRef.current) {
      const anchor = rows.findIndex((row) => row.full === selectionAnchorRef.current);
      const target = rows.findIndex((row) => row.full === item.full);
      if (anchor >= 0 && target >= 0) {
        const [start, end] = anchor < target ? [anchor, target] : [target, anchor];
        setSelected(new Set(rows.slice(start, end + 1).map((row) => row.full)));
        return;
      }
    }
    if (event.metaKey || event.ctrlKey) {
      setSelected((current) => {
        const next = new Set(current);
        if (next.has(item.full)) next.delete(item.full);
        else next.add(item.full);
        return next;
      });
    } else {
      setSelected(new Set([item.full]));
    }
    selectionAnchorRef.current = item.full;
  };

  const startLongPress = (event: React.PointerEvent, item: RowItem) => {
    if (event.pointerType !== 'touch' && !window.matchMedia('(pointer: coarse)').matches) return;
    cancelLongPress();
    const timer = window.setTimeout(() => {
      suppressTouchClickRef.current = item.full;
      setTouchSelectionMode(true);
      setSelected(new Set([item.full]));
      selectionAnchorRef.current = item.full;
      longPressRef.current = null;
    }, LONG_PRESS_MS);
    longPressRef.current = { path: item.full, x: event.clientX, y: event.clientY, timer };
  };

  const moveLongPress = (event: React.PointerEvent) => {
    const press = longPressRef.current;
    if (!press) return;
    if (Math.abs(event.clientX - press.x) > LONG_PRESS_MOVE_PX || Math.abs(event.clientY - press.y) > LONG_PRESS_MOVE_PX) {
      cancelLongPress();
    }
  };

  // The in-app clipboard intentionally never touches the OS clipboard and is not persisted. Paths
  // remain usable while navigating between folders in this Files app instance.
  const [clipboard, setClipboard] = useState<RowItem[]>([]);
  const copyRowsToClipboard = useCallback((items: RowItem[]) => {
    if (items.length > 0) setClipboard(items.map((item) => ({ ...item, entry: { ...item.entry } })));
    setMenu(null);
  }, []);

  const applyCopy = useCallback(
    async (item: RowItem, destDir: string, name: string, overwrite: boolean) => {
      await copyPath(item.full, joinPath(destDir, name), overwrite);
      if (overwrite) {
        // Replace destroys the previous destination and has no lossless undo, matching overwrite
        // move. Clear an older bar rather than implying the replacement itself can be reverted.
        if (undoTimerRef.current != null) window.clearTimeout(undoTimerRef.current);
        setUndoEntry(null);
      }
    },
    [],
  );

  const copyOne = useCallback(
    async (item: RowItem, destDir: string) => {
      // Pasting back into the source parent is inherently a clash. In particular, the backend
      // rejects a directory/symlink `src === dst` before its normal `exists` response, so enter the
      // same Replace / Keep both decision directly instead of surfacing invalid_path/invalid_copy.
      const sameParent = item.dir === destDir;
      if (!sameParent) {
        try {
          await applyCopy(item, destDir, item.entry.name, false);
          return true;
        } catch (error) {
          if (!(error instanceof FilesApiError) || error.code !== 'exists') throw error;
        }
      }
      return requestNameClash(item, destDir, async (action) => {
        if (action === 'replace') {
          // Replacing an entry with itself is already the requested end state. Treat it as a no-op;
          // the backend must not be asked to destroy/rebuild a source through the same path.
          if (sameParent) return;
          await applyCopy(item, destDir, item.entry.name, true);
          return;
        }
        for (let n = 2; n <= MAX_KEEP_BOTH; n++) {
          try {
            await applyCopy(item, destDir, dedupeName(item.entry.name, n), false);
            return;
          } catch (error) {
            if (error instanceof FilesApiError && error.code === 'exists') continue;
            throw error;
          }
        }
        throw new Error(t('apps.fileBrowser.moveClashTooMany'));
      });
    },
    [applyCopy, requestNameClash, t],
  );

  const pasteRows = useCallback(
    async (items: RowItem[], destDir: string) => {
      if (!destDir || items.length === 0) return;
      setMenu(null);
      const failures: string[] = [];
      let copied = 0;
      for (const item of items) {
        try {
          if (await copyOne(item, destDir)) copied += 1;
        } catch (error) {
          failures.push(
            t('apps.fileBrowser.operationError', {
              name: item.entry.name,
              message: fileBrowserErrorMessage(error, t, t('apps.fileBrowser.errors.copyFailed')),
            }),
          );
        }
      }
      if (copied > 0) refreshAll();
      setError(failures.length ? failures.join(' · ') : null);
    },
    [copyOne, refreshAll, t],
  );

  const duplicateItem = useCallback(
    async (item: RowItem) => {
      setMenu(null);
      try {
        for (let n = 2; n <= MAX_KEEP_BOTH; n++) {
          try {
            await applyCopy(item, item.dir, dedupeName(item.entry.name, n), false);
            setError(null);
            refreshAll();
            return;
          } catch (error) {
            if (error instanceof FilesApiError && error.code === 'exists') continue;
            throw error;
          }
        }
        setError(t('apps.fileBrowser.moveClashTooMany'));
      } catch (error) {
        setError(fileBrowserErrorMessage(error, t, t('apps.fileBrowser.errors.copyFailed')));
      }
    },
    [applyCopy, refreshAll, t],
  );

  const downloadItems = useCallback(
    async (items: RowItem[]) => {
      setMenu(null);
      const files = items.filter((item) => item.entry.kind !== 'dir');
      const toDownload = files.slice(0, MAX_BATCH_DOWNLOADS);
      for (const item of toDownload) {
        downloadFile(item.full);
        // Preserve ordering and avoid collapsing the whole batch into one browser event burst.
        await new Promise<void>((resolve) => window.setTimeout(resolve, 80));
      }
      // Advance the capped batch: a second Download action now starts with the next selected files
      // instead of repeating the same first ten. Concurrent selection changes are preserved.
      const downloaded = new Set(toDownload.map((item) => item.full));
      setSelected((current) => new Set([...current].filter((path) => !downloaded.has(path))));
      const hints: string[] = [];
      const skippedDirs = items.length - files.length;
      if (skippedDirs > 0) hints.push(t('apps.fileBrowser.downloadFoldersSkipped', { count: skippedDirs }));
      if (files.length > MAX_BATCH_DOWNLOADS) {
        hints.push(t('apps.fileBrowser.downloadLimit', { count: MAX_BATCH_DOWNLOADS, remaining: files.length - MAX_BATCH_DOWNLOADS }));
      }
      setError(hints.length ? hints.join(' · ') : null);
    },
    [t],
  );

  const showInitialSpinner = inSearch ? searchBusy && searchRows === null : loading && !listing;
  const showEmpty = inSearch ? !searchBusy && (searchRows?.length ?? 0) === 0 : !!listing && rows.length === 0 && newEntry === null;
  // Single row keeps its original four actions plus Copy/Duplicate; a batch has four batch actions.
  // Blank space keeps New File/Folder + Terminal and gains Paste while the in-app clipboard is set.
  const menuItemCount = menu
    ? menu.item
      ? menu.items.length > 1
        ? 4
        : 6
      : (cwd ? 3 : 2) + (clipboard.length > 0 && cwd ? 1 : 0)
    : 0;

  return (
    <div className={windowed ? 'relative flex h-full w-full flex-col bg-surface' : 'relative flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]'}>
      {!windowed && (
        <div>
          <h1 className="text-[18px] font-semibold text-foreground">{t('apps.fileBrowser.label')}</h1>
          <p className="text-[12px] text-muted">{t('apps.fileBrowser.tagline')}</p>
        </div>
      )}

      <div className={clsx('flex min-h-0 flex-1 flex-col overflow-hidden', !windowed && 'rounded-xl border border-border')}>
        {/* Toolbar: breadcrumb (left) + search + New File / New Folder (right) */}
        <div className="flex items-center gap-2 border-b border-border bg-surface-2/60 px-3 py-2">
          <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
            <Button type="button" size="icon" variant="ghost" className="size-7 shrink-0 text-muted" aria-label={t('apps.fileBrowser.refresh')} onClick={() => cwd && refreshAll()}>
              <RefreshCw className={clsx('size-3.5', (loading || searchBusy) && 'animate-spin')} />
            </Button>
            {crumbs.map((c, i) => (
              <span key={c.path} className="flex shrink-0 items-center">
                {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                <button
                  type="button"
                  onClick={() => {
                    setQuery('');
                    navigate(c.path);
                  }}
                  {...dropProps(c.path)}
                  className={clsx(
                    'max-w-[140px] truncate rounded px-1.5 py-0.5 text-[12.5px] text-muted transition hover:bg-foreground/[0.06] hover:text-foreground',
                    dropTarget === c.path && 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan',
                  )}
                >
                  {c.label}
                </button>
              </span>
            ))}
          </div>
          <label className="flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2 py-1">
            {searchBusy ? <Loader2 className="size-3.5 shrink-0 animate-spin text-muted" /> : <Search className="size-3.5 shrink-0 text-muted" />}
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t(searchMode === 'content' ? 'apps.fileBrowser.searchContentPlaceholder' : 'apps.fileBrowser.searchPlaceholder')}
              className="w-28 bg-transparent text-[12px] text-foreground placeholder:text-muted focus:outline-none"
            />
            {/* Mode toggle: file/folder NAME search (default) ⇄ file CONTENT search; pressed = content. */}
            <button
              type="button"
              aria-pressed={searchMode === 'content'}
              aria-label={t('apps.fileBrowser.searchContents')}
              title={t('apps.fileBrowser.searchContents')}
              onClick={() => setSearchMode((m) => (m === 'content' ? 'name' : 'content'))}
              className={clsx(
                'grid size-5 shrink-0 place-items-center rounded transition',
                searchMode === 'content' ? 'bg-cyan-soft text-cyan' : 'text-muted hover:bg-foreground/10 hover:text-foreground',
              )}
            >
              <FileSearch className="size-3.5" />
            </button>
            {query && (
              <button type="button" onClick={() => setQuery('')} className="shrink-0 text-muted transition hover:text-foreground" aria-label={t('common.close')}>
                <X className="size-3" strokeWidth={2.5} />
              </button>
            )}
          </label>
          <input ref={uploadInputRef} type="file" multiple className="hidden" onChange={onUploadPick} />
          {selectedItems.length > 1 || touchSelectionMode ? (
            <div className="flex shrink-0 items-center gap-1 overflow-x-auto">
              <Button type="button" size="sm" variant="ghost" className="h-7 gap-1.5 px-2 text-[12px]" onClick={() => copyRowsToClipboard(selectedItems)} disabled={selectedItems.length === 0}>
                <Copy className="size-3.5" /> {t('apps.fileBrowser.copy')}
              </Button>
              {clipboard.length > 0 && (
                <Button type="button" size="sm" variant="ghost" className="h-7 gap-1.5 px-2 text-[12px]" disabled={!cwd} onClick={() => void pasteRows(clipboard, cwd)}>
                  <ClipboardPaste className="size-3.5" /> {t('apps.fileBrowser.paste')}
                </Button>
              )}
              <Button type="button" size="sm" variant="ghost" className="h-7 gap-1.5 px-2 text-[12px]" onClick={() => setMovePickerItems(selectedItems)} disabled={selectedItems.length === 0}>
                <FolderInput className="size-3.5" /> {t('apps.fileBrowser.move')}
              </Button>
              <Button type="button" size="sm" variant="ghost" className="h-7 gap-1.5 px-2 text-[12px]" onClick={() => void downloadItems(selectedItems)} disabled={selectedItems.length === 0}>
                <Download className="size-3.5" /> {t('apps.fileBrowser.download')}
              </Button>
              <Button type="button" size="sm" variant="ghost" className="h-7 gap-1.5 px-2 text-[12px] text-destructive" onClick={() => removeItems(selectedItems)} disabled={selectedItems.length === 0}>
                <Trash2 className="size-3.5" /> {t('apps.fileBrowser.delete')}
              </Button>
              <Button type="button" size="icon" variant="ghost" className="size-7 text-muted" aria-label={t('apps.fileBrowser.clearSelection')} onClick={clearSelection}>
                <X className="size-4" />
              </Button>
            </div>
          ) : (
            <>
              {clipboard.length > 0 && (
                <Button type="button" size="sm" variant="ghost" className="h-7 shrink-0 gap-1.5 px-2 text-[12px]" disabled={!cwd} onClick={() => void pasteRows(clipboard, cwd)}>
                  <ClipboardPaste className="size-3.5" /> {t('apps.fileBrowser.paste')}
                </Button>
              )}
              <Button type="button" size="sm" variant="brand" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd || newEntry !== null} onClick={onNewFile}>
                <FilePlus className="size-3.5" /> {t('apps.fileBrowser.newFile')}
              </Button>
              <Button type="button" size="sm" variant="outline" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd || newEntry !== null} onClick={() => startNewEntry('folder')}>
                <FolderPlus className="size-3.5" /> {t('apps.fileBrowser.newFolder')}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]"
                disabled={!cwd || uploadProgress !== null}
                onClick={() => uploadInputRef.current?.click()}
              >
                {uploadProgress ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />} {t('apps.fileBrowser.upload')}
              </Button>
            </>
          )}
        </div>

        {/* Mobile favorites/projects strip: the rail (below) is hidden under md, so surface the same
            destinations — system favorites then project folders — as a horizontal chip row pinned
            under the toolbar. Tap navigates; the current folder's chip is highlighted. */}
        {(sysFavs.length > 0 || projectFavs.length > 0) && (
          <div className="flex shrink-0 items-center gap-2 overflow-x-auto border-b border-border bg-surface-2/60 px-3 py-2 md:hidden">
            {sysFavs.map((f) => {
              const Icon = FAV_ICON[f.key] ?? Folder;
              const active = cwd === f.path;
              return (
                <button
                  key={f.path}
                  type="button"
                  aria-current={active ? 'true' : undefined}
                  onClick={() => {
                    setQuery('');
                    navigate(f.path);
                  }}
                  className={clsx(
                    'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition',
                    active ? 'border-cyan/40 bg-cyan-soft text-foreground' : 'border-border-strong text-muted',
                  )}
                >
                  <Icon className="size-3.5 shrink-0" />
                  <span className="max-w-[140px] truncate">{f.path.split('/').filter(Boolean).pop() || f.path}</span>
                </button>
              );
            })}
            {projectFavs.map((f) => {
              const active = cwd === f.path;
              return (
                <button
                  key={f.path}
                  type="button"
                  aria-current={active ? 'true' : undefined}
                  onClick={() => {
                    setQuery('');
                    navigate(f.path);
                  }}
                  className={clsx(
                    'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition',
                    active ? 'border-cyan/40 bg-cyan-soft text-foreground' : 'border-border-strong text-muted',
                  )}
                >
                  <Folder className={clsx('size-3.5 shrink-0', active ? 'text-cyan' : 'text-cyan/70')} />
                  <span className="max-w-[140px] truncate">{f.label}</span>
                </button>
              );
            })}
          </div>
        )}

        {error && <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive">{error}</div>}

        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* Rail: Favorites THEN Projects (design nknn2 order). Folders here are drop targets too. */}
          <aside className="hidden w-[196px] shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-surface-2/40 p-2 md:flex">
            {sysFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.favorites')}</RailTitle>}
            {sysFavs.map((f) => {
              const Icon = FAV_ICON[f.key] ?? Folder;
              return (
                <RailRow
                  key={f.path}
                  icon={<Icon className="size-3.5 text-muted" />}
                  label={f.path.split('/').filter(Boolean).pop() || f.path}
                  active={cwd === f.path}
                  dropActive={dropTarget === f.path}
                  dropProps={dropProps(f.path)}
                  onClick={() => {
                    setQuery('');
                    navigate(f.path);
                  }}
                />
              );
            })}
            {projectFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.projects')}</RailTitle>}
            {projectFavs.map((f) => (
              <RailRow
                key={f.path}
                icon={<Folder className="size-3.5 text-cyan" />}
                label={f.label}
                active={cwd === f.path}
                dropActive={dropTarget === f.path}
                dropProps={dropProps(f.path)}
                onClick={() => {
                  setQuery('');
                  navigate(f.path);
                }}
              />
            ))}
          </aside>

          {/* Listing: Name / Size / Modified. An OS file drag drops here to upload into the current
              folder; the internal row-move drag (dragRef) is filtered out by isExternalFileDrag. */}
          <div
            className="relative flex min-w-0 flex-1 flex-col"
            onDragOver={onListingDragOver}
            onDragLeave={onListingDragLeave}
            onDrop={onListingDrop}
          >
            <div className="flex items-center border-b border-border px-3 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">
              {touchSelectionMode && <span className="mr-2 size-[18px] shrink-0" aria-hidden />}
              <button type="button" onClick={() => cycleSort('name')} className={clsx('flex min-w-0 flex-1 items-center gap-1 text-left transition hover:text-foreground', sort?.col === 'name' && 'text-foreground')}>
                <span className="truncate">{t('apps.fileBrowser.colName')}</span>
                {sort?.col === 'name' && (sort.dir === 'asc' ? <ChevronUp className="size-3 shrink-0" /> : <ChevronDown className="size-3 shrink-0" />)}
              </button>
              <button type="button" onClick={() => cycleSort('size')} className={clsx('flex w-20 shrink-0 items-center justify-end gap-1 transition hover:text-foreground', sort?.col === 'size' && 'text-foreground')}>
                {t('apps.fileBrowser.colSize')}
                {sort?.col === 'size' && (sort.dir === 'asc' ? <ChevronUp className="size-3 shrink-0" /> : <ChevronDown className="size-3 shrink-0" />)}
              </button>
              <button type="button" onClick={() => cycleSort('modified')} className={clsx('flex w-36 shrink-0 items-center gap-1 pl-4 transition hover:text-foreground', sort?.col === 'modified' && 'text-foreground')}>
                {t('apps.fileBrowser.colModified')}
                {sort?.col === 'modified' && (sort.dir === 'asc' ? <ChevronUp className="size-3 shrink-0" /> : <ChevronDown className="size-3 shrink-0" />)}
              </button>
            </div>
            {/* Right-clicking blank space offers New File / New Folder in the current folder. */}
            <div className="min-h-0 flex-1 overflow-y-auto py-1" onContextMenu={(e) => openMenu(e, null)}>
              {showInitialSpinner && (
                <div className="grid place-items-center py-8"><Loader2 className="size-4 animate-spin text-muted" /></div>
              )}
              {!inSearch && newEntry !== null && (
                // Stop the contextmenu here so right-clicking the input shows the browser's native
                // menu (paste) instead of our blank-space New menu from the container below.
                <div className="flex items-center px-3 py-1.5" onContextMenu={(e) => e.stopPropagation()}>
                  <span className="flex min-w-0 flex-1 items-center gap-2">
                    {newEntry.kind === 'folder' ? <Folder className="size-4 shrink-0 text-cyan" /> : <FileIcon className="size-4 shrink-0 text-muted" />}
                    <InlineNameInput
                      initial=""
                      placeholder={t(newEntry.kind === 'folder' ? 'apps.fileBrowser.newFolderPlaceholder' : 'apps.fileBrowser.newFilePrompt')}
                      onCommit={(v) => void commitNewEntry(newEntry.kind, v)}
                      onCancel={() => setNewEntry(null)}
                      className="min-w-0 flex-1 rounded border border-cyan bg-surface px-1.5 py-0.5 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
                    />
                  </span>
                </div>
              )}
              {showEmpty && (
                <div className="px-3 py-8 text-center text-[12px] text-muted">{inSearch ? t('apps.fileBrowser.noMatches') : t('apps.fileBrowser.empty')}</div>
              )}
              {rows.map((item) => {
                const { Icon, color } = entryIcon(item.entry);
                const isDir = item.entry.kind === 'dir';
                const folder = relFolder(item.rel);
                if (rename?.full === item.full) {
                  return (
                    <div key={item.full} className="flex items-center px-3 py-1.5" onContextMenu={(e) => e.stopPropagation()}>
                      <span className="flex min-w-0 flex-1 items-center gap-2">
                        <Icon className="size-4 shrink-0" style={{ color }} />
                        <InlineNameInput
                          initial={item.entry.name}
                          onCommit={(v) => void commitRename(item, v)}
                          onCancel={() => setRename(null)}
                          className="min-w-0 flex-1 rounded border border-cyan bg-surface px-1.5 py-0.5 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
                        />
                      </span>
                    </div>
                  );
                }
                return (
                  <button
                    key={item.full}
                    type="button"
                    draggable={!touchSelectionMode}
                    role={touchSelectionMode ? 'checkbox' : undefined}
                    aria-checked={touchSelectionMode ? selected.has(item.full) : undefined}
                    onDragStart={(e) => {
                      const dragged = selected.has(item.full) && selectedItems.length > 1 ? selectedItems : [item];
                      dragRef.current = dragged;
                      e.dataTransfer.effectAllowed = 'move';
                      try {
                        e.dataTransfer.setData('text/plain', dragged.map((entry) => entry.full).join('\n'));
                      } catch {
                        // Some browsers throw if setData is called outside a real drag; harmless.
                      }
                    }}
                    onDragEnd={() => {
                      dragRef.current = null;
                      setDropTarget(null);
                    }}
                    onDragOver={(e) => {
                      if (!isDir || !dragRef.current || dragRef.current.some((entry) => entry.full === item.full)) return;
                      e.preventDefault();
                      setDropTarget((d) => (d === item.full ? d : item.full));
                    }}
                    onDragLeave={() => {
                      if (isDir) setDropTarget((d) => (d === item.full ? null : d));
                    }}
                    onDrop={(e) => {
                      if (!isDir) return;
                      e.preventDefault();
                      void moveInto(item.full);
                    }}
                    onPointerDown={(event) => startLongPress(event, item)}
                    onPointerMove={moveLongPress}
                    onPointerUp={cancelLongPress}
                    onPointerCancel={cancelLongPress}
                    // Mouse: plain single-click selects, modifiers extend/toggle, double-click opens.
                    // Touch: tap opens until long-press enters checkbox selection mode, then toggles.
                    onClick={(event) => selectRow(event, item)}
                    onDoubleClick={() => void openItem(item)}
                    onKeyDown={(ev) => {
                      if (ev.key === 'Enter' || ev.key === ' ') {
                        ev.preventDefault();
                        if (touchSelectionMode) {
                          setSelected((current) => {
                            const next = new Set(current);
                            if (next.has(item.full)) next.delete(item.full);
                            else next.add(item.full);
                            return next;
                          });
                        } else {
                          void openItem(item);
                        }
                      }
                    }}
                    onContextMenu={(e) => {
                      if (window.matchMedia('(pointer: coarse)').matches) {
                        e.preventDefault();
                        return;
                      }
                      const items = selected.has(item.full) ? selectedItems : [item];
                      if (!selected.has(item.full)) {
                        setSelected(new Set([item.full]));
                        selectionAnchorRef.current = item.full;
                      }
                      openMenu(e, item, items);
                    }}
                    className={clsx(
                      'flex w-full items-center px-3 py-1.5 text-left text-[12.5px] transition',
                      dropTarget === item.full
                        ? 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan'
                        : selected.has(item.full)
                          ? 'bg-cyan-soft text-foreground'
                          : 'text-foreground hover:bg-foreground/[0.04]',
                    )}
                  >
                    {touchSelectionMode && <Checkbox checked={selected.has(item.full)} presentational className="mr-2" />}
                    <span className="flex min-w-0 flex-1 items-center gap-2">
                      <Icon className="size-4 shrink-0" style={{ color }} />
                      <span className="truncate">{item.entry.name}</span>
                      {folder && <span className="min-w-0 shrink truncate text-[11px] text-muted">{folder}</span>}
                      {item.matchCount != null && (
                        <span
                          className="ml-auto shrink-0 rounded-full bg-surface-3 px-1.5 font-mono text-[10px] text-muted"
                          title={t('apps.fileBrowser.matchCount', { count: item.matchCount })}
                        >
                          {item.matchCount}
                        </span>
                      )}
                    </span>
                    <span className="w-20 shrink-0 text-right font-mono text-[11px] text-muted">{isDir ? '—' : formatSize(item.entry.size)}</span>
                    <span className="w-36 shrink-0 pl-4 font-mono text-[11px] text-muted">{formatMtime(item.entry.mtime)}</span>
                  </button>
                );
              })}
            </div>
            {fileDragOver && (
              <div className="pointer-events-none absolute inset-1.5 z-20 flex items-center justify-center rounded-lg border-2 border-dashed border-cyan bg-cyan-soft/70">
                <span className="rounded-md bg-surface px-3 py-1.5 text-[12.5px] font-medium text-foreground shadow-sm">
                  {t('apps.fileBrowser.dropHint')}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Undo bar: one recent revertible action (delete via backend token / move via reverse
            move). Auto-dismisses; doesn't steal focus from the listing. */}
        {undoEntry && (
          <div className="flex items-center gap-2 border-t border-border bg-surface-2/80 px-3 py-1.5 text-[12px]">
            <span className="min-w-0 flex-1 truncate text-muted">
              {undoEntry.count > 1
                ? t(undoEntry.kind === 'delete' ? 'apps.fileBrowser.deletedBatchNotice' : 'apps.fileBrowser.movedBatchNotice', { count: undoEntry.count })
                : t(undoEntry.kind === 'delete' ? 'apps.fileBrowser.deletedNotice' : 'apps.fileBrowser.movedNotice', { name: undoEntry.label })}
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 shrink-0 gap-1 px-2 text-[12px] text-cyan"
              disabled={undoBusy}
              onClick={() => void performUndo()}
            >
              {undoBusy ? <Loader2 className="size-3 animate-spin" /> : <Undo2 className="size-3" />}
              {t('apps.fileBrowser.undo')}
            </Button>
            <button
              type="button"
              aria-label={t('common.close')}
              onClick={() => setUndoEntry(null)}
              className="shrink-0 text-muted transition hover:text-foreground"
            >
              <X className="size-3" strokeWidth={2.5} />
            </button>
          </div>
        )}

        {/* Status bar: item count + selection, with the hidden-files toggle. */}
        <div className="flex items-center gap-3 border-t border-border bg-surface-2/60 px-3 py-1.5 text-[11px] text-muted">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} className="size-3" />
            {t('apps.fileBrowser.showHidden')}
          </label>
          {uploadProgress && (
            <span className="flex items-center gap-1.5 text-cyan">
              <Loader2 className="size-3 animate-spin" />
              {t('apps.fileBrowser.uploading', { done: uploadProgress.done, total: uploadProgress.total })}
            </span>
          )}
          <span className="ml-auto flex min-w-0 items-center gap-2 font-mono">
            {selectedItems.length > 1 && (
              <span className="truncate text-foreground/80">
                {t('apps.fileBrowser.selectedStatus', { count: selectedItems.length, size: formatSize(selectedSize) })}
              </span>
            )}
            {selectedEntry && (
              <span className="truncate text-foreground/80">
                {selectedEntry.name}
                {selectedEntry.kind !== 'dir' && selectedEntry.size != null ? ` · ${formatSize(selectedEntry.size)}` : ''}
              </span>
            )}
            <span className="shrink-0">
              {inSearch ? t('apps.fileBrowser.searchCount', { count: rows.length }) : t('apps.fileBrowser.itemCount', { count: rows.length })}
            </span>
            {!inSearch && listing?.truncated && <span className="shrink-0">· {t('apps.fileBrowser.listTruncated', { count: listing.limit ?? rows.length })}</span>}
            {inSearch && searchTruncated && <span className="shrink-0">· {t('apps.fileBrowser.searchTruncated')}</span>}
          </span>
        </div>
      </div>

      {/* MOBILE-ONLY quick-look overlay: on mobile there's no window layer, so a previewable file
          opens here in-page instead of the standalone Preview window (desktop uses the window). */}
      {preview && (
        <div className="absolute inset-0 z-20 flex flex-col bg-surface">
          <div className="flex items-center gap-2 border-b border-border bg-surface-2/60 px-3 py-2">
            <FileText className="size-4 shrink-0 text-muted" />
            <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-foreground">{preview.name}</span>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0 text-mint"
              aria-label={t('apps.fileBrowser.download')}
              onClick={() => downloadFile(preview.path)}
            >
              <Download className="size-3.5" />
            </Button>
            <Button type="button" size="icon" variant="ghost" className="size-7 shrink-0 text-muted" aria-label={t('common.close')} onClick={() => setPreview(null)}>
              <X className="size-4" strokeWidth={2.5} />
            </Button>
          </div>
          <div className="min-h-0 flex-1">
            <FilePreview source={{ url: contentUrl(preview.path), name: preview.name }} />
          </div>
        </div>
      )}

      {movePickerItems && (
        <FilePicker
          mode="open-directory"
          initialPath={cwd}
          onCancel={() => setMovePickerItems(null)}
          onConfirm={async ({ path }) => {
            await moveItemsInto(movePickerItems, path);
            setMovePickerItems(null);
          }}
        />
      )}

      {/* Destructive delete confirmation (product dialog; replaces window.confirm). The wording
          stays neutral about undoability — whether the backend could stage the entry is only known
          after the call, and the undo bar communicates it. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={
          pendingDelete && pendingDelete.length > 1
            ? t('apps.fileBrowser.deleteBatchTitle', { count: pendingDelete.length })
            : t('apps.fileBrowser.deleteTitle', { name: pendingDelete?.[0]?.entry.name ?? '' })
        }
        description={
          pendingDelete && pendingDelete.length > 1
            ? t('apps.fileBrowser.deleteBatchHint', { count: pendingDelete.length })
            : pendingDelete?.[0]?.entry.kind === 'dir'
              ? t('apps.fileBrowser.deleteDirHint')
              : undefined
        }
        confirmLabel={t('apps.fileBrowser.delete')}
        destructive
        onConfirm={performDelete}
      />

      {/* Upload name-clash replace prompt (promise-bridged from the async upload workers). */}
      <ConfirmDialog
        open={replaceAsk !== null}
        onOpenChange={(open) => {
          if (!open) answerReplace(false);
        }}
        title={t('apps.fileBrowser.uploadReplace', { name: replaceAsk?.name ?? '' })}
        confirmLabel={t('apps.fileBrowser.replace')}
        onConfirm={() => answerReplace(true)}
      />

      {/* Move/copy name-clash: Replace / Keep both / Cancel. Purpose-built on the Dialog primitive
          because ConfirmDialog's footer is fixed to two actions — but it reuses the same primitives,
          so the look matches. Dismissal is blocked while a retry is in flight. */}
      <Dialog
        open={nameClash !== null}
        onOpenChange={(open) => {
          if (!open && nameClashBusy === null) answerNameClash(false);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('apps.fileBrowser.moveClashTitle', { name: nameClash?.item.entry.name ?? '' })}</DialogTitle>
            <DialogDescription>{t('apps.fileBrowser.moveClashDesc')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => answerNameClash(false)} disabled={nameClashBusy !== null}>
              {t('common.cancel')}
            </Button>
            <Button variant="outline" onClick={() => void runNameClash('keep')} disabled={nameClashBusy !== null}>
              {nameClashBusy === 'keep' ? <Loader2 className="size-4 animate-spin" /> : null}
              {t('apps.fileBrowser.keepBoth')}
            </Button>
            <Button variant="default" onClick={() => void runNameClash('replace')} disabled={nameClashBusy !== null}>
              {nameClashBusy === 'replace' ? <Loader2 className="size-4 animate-spin" /> : null}
              {t('apps.fileBrowser.replace')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {menu && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu} itemCount={menuItemCount}>
          {menu.item ? (
            menu.items.length > 1 ? (
              <>
                <ContextMenuItem
                  icon={<Copy className="size-3.5" />}
                  label={t('apps.fileBrowser.copy')}
                  onClick={() => copyRowsToClipboard(menu.items)}
                />
                <ContextMenuItem
                  icon={<FolderInput className="size-3.5 text-cyan" />}
                  label={t('apps.fileBrowser.move')}
                  onClick={() => {
                    const items = menu.items;
                    closeMenu();
                    setMovePickerItems(items);
                  }}
                />
                <ContextMenuItem
                  icon={<Download className="size-3.5 text-mint" />}
                  label={t('apps.fileBrowser.download')}
                  onClick={() => void downloadItems(menu.items)}
                />
                <ContextMenuItem icon={<Trash2 className="size-3.5" />} label={t('apps.fileBrowser.delete')} danger onClick={() => removeItems(menu.items)} />
              </>
            ) : (
              <>
                <ContextMenuItem
                  icon={menu.item.entry.kind === 'dir' ? <Folder className="size-3.5 text-cyan" /> : <FileText className="size-3.5 text-cyan" />}
                  label={t('apps.fileBrowser.open')}
                  onClick={() => {
                    const it = menu.item as RowItem;
                    closeMenu();
                    void openItem(it);
                  }}
                />
                {menu.item.entry.kind === 'dir' && (
                  <ContextMenuItem
                    icon={<SquareTerminal className="size-3.5 text-mint" />}
                    label={t('apps.fileBrowser.openTerminalHere')}
                    onClick={() => {
                      const it = menu.item as RowItem;
                      closeMenu();
                      openTerminalHere(it.full);
                    }}
                  />
                )}
                {menu.item.entry.kind !== 'dir' && (
                  <ContextMenuItem
                    icon={<Download className="size-3.5 text-mint" />}
                    label={t('apps.fileBrowser.download')}
                    onClick={() => {
                      const it = menu.item as RowItem;
                      closeMenu();
                      downloadFile(it.full);
                    }}
                  />
                )}
                <ContextMenuItem icon={<Copy className="size-3.5" />} label={t('apps.fileBrowser.copy')} onClick={() => copyRowsToClipboard(menu.items)} />
                <ContextMenuItem icon={<Copy className="size-3.5 text-gold" />} label={t('apps.fileBrowser.duplicate')} onClick={() => void duplicateItem(menu.item as RowItem)} />
                <ContextMenuItem
                  icon={<Pencil className="size-3.5" />}
                  label={t('apps.fileBrowser.rename')}
                  onClick={() => {
                    const it = menu.item as RowItem;
                    closeMenu();
                    startRename(it);
                  }}
                />
                <ContextMenuItem icon={<Trash2 className="size-3.5" />} label={t('apps.fileBrowser.delete')} danger onClick={() => removeItems(menu.items)} />
              </>
            )
          ) : (
            <>
              {clipboard.length > 0 && cwd && (
                <ContextMenuItem icon={<ClipboardPaste className="size-3.5 text-cyan" />} label={t('apps.fileBrowser.paste')} onClick={() => void pasteRows(clipboard, cwd)} />
              )}
              <ContextMenuItem
                icon={<FilePlus className="size-3.5 text-mint" />}
                label={t('apps.fileBrowser.newFile')}
                onClick={() => {
                  closeMenu();
                  startNewEntry('file');
                }}
              />
              <ContextMenuItem
                icon={<FolderPlus className="size-3.5 text-gold" />}
                label={t('apps.fileBrowser.newFolder')}
                onClick={() => {
                  closeMenu();
                  startNewEntry('folder');
                }}
              />
              {cwd && (
                <ContextMenuItem
                  icon={<SquareTerminal className="size-3.5 text-mint" />}
                  label={t('apps.fileBrowser.openTerminalHere')}
                  onClick={() => {
                    closeMenu();
                    openTerminalHere(cwd);
                  }}
                />
              )}
            </>
          )}
        </ContextMenu>
      )}
    </div>
  );
};

const RailTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="px-1 pb-0.5 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">{children}</div>
);

const RailRow: React.FC<{
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
  dropActive?: boolean;
  dropProps?: {
    onDragOver: (e: React.DragEvent) => void;
    onDragLeave: () => void;
    onDrop: (e: React.DragEvent) => void;
  };
}> = ({ icon, label, active, onClick, dropActive, dropProps }) => (
  <button
    type="button"
    onClick={onClick}
    {...dropProps}
    className={clsx(
      'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
      dropActive
        ? 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan'
        : active
          ? 'bg-cyan-soft text-foreground'
          : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
    )}
  >
    <span className="shrink-0">{icon}</span>
    <span className="truncate">{label}</span>
  </button>
);
