import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Braces,
  ChevronRight,
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
  type LucideIcon,
} from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { isEditableFile } from '../../lib/filePreview';
import {
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

  // Open: dir → navigate; editable text/code file (within the size cap) → Editor window;
  // anything else → raw download. (Richer per-type "default open" is a later design.)
  const open = async (e: FsEntry) => {
    const full = joinPath(cwd, e.name);
    if (e.kind === 'dir') {
      navigate(full);
      return;
    }
    // Non-editable (symlink / binary / oversized — gated by the listing entry's kind+size) or
    // mobile (the editor window layer is hidden below md): download. This branch MUST run
    // synchronously, before any await, or the click's user activation is lost and Safari/iOS block
    // the popup.
    const desktop = window.matchMedia('(min-width: 768px)').matches;
    if (!isEditableFile(e) || !desktop) {
      downloadFile(full);
      return;
    }
    // Editable + desktop → editor window. Fetch CURRENT metadata: it gives the save-baseline mtime
    // AND re-validates the open (the file may have grown past the cap or become a symlink since it
    // was listed) — if it's no longer editable, download instead. Safe to await: opening an internal
    // window needs no user activation, and the rare changed-since-listing download is acceptable.
    try {
      const m = await fileMeta(full);
      if (!isEditableFile(m)) {
        downloadFile(full);
        return;
      }
      wm.openApp('editor', { title: e.name, params: { path: full, filename: e.name, mtime: m.mtime } });
    } catch {
      // Metadata fetch failed — open with the listing mtime as the baseline.
      wm.openApp('editor', { title: e.name, params: { path: full, filename: e.name, mtime: e.mtime } });
    }
  };

  const createNamed = async (kind: 'file' | 'dir') => {
    const raw = window.prompt(t(kind === 'dir' ? 'apps.fileBrowser.newFolderPrompt' : 'apps.fileBrowser.newFilePrompt'));
    if (raw == null) return;
    const name = raw.trim();
    if (name === '') return;
    if (!isPlainEntryName(name)) {
      setError(t('apps.fileBrowser.errors.invalid_name'));
      return;
    }
    const target = joinPath(cwd, name);
    try {
      if (kind === 'dir') await makeDir(target);
      // create-only: the backend atomically refuses (errors.exists) when the name is taken, so a
      // typo can't truncate an existing file.
      else await writeFile(target, '', undefined, true);
      navigate(cwd);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t(kind === 'dir' ? 'apps.fileBrowser.errors.createFolderFailed' : 'apps.fileBrowser.errors.saveFailed')));
    }
  };

  const projectFavs = useMemo(
    () => (projects || []).filter((p) => !!p.folder_path).map((p) => ({ label: p.display_name, path: p.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];
  const entries = useMemo(() => {
    const all = listing?.entries ?? [];
    const q = query.trim().toLowerCase();
    const filtered = q ? all.filter((e) => e.name.toLowerCase().includes(q)) : all;
    return [...filtered].sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
  }, [listing, query]);
  const selectedEntry = useMemo(
    () => (selected ? entries.find((e) => joinPath(cwd, e.name) === selected) ?? null : null),
    [entries, cwd, selected],
  );

  return (
    <div className={windowed ? 'flex h-full w-full flex-col bg-surface' : 'flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]'}>
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
          <Button type="button" size="sm" variant="brand" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd} onClick={() => void createNamed('file')}>
            <FilePlus className="size-3.5" /> {t('apps.fileBrowser.newFile')}
          </Button>
          <Button type="button" size="sm" variant="outline" className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]" disabled={!cwd} onClick={() => void createNamed('dir')}>
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
              <span className="min-w-0 flex-1">{t('apps.fileBrowser.colName')}</span>
              <span className="w-20 shrink-0 text-right">{t('apps.fileBrowser.colSize')}</span>
              <span className="w-36 shrink-0 pl-4">{t('apps.fileBrowser.colModified')}</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto py-1">
              {loading && !listing && (
                <div className="grid place-items-center py-8"><Loader2 className="size-4 animate-spin text-muted" /></div>
              )}
              {listing && entries.length === 0 && (
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
