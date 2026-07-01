import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Braces,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Download,
  FileCode2,
  FileText,
  File as FileIcon,
  Folder,
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
  Trash2,
  X,
  type LucideIcon,
} from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { isEditableFile, isEditableMeta, previewOverlayKind, type PreviewOverlayKind } from '../../lib/filePreview';
import {
  contentUrl,
  deletePath,
  downloadFile,
  fileBrowserErrorMessage,
  fileMeta,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  movePath,
  parentDir,
  pathCrumbs,
  renamePath,
  searchNames,
  systemFavorites,
  writeFile,
  type Favorite,
  type FsEntry,
  type FsListing,
  type NameHit,
} from '../../lib/filesApi';
import { Button } from '../ui/button';
import { ContextMenu, ContextMenuItem } from '../ui/context-menu';
import { FilePreview } from '../ui/file-preview';
import { InlineNameInput } from '../ui/inline-name-input';

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
// search root, so a nested hit can show the folder it lives in.
type RowItem = { entry: FsEntry; full: string; dir: string; rel?: string };

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

// Whole-machine Finder: favorites/projects rail + a Name/Size/Modified list + a toolbar (breadcrumb,
// search, New File/Folder) + a status bar. Right-click a row for Open/Download/Rename/Delete, or
// blank space for New File/Folder; drag a row onto a folder (row, rail, or breadcrumb) to move it;
// the search box does a recursive file/folder NAME search under the current folder. Double-clicking a
// text/code file opens it in the Editor window. Backend contract: ui/src/lib/filesApi.ts →
// /api/files/*. Design: design.pen `nknn2`.
export const AppsFileBrowserPage: React.FC<{ windowed?: boolean; windowId?: string }> = ({ windowed = false }) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const { projects } = useWorkbenchProjectsTree();
  const [cwd, setCwd] = useState('');
  const [listing, setListing] = useState<FsListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [sysFavs, setSysFavs] = useState<Favorite[]>([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
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
  const [preview, setPreview] = useState<{ path: string; name: string; kind: PreviewOverlayKind } | null>(null);
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
      setSelected(null);
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
    [showHidden, t],
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

  // ---- Recursive name search (backend: /api/files/search_names) --------------------------------
  // A non-empty query switches the listing to recursive file/folder NAME results under `cwd`. The
  // search is debounced and abortable so fast typing doesn't pile up stale requests.
  const [searchResults, setSearchResults] = useState<NameHit[] | null>(null);
  const [searchTruncated, setSearchTruncated] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const searchSeq = useRef(0);
  const searchAbort = useRef<AbortController | null>(null);
  const inSearch = query.trim().length > 0;

  const runSearch = useCallback(
    (raw: string) => {
      const q = raw.trim();
      searchAbort.current?.abort();
      if (!q || !cwd) {
        setSearchResults(null);
        setSearchBusy(false);
        return;
      }
      const ac = new AbortController();
      searchAbort.current = ac;
      const seq = ++searchSeq.current;
      setSearchBusy(true);
      searchNames(cwd, q, showHidden, ac.signal)
        .then((r) => {
          if (seq !== searchSeq.current) return;
          setSearchResults(r.results);
          setSearchTruncated(r.truncated);
        })
        .catch((e: unknown) => {
          if (seq !== searchSeq.current || (e as { name?: string })?.name === 'AbortError') return;
          setSearchResults([]);
          setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.searchFailed')));
        })
        .finally(() => {
          if (seq === searchSeq.current) setSearchBusy(false);
        });
    },
    [cwd, showHidden, t],
  );

  // Debounce the search as the user types; also re-fires when cwd/showHidden change (runSearch
  // identity changes), so switching folders re-scopes an active search.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      searchAbort.current?.abort();
      setSearchResults(null);
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

  // Open: dir → navigate (and leave search); raster image / PDF / Office doc → in-window preview
  // overlay; editable text/code file (within the size cap) → Editor window; anything else → download.
  const openItem = async (item: RowItem) => {
    if (item.entry.kind === 'dir') {
      setQuery('');
      navigate(item.full);
      return;
    }
    const overlay = previewOverlayKind(item.entry);
    if (overlay) {
      setPreview({ path: item.full, name: item.entry.name, kind: overlay });
      return;
    }
    // Mobile has no editor window layer (windows are md+), so a non-previewable file just downloads.
    const desktop = window.matchMedia('(min-width: 768px)').matches;
    if (!desktop) {
      downloadFile(item.full);
      return;
    }
    // Desktop: fetch CURRENT metadata (content-sniffs `text`) and decide by CONTENT, not just the
    // extension — so an extensionless TEXT file opens in the editor while a binary file downloads.
    try {
      const m = await fileMeta(item.full);
      if (isEditableMeta(m)) {
        wm.openApp('editor', { title: item.entry.name, params: { path: item.full, filename: item.entry.name, mtime: m.mtime } });
      } else {
        downloadFile(item.full);
      }
    } catch {
      if (isEditableFile(item.entry)) {
        wm.openApp('editor', { title: item.entry.name, params: { path: item.full, filename: item.entry.name, mtime: item.entry.mtime } });
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

  const removeItem = useCallback(
    async (item: RowItem) => {
      setMenu(null);
      if (!window.confirm(t('apps.fileBrowser.confirmDelete', { name: item.entry.name }))) return;
      try {
        await deletePath(item.full, item.entry.kind === 'dir');
        setError(null);
        setSelected((s) => (s === item.full ? null : s));
        refreshAll();
      } catch (e: unknown) {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.deleteFailed')));
      }
    },
    [t, refreshAll],
  );

  // ---- Context menu ----------------------------------------------------------------------------
  // `item` is null for blank space (offers New File/Folder only).
  const [menu, setMenu] = useState<{ x: number; y: number; item: RowItem | null } | null>(null);
  const openMenu = useCallback((ev: React.MouseEvent, item: RowItem | null) => {
    ev.preventDefault();
    ev.stopPropagation();
    setMenu({ x: ev.clientX, y: ev.clientY, item });
  }, []);
  const closeMenu = useCallback(() => setMenu(null), []);

  // ---- Drag-and-drop move ----------------------------------------------------------------------
  // The dragged row is held in a ref (no re-render mid-drag); `dropTarget` (a folder path) drives the
  // hover highlight. Folders — rows, rail favorites/projects, and breadcrumbs — are drop targets.
  const dragRef = useRef<RowItem | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const moveInto = useCallback(
    async (destDir: string) => {
      const item = dragRef.current;
      dragRef.current = null;
      setDropTarget(null);
      // No-op if it's already in that folder or dropped onto itself; the backend also refuses moving
      // a folder into its own subtree, which surfaces as an error below.
      if (!item || item.dir === destDir || item.full === destDir) return;
      try {
        await movePath(item.full, joinPath(destDir, item.entry.name));
        setError(null);
        refreshAll();
      } catch (e: unknown) {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.moveFailed')));
      }
    },
    [t, refreshAll],
  );
  // Drop-target props for any folder (row / rail / breadcrumb). Only active while a drag is in flight
  // and never onto the dragged item itself.
  const dropProps = (destDir: string) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragRef.current || dragRef.current.full === destDir) return;
      e.preventDefault();
      setDropTarget((d) => (d === destDir ? d : destDir));
    },
    onDragLeave: () => setDropTarget((d) => (d === destDir ? null : d)),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      void moveInto(destDir);
    },
  });

  const projectFavs = useMemo(
    () => (projects || []).filter((p) => !!p.folder_path).map((p) => ({ label: p.display_name, path: p.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];

  // Rows come from the recursive search when a query is active (backend walk order: shallow first),
  // otherwise from the current-folder listing (dirs-first, then the active column sort).
  const rows = useMemo<RowItem[]>(() => {
    if (query.trim()) {
      return (searchResults ?? []).map((h) => ({ entry: h as FsEntry, full: h.path, dir: parentDir(h.path), rel: h.rel }));
    }
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
  }, [query, searchResults, listing, sort, cwd]);

  const selectedEntry = useMemo(() => (selected ? rows.find((r) => r.full === selected)?.entry ?? null : null), [rows, selected]);

  const showInitialSpinner = inSearch ? searchBusy && searchResults === null : loading && !listing;
  const showEmpty = inSearch ? !searchBusy && (searchResults?.length ?? 0) === 0 : !!listing && rows.length === 0 && newEntry === null;
  const menuItemCount = menu ? (menu.item ? (menu.item.entry.kind === 'dir' ? 3 : 4) : 2) : 0;

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
              placeholder={t('apps.fileBrowser.searchPlaceholder')}
              className="w-28 bg-transparent text-[12px] text-foreground placeholder:text-muted focus:outline-none"
            />
            {query && (
              <button type="button" onClick={() => setQuery('')} className="shrink-0 text-muted transition hover:text-foreground" aria-label={t('common.close')}>
                <X className="size-3" strokeWidth={2.5} />
              </button>
            )}
          </label>
          <Button type="button" size="sm" variant="brand" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd || newEntry !== null} onClick={onNewFile}>
            <FilePlus className="size-3.5" /> {t('apps.fileBrowser.newFile')}
          </Button>
          <Button type="button" size="sm" variant="outline" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd || newEntry !== null} onClick={() => startNewEntry('folder')}>
            <FolderPlus className="size-3.5" /> {t('apps.fileBrowser.newFolder')}
          </Button>
        </div>

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

          {/* Listing: Name / Size / Modified */}
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex items-center border-b border-border px-3 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">
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
                    draggable
                    onDragStart={(e) => {
                      dragRef.current = item;
                      e.dataTransfer.effectAllowed = 'move';
                      try {
                        e.dataTransfer.setData('text/plain', item.full);
                      } catch {
                        // Some browsers throw if setData is called outside a real drag; harmless.
                      }
                    }}
                    onDragEnd={() => {
                      dragRef.current = null;
                      setDropTarget(null);
                    }}
                    onDragOver={(e) => {
                      if (!isDir || !dragRef.current || dragRef.current.full === item.full) return;
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
                    // Mouse: single-click selects, double-click opens. Touch (coarse pointer) has no
                    // double-click, so a single tap opens. Keyboard: Enter/Space opens/navigates.
                    onClick={() => {
                      if (window.matchMedia('(pointer: coarse)').matches) void openItem(item);
                      else setSelected(item.full);
                    }}
                    onDoubleClick={() => void openItem(item)}
                    onKeyDown={(ev) => {
                      if (ev.key === 'Enter' || ev.key === ' ') {
                        ev.preventDefault();
                        void openItem(item);
                      }
                    }}
                    onContextMenu={(e) => openMenu(e, item)}
                    className={clsx(
                      'flex w-full items-center px-3 py-1.5 text-left text-[12.5px] transition',
                      dropTarget === item.full
                        ? 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan'
                        : selected === item.full
                          ? 'bg-cyan-soft text-foreground'
                          : 'text-foreground hover:bg-foreground/[0.04]',
                    )}
                  >
                    <span className="flex min-w-0 flex-1 items-center gap-2">
                      <Icon className="size-4 shrink-0" style={{ color }} />
                      <span className="truncate">{item.entry.name}</span>
                      {folder && <span className="min-w-0 shrink truncate text-[11px] text-muted">{folder}</span>}
                    </span>
                    <span className="w-20 shrink-0 text-right font-mono text-[11px] text-muted">{isDir ? '—' : formatSize(item.entry.size)}</span>
                    <span className="w-36 shrink-0 pl-4 font-mono text-[11px] text-muted">{formatMtime(item.entry.mtime)}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Status bar: item count + selection, with the hidden-files toggle. */}
        <div className="flex items-center gap-3 border-t border-border bg-surface-2/60 px-3 py-1.5 text-[11px] text-muted">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} className="size-3" />
            {t('apps.fileBrowser.showHidden')}
          </label>
          <span className="ml-auto flex min-w-0 items-center gap-2 font-mono">
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

      {/* In-window image preview overlay (Finder-style quick look). In-window, not a portaled
          Dialog, so it stays inside the window's dark data-theme scope and bounds. */}
      {preview && (
        <div className="absolute inset-0 z-20 flex flex-col bg-surface">
          <div className="flex items-center gap-2 border-b border-border bg-surface-2/60 px-3 py-2">
            {preview.kind === 'image' ? <ImageIcon className="size-4 shrink-0 text-muted" /> : <FileText className="size-4 shrink-0 text-muted" />}
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

      {menu && (
        <ContextMenu x={menu.x} y={menu.y} onClose={closeMenu} itemCount={menuItemCount}>
          {menu.item ? (
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
              <ContextMenuItem
                icon={<Pencil className="size-3.5" />}
                label={t('apps.fileBrowser.rename')}
                onClick={() => {
                  const it = menu.item as RowItem;
                  closeMenu();
                  startRename(it);
                }}
              />
              <ContextMenuItem icon={<Trash2 className="size-3.5" />} label={t('apps.fileBrowser.delete')} danger onClick={() => void removeItem(menu.item as RowItem)} />
            </>
          ) : (
            <>
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
