import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Folder, FolderPlus, File as FileIcon, FolderOpen, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { sortProjectsByRecent } from '../../lib/projectOrder';
import { useIsDesktop } from '../../lib/useIsDesktop';
import {
  fileBrowserErrorMessage,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  pathCrumbs,
  resolveDirectoryPath,
  searchNames,
  systemFavorites,
  type Favorite,
  type FsEntry,
  type FsListing,
  type NameHit,
} from '../../lib/filesApi';
import { Button } from './button';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { FileBrowser, type FileBrowserRow } from './file-browser';
import { InlineNameInput } from './inline-name-input';

interface FolderBrowserProps {
  initialPath?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}

function sortEntries(entries: FsEntry[]): FsEntry[] {
  return [...entries].sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
}

function searchRow(hit: NameHit): FileBrowserRow {
  return { entry: hit, full: hit.path, dir: hit.path.slice(0, hit.path.length - hit.name.length).replace(/[\\/]$/, ''), rel: hit.rel };
}

function relativeFolder(rel: string | undefined): string {
  if (!rel || !rel.includes('/')) return '';
  return rel.slice(0, rel.lastIndexOf('/'));
}

function formatSize(size: number | null): string {
  if (size == null) return '—';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatMtime(seconds: number | null): string {
  if (seconds == null) return '—';
  const date = new Date(seconds * 1000);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return `${date.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { year: 'numeric', month: 'short', day: 'numeric' })} ${date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`;
}

function needsDirectoryResolution(path: string): boolean {
  return !path.startsWith('~') && !path.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(path) && !/^\\\\/.test(path);
}

export const FolderBrowser: React.FC<FolderBrowserProps> = ({ initialPath, onSelect, onClose }) => {
  const { t } = useTranslation();
  const { projects, projectsError } = useWorkbenchProjectsTree();
  const isDesktop = useIsDesktop();
  const [cwd, setCwd] = useState('');
  const [listing, setListing] = useState<FsListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listingError, setListingError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [sysFavs, setSysFavs] = useState<Favorite[]>([]);
  const [sysFavsLoaded, setSysFavsLoaded] = useState(false);
  const [query, setQuery] = useState('');
  const [searchRows, setSearchRows] = useState<FileBrowserRow[] | null>(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchRevision, setSearchRevision] = useState(0);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [searchTruncated, setSearchTruncated] = useState(false);
  const navSeq = useRef(0);
  const pendingNavigation = useRef<string | null>(null);
  const searchAbort = useRef<AbortController | null>(null);
  const initialPathHandled = useRef(false);
  const initialPathResolving = useRef<string | null>(null);
  const mounted = useRef(true);
  const previousShowHidden = useRef(showHidden);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const changeQuery = useCallback((value: string) => {
    searchAbort.current?.abort();
    setQuery(value);
    if (value.trim()) {
      setCreatingFolder(false);
      setNewFolderName('');
    }
    setSearchRows(null);
    setSearchTruncated(false);
    setSearchBusy(value.trim().length > 0);
    setError(null);
  }, []);

  const navigate = useCallback(
    (path: string, options: { preserveQuery?: boolean } = {}) => {
      initialPathHandled.current = true;
      previousShowHidden.current = showHidden;
      const seq = ++navSeq.current;
      pendingNavigation.current = path;
      if (options.preserveQuery) {
        setError(null);
      } else {
        changeQuery('');
      }
      setCreatingFolder(false);
      setNewFolderName('');
      setListingError(null);
      setLoading(true);
      listDir(path, showHidden)
        .then((result) => {
          if (seq !== navSeq.current) return;
          setCwd(result.path);
          setListing(result);
        })
        .catch((cause: unknown) => {
          if (seq === navSeq.current) {
            setListingError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.listFailed')));
          }
        })
        .finally(() => {
          if (seq === navSeq.current) {
            pendingNavigation.current = null;
            setLoading(false);
          }
        });
    },
    [changeQuery, showHidden, t],
  );

  useEffect(() => {
    systemFavorites()
      .then(setSysFavs)
      .catch(() => {})
      .finally(() => setSysFavsLoaded(true));
  }, []);

  useEffect(() => {
    if (initialPathHandled.current) return;
    if (!initialPath && projects === null && !projectsError) return;
    const recentProject = sortProjectsByRecent(projects || [])[0]?.folder_path;
    const home = sysFavs.find((favorite) => favorite.key === 'home')?.path;
    const start = initialPath || recentProject || (sysFavsLoaded ? home || '~' : undefined);
    if (!start) return;
    if (initialPathResolving.current === start) return;
    initialPathResolving.current = start;
    Promise.resolve().then(() => {
      return needsDirectoryResolution(start) ? resolveDirectoryPath(start) : start;
    }).then((resolved) => {
      if (mounted.current && !initialPathHandled.current && resolved) navigate(resolved);
    }).catch((cause: unknown) => {
      if (mounted.current) setListingError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.listFailed')));
    }).finally(() => {
      if (initialPathResolving.current === start) initialPathResolving.current = null;
    });
  }, [initialPath, navigate, projects, projectsError, sysFavs, sysFavsLoaded, t]);

  const refreshSearch = useCallback(() => {
    searchAbort.current?.abort();
    setSearchRows(null);
    setSearchTruncated(false);
    setSearchBusy(true);
    setError(null);
    setSearchRevision((revision) => revision + 1);
  }, []);

  const refreshCurrent = useCallback(() => {
    const target = pendingNavigation.current || cwd;
    if (!target) return;
    if (query.trim() && !pendingNavigation.current) {
      navigate(target, { preserveQuery: true });
      refreshSearch();
    } else {
      navigate(target);
    }
  }, [cwd, navigate, query, refreshSearch]);

  useEffect(() => {
    if (previousShowHidden.current === showHidden || !cwd) return;
    previousShowHidden.current = showHidden;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) refreshCurrent();
    });
    return () => {
      cancelled = true;
    };
  }, [cwd, refreshCurrent, showHidden]);

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed || !cwd) {
      return;
    }
    const controller = new AbortController();
    searchAbort.current = controller;
    const timeout = window.setTimeout(() => {
      searchNames(cwd, trimmed, showHidden, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          setSearchRows(result.results.map(searchRow));
          setSearchTruncated(result.truncated);
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          setError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.searchFailed')));
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearchBusy(false);
        });
    }, 220);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [cwd, query, searchRevision, showHidden, t]);

  const projectFavs = useMemo(
    () => (projects || []).filter((project) => !!project.folder_path).map((project) => ({ label: project.display_name, path: project.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];
  const inSearch = query.trim().length > 0;
  const rows = useMemo<FileBrowserRow[]>(
    () =>
      inSearch
        ? searchRows || []
        : sortEntries(listing?.entries || []).map((entry) => ({ entry, full: joinPath(cwd, entry.name), dir: cwd })),
    [cwd, inSearch, listing, searchRows],
  );
  const showSpinner = loading && !listing;
  const showEmpty =
    !loading &&
    !listingError &&
    !creatingFolder &&
    (inSearch ? !searchBusy && searchRows !== null && searchRows.length === 0 : listing !== null && listing.entries.length === 0);

  const cancelCreateFolder = () => {
    setCreatingFolder(false);
    setNewFolderName('');
    setError(null);
  };

  const createFolder = async (name: string) => {
    const trimmed = name.trim();
    if (!trimmed) {
      cancelCreateFolder();
      return;
    }
    if (!isPlainEntryName(trimmed)) {
      setError(t('apps.fileBrowser.errors.invalid_name'));
      return;
    }
    try {
      await makeDir(joinPath(cwd, trimmed));
      cancelCreateFolder();
      navigate(cwd);
    } catch (cause: unknown) {
      setError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.createFolderFailed')));
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        closeLabel={t('common.close')}
        mobileSheetHeight="tall"
        className="flex h-[min(84dvh,760px)] max-w-5xl flex-col gap-0 overflow-hidden border-border-strong bg-surface p-0 max-md:p-0 max-md:pb-0"
        onEscapeKeyDown={(event) => {
          if (creatingFolder) {
            event.preventDefault();
            cancelCreateFolder();
          }
        }}
      >
        <div className="flex shrink-0 items-center gap-3 border-b border-border bg-surface-2 py-4 pl-4 pr-12">
          <FolderOpen className="size-4 shrink-0 text-mint-ink" />
          <DialogTitle className="truncate text-[13px] font-semibold text-foreground">{t('directoryBrowser.title')}</DialogTitle>
        </div>

        <FileBrowser
          fullBleed
          className="min-h-0 flex-1"
          mobileRoute={!isDesktop}
          cwd={cwd}
          crumbs={crumbs}
          sysFavs={sysFavs}
          projectFavs={projectFavs}
          loading={loading}
          searchBusy={searchBusy}
          query={query}
          onQueryChange={changeQuery}
          onRefresh={refreshCurrent}
          onNavigate={navigate}
          showHidden={showHidden}
          onShowHiddenChange={setShowHidden}
          error={listingError || error}
          toolbarActions={
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-7 shrink-0 gap-1.5 px-2.5 text-[12px]"
              disabled={!cwd || loading || !!listingError || creatingFolder}
              onClick={() => {
                changeQuery('');
                setNewFolderName('');
                setCreatingFolder(true);
              }}
            >
              <FolderPlus className="size-3.5" />
              {t('apps.fileBrowser.newFolder')}
            </Button>
          }
          listHeader={
            <div className="flex items-center border-b border-border px-3 py-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">
              <span className="min-w-0 flex-1">{t('apps.fileBrowser.colName')}</span>
              <span className="hidden w-20 shrink-0 text-right sm:block">{t('apps.fileBrowser.colSize')}</span>
              <span className="hidden w-36 shrink-0 pl-4 sm:block">{t('apps.fileBrowser.colModified')}</span>
            </div>
          }
          listContent={
            <>
              {showSpinner && <div className="grid place-items-center py-8"><Loader2 className="size-4 animate-spin text-muted" /></div>}
              {!inSearch && creatingFolder && (
                <div className="flex items-center px-3 py-1.5" onContextMenu={(event) => event.stopPropagation()}>
                  <span className="flex min-w-0 flex-1 items-center gap-2">
                    <Folder className="size-4 shrink-0 text-cyan-ink" />
                    <InlineNameInput
                      initial=""
                      value={newFolderName}
                      onChange={setNewFolderName}
                      placeholder={t('apps.fileBrowser.newFolderPlaceholder')}
                      onCommit={(value) => void createFolder(value)}
                      onCancel={cancelCreateFolder}
                      className="min-w-0 flex-1 rounded border border-cyan bg-surface px-1.5 py-0.5 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
                    />
                  </span>
                </div>
              )}
              {showEmpty && (
                <div className="px-3 py-8 text-center text-[12px] text-muted">
                  {inSearch ? t('apps.fileBrowser.noMatches') : t('apps.fileBrowser.empty')}
                </div>
              )}
              {rows.map((row) => {
                const isDir = row.entry.kind === 'dir';
                const folder = relativeFolder(row.rel);
                return (
                  <button
                    key={row.full}
                    type="button"
                    disabled={!isDir}
                    onClick={() => isDir && navigate(row.full)}
                    className={clsx(
                      'flex w-full items-center px-3 py-1.5 text-left text-[12.5px] transition',
                      isDir ? 'text-foreground hover:bg-foreground/[0.04]' : 'cursor-default text-muted/70',
                    )}
                  >
                    {isDir ? <Folder className="size-4 shrink-0 text-cyan-ink" /> : <FileIcon className="size-4 shrink-0 text-muted" />}
                    <span className="ml-2 flex min-w-0 flex-1 items-center gap-2">
                      <span className="truncate">{row.entry.name}</span>
                      {folder && <span className="min-w-0 shrink truncate text-[11px] text-muted">{folder}</span>}
                    </span>
                    <span className="hidden w-20 shrink-0 text-right font-mono text-[11px] text-muted sm:block">
                      {isDir ? '—' : formatSize(row.entry.size)}
                    </span>
                    <span className="hidden w-36 shrink-0 pl-4 font-mono text-[11px] text-muted sm:block">{formatMtime(row.entry.mtime)}</span>
                  </button>
                );
              })}
            </>
          }
          footerContent={
            <div className="flex items-center gap-3 border-t border-border bg-surface-2/80 px-3 py-2">
              <code className="min-w-0 flex-1 truncate rounded-md border border-border-strong bg-surface-3 px-2.5 py-1.5 font-mono text-[11px] text-foreground">
                {cwd || '—'}
              </code>
              <Button type="button" size="sm" variant="ghost" className="h-8 px-3 text-[12px]" onClick={onClose}>
                {t('common.cancel')}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="brand"
                className="h-8 gap-1.5 px-3.5 text-[12px]"
                disabled={!cwd || loading || !!listingError}
                onClick={() => cwd && onSelect(cwd)}
              >
                <FolderOpen className="size-3.5" />
                {t('directoryBrowser.select')}
              </Button>
            </div>
          }
          statusContent={
            <span className="ml-auto flex min-w-0 items-center gap-2 font-mono">
              <span className="truncate">{cwd || '—'}</span>
              <span className="shrink-0">
                {inSearch ? t('apps.fileBrowser.searchCount', { count: rows.length }) : t('apps.fileBrowser.itemCount', { count: rows.length })}
              </span>
              {!inSearch && listing?.truncated && <span className="shrink-0">· {t('apps.fileBrowser.listTruncated', { count: listing.limit ?? rows.length })}</span>}
              {inSearch && searchTruncated && <span className="shrink-0">· {t('apps.fileBrowser.searchTruncated')}</span>}
            </span>
          }
        />
      </DialogContent>
    </Dialog>
  );
};
