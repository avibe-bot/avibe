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
  RefreshCw,
  Search,
  X,
  type LucideIcon,
} from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { isEditableFile, isEditableMeta, isRenderOnlyImage } from '../../lib/filePreview';
import {
  contentUrl,
  downloadFile,
  fileBrowserErrorMessage,
  fileMeta,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  pathCrumbs,
  systemFavorites,
  writeFile,
  type Favorite,
  type FsEntry,
  type FsListing,
} from '../../lib/filesApi';
import { Button } from '../ui/button';
import { FilePreview } from './FilePreview';

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

// Whole-machine Finder: favorites/projects rail + a Name/Size/Modified list + a toolbar
// (breadcrumb, search, New File/Folder) + a status bar. A pure directory browser — it does
// not edit files; double-clicking a text/code file opens it in the Editor window. Backend
// contract: ui/src/lib/filesApi.ts → /api/files/*. Design: design.pen `nknn2`.
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

  // Open: dir → navigate; raster image → in-window preview overlay; editable text/code file (within
  // the size cap) → Editor window; anything else → raw download.
  const open = async (e: FsEntry) => {
    const full = joinPath(cwd, e.name);
    if (e.kind === 'dir') {
      navigate(full);
      return;
    }
    // A raster image has no editable text form (it would otherwise just download) — preview it in an
    // overlay instead. Works on mobile too (the overlay is in-window, unlike the desktop-only editor).
    if (isRenderOnlyImage(e)) {
      setPreview({ path: full, name: e.name });
      return;
    }
    // Mobile has no editor window layer (windows are md+), so a non-image file just downloads.
    const desktop = window.matchMedia('(min-width: 768px)').matches;
    if (!desktop) {
      downloadFile(full);
      return;
    }
    // Desktop: fetch CURRENT metadata (which content-sniffs `text`) and decide by CONTENT, not just
    // the extension — so an extensionless / unknown-type TEXT file (LICENSE, README, a `notes` file)
    // opens in the editor instead of downloading, while a symlink / oversized / binary file
    // downloads. Awaiting is safe: opening an internal window needs no user activation, and the
    // anchor download (the binary branch) isn't a popup, so it survives the await.
    try {
      const m = await fileMeta(full);
      if (isEditableMeta(m)) {
        wm.openApp('editor', { title: e.name, params: { path: full, filename: e.name, mtime: m.mtime } });
      } else {
        downloadFile(full);
      }
    } catch {
      // Metadata fetch failed — fall back to the name-only guess so a known text type still opens.
      if (isEditableFile(e)) {
        wm.openApp('editor', { title: e.name, params: { path: full, filename: e.name, mtime: e.mtime } });
      } else {
        downloadFile(full);
      }
    }
  };

  // New File / New Folder: an inline editable row in the listing (mirrors FilePicker), not a
  // window.prompt. null = not creating. Enter creates, Esc/blur cancels.
  const [newEntry, setNewEntry] = useState<{ kind: 'file' | 'folder'; value: string } | null>(null);
  const startNewEntry = useCallback((kind: 'file' | 'folder') => {
    setError(null);
    setNewEntry({ kind, value: '' });
  }, []);
  const newEntryRef = useRef<{ kind: 'file' | 'folder'; value: string } | null>(null);
  newEntryRef.current = newEntry;
  const commitNewEntry = useCallback(async () => {
    const session = newEntryRef.current;
    if (!session) return;
    const name = session.value.trim();
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
      if (session.kind === 'folder') await makeDir(joinPath(cwd, name));
      else await writeFile(joinPath(cwd, name), '', undefined, true);
      setNewEntry(null);
      navigate(cwd);
    } catch (e: unknown) {
      setError(
        fileBrowserErrorMessage(e, t, t(session.kind === 'folder' ? 'apps.fileBrowser.errors.createFolderFailed' : 'apps.fileBrowser.errors.saveFailed')),
      );
    }
  }, [cwd, navigate, t]);

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

  const projectFavs = useMemo(
    () => (projects || []).filter((p) => !!p.folder_path).map((p) => ({ label: p.display_name, path: p.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];
  const entries = useMemo(() => {
    const all = listing?.entries ?? [];
    const q = query.trim().toLowerCase();
    const filtered = q ? all.filter((e) => e.name.toLowerCase().includes(q)) : all;
    return [...filtered].sort((a, b) => {
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
  }, [listing, query, sort]);
  const selectedEntry = useMemo(
    () => (selected ? entries.find((e) => joinPath(cwd, e.name) === selected) ?? null : null),
    [entries, cwd, selected],
  );

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
            <Button type="button" size="icon" variant="ghost" className="size-7 shrink-0 text-muted" aria-label={t('apps.fileBrowser.refresh')} onClick={() => cwd && navigate(cwd)}>
              <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            </Button>
            {crumbs.map((c, i) => (
              <span key={c.path} className="flex shrink-0 items-center">
                {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                <button
                  type="button"
                  onClick={() => navigate(c.path)}
                  className="max-w-[140px] truncate rounded px-1.5 py-0.5 text-[12.5px] text-muted transition hover:bg-foreground/[0.06] hover:text-foreground"
                >
                  {c.label}
                </button>
              </span>
            ))}
          </div>
          <label className="flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2 py-1">
            <Search className="size-3.5 shrink-0 text-muted" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('apps.fileBrowser.searchPlaceholder')}
              className="w-28 bg-transparent text-[12px] text-foreground placeholder:text-muted focus:outline-none"
            />
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
          {/* Rail: Favorites THEN Projects (design nknn2 order). */}
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
                  onClick={() => navigate(f.path)}
                />
              );
            })}
            {projectFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.projects')}</RailTitle>}
            {projectFavs.map((f) => (
              <RailRow key={f.path} icon={<Folder className="size-3.5 text-cyan" />} label={f.label} active={cwd === f.path} onClick={() => navigate(f.path)} />
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
            <div className="min-h-0 flex-1 overflow-y-auto py-1">
              {loading && !listing && (
                <div className="grid place-items-center py-8"><Loader2 className="size-4 animate-spin text-muted" /></div>
              )}
              {newEntry !== null && (
                <div className="flex items-center px-3 py-1.5">
                  <span className="flex min-w-0 flex-1 items-center gap-2">
                    {newEntry.kind === 'folder' ? <Folder className="size-4 shrink-0 text-cyan" /> : <FileIcon className="size-4 shrink-0 text-muted" />}
                    <input
                      autoFocus
                      value={newEntry.value}
                      onChange={(ev) => setNewEntry((s) => (s ? { ...s, value: ev.target.value } : s))}
                      onKeyDown={(ev) => {
                        if (ev.key === 'Enter') {
                          ev.preventDefault();
                          void commitNewEntry();
                        } else if (ev.key === 'Escape') {
                          ev.preventDefault();
                          setNewEntry(null);
                        }
                      }}
                      onBlur={() => setNewEntry(null)}
                      placeholder={t(newEntry.kind === 'folder' ? 'apps.fileBrowser.newFolderPlaceholder' : 'apps.fileBrowser.newFilePrompt')}
                      className="min-w-0 flex-1 rounded border border-cyan bg-surface px-1.5 py-0.5 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
                    />
                  </span>
                </div>
              )}
              {listing && entries.length === 0 && newEntry === null && (
                <div className="px-3 py-8 text-center text-[12px] text-muted">{query ? t('apps.fileBrowser.noMatches') : t('apps.fileBrowser.empty')}</div>
              )}
              {entries.map((e) => {
                const { Icon, color } = entryIcon(e);
                const full = joinPath(cwd, e.name);
                return (
                  <button
                    key={e.name}
                    type="button"
                    // Mouse: single-click selects, double-click opens. Touch (coarse pointer) has no
                    // double-click, so a single tap opens. Keyboard: Enter/Space opens/navigates.
                    onClick={() => {
                      if (window.matchMedia('(pointer: coarse)').matches) open(e);
                      else setSelected(full);
                    }}
                    onDoubleClick={() => open(e)}
                    onKeyDown={(ev) => {
                      if (ev.key === 'Enter' || ev.key === ' ') {
                        ev.preventDefault();
                        open(e);
                      }
                    }}
                    className={clsx(
                      'flex w-full items-center px-3 py-1.5 text-left text-[12.5px] transition',
                      selected === full ? 'bg-cyan-soft text-foreground' : 'text-foreground hover:bg-foreground/[0.04]',
                    )}
                  >
                    <span className="flex min-w-0 flex-1 items-center gap-2">
                      <Icon className="size-4 shrink-0" style={{ color }} />
                      <span className="truncate">{e.name}</span>
                    </span>
                    <span className="w-20 shrink-0 text-right font-mono text-[11px] text-muted">{e.kind === 'dir' ? '—' : formatSize(e.size)}</span>
                    <span className="w-36 shrink-0 pl-4 font-mono text-[11px] text-muted">{formatMtime(e.mtime)}</span>
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
            <span className="shrink-0">{t('apps.fileBrowser.itemCount', { count: entries.length })}</span>
            {listing?.truncated && <span className="shrink-0">· {t('apps.fileBrowser.listTruncated', { count: listing.limit ?? entries.length })}</span>}
          </span>
        </div>
      </div>

      {/* In-window image preview overlay (Finder-style quick look). In-window, not a portaled
          Dialog, so it stays inside the window's dark data-theme scope and bounds. */}
      {preview && (
        <div className="absolute inset-0 z-20 flex flex-col bg-surface">
          <div className="flex items-center gap-2 border-b border-border bg-surface-2/60 px-3 py-2">
            <ImageIcon className="size-4 shrink-0 text-muted" />
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
            <FilePreview kind="image" src={contentUrl(preview.path)} name={preview.name} />
          </div>
        </div>
      )}
    </div>
  );
};

const RailTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="px-1 pb-0.5 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">{children}</div>
);

const RailRow: React.FC<{ icon: React.ReactNode; label: string; active: boolean; onClick: () => void }> = ({ icon, label, active, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
      active ? 'bg-cyan-soft text-foreground' : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
    )}
  >
    <span className="shrink-0">{icon}</span>
    <span className="truncate">{label}</span>
  </button>
);
