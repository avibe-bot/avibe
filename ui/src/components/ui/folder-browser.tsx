import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Folder, FolderPlus, File as FileIcon, FolderOpen, Keyboard, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { sortProjectsByRecent } from '../../lib/projectOrder';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
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

type NavigationRequest = {
  path: string;
  resolve?: boolean;
  history: 'push' | 'refresh' | number;
  // Only a manual submission can dismiss its own, unchanged editor.
  editRevision?: number;
};

type SearchResult = { key: string; rows: FileBrowserRow[]; truncated: boolean; error?: string };

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

export const FolderBrowser: React.FC<FolderBrowserProps> = ({ initialPath, onSelect, onClose }) => {
  const { t } = useTranslation();
  const { projects, projectsError } = useWorkbenchProjectsTree();
  const surfaceActive = useRouteSurfaceActive();
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
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null);
  const [searchRevision, setSearchRevision] = useState(0);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [pathEditing, setPathEditing] = useState(false);
  const [pathInput, setPathInput] = useState('');
  const [pathError, setPathError] = useState<string | null>(null);
  const [history, setHistory] = useState<{ paths: string[]; index: number }>({ paths: [], index: -1 });
  const navSeq = useRef(0);
  const pendingNavigation = useRef<NavigationRequest | null>(null);
  const lastNavigation = useRef<NavigationRequest | null>(null);
  const loadedDirectory = useRef<{ path: string; hidden: boolean } | null>(null);
  const searchAbort = useRef<AbortController | null>(null);
  const initialPathHandled = useRef(false);
  const pathInputRef = useRef<HTMLInputElement | null>(null);
  const pathEditRevision = useRef(0);
  const pathSelection = useRef<{ start: number; end: number; direction: 'forward' | 'backward' | 'none' } | null>(null);
  const createSeq = useRef(0);
  const mounted = useRef(true);
  const showHiddenRef = useRef(showHidden);
  const foreground = useRef(surfaceActive);
  // A search result/error is valid only for the directory, query and options
  // that produced it. Navigation can finish while the user is still searching.
  const searchKey = JSON.stringify([cwd, query.trim(), showHidden, searchRevision]);
  const currentSearch = searchResult?.key === searchKey ? searchResult : null;
  const searchRows = currentSearch?.rows ?? null;
  const searchTruncated = currentSearch?.truncated ?? false;
  const searchBusy = !!query.trim() && !currentSearch;

  useLayoutEffect(() => { foreground.current = surfaceActive; }, [surfaceActive]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const cancelCreateFolder = useCallback(() => {
    createSeq.current += 1;
    setCreatingFolder(false);
    setNewFolderName('');
    setError(null);
  }, []);

  const changeQuery = useCallback((value: string) => {
    searchAbort.current?.abort();
    setQuery(value);
    if (value.trim()) cancelCreateFolder();
    setSearchResult(null);
    setError(null);
  }, [cancelCreateFolder]);

  const capturePathSelection = (input: HTMLInputElement) => {
    if (!foreground.current || !input.isConnected || input.ownerDocument.activeElement !== input) return;
    pathSelection.current = {
      start: input.selectionStart ?? 0,
      end: input.selectionEnd ?? 0,
      direction: input.selectionDirection ?? 'none',
    };
  };

  const focusPathInput = useCallback(() => {
    const input = pathInputRef.current;
    if (!input || !foreground.current) return;
    input.focus();
    if (pathSelection.current) {
      const { start, end, direction } = pathSelection.current;
      input.setSelectionRange(start, end, direction);
    } else {
      input.select();
    }
  }, []);

  useEffect(() => {
    if (pathEditing && surfaceActive) focusPathInput();
  }, [focusPathInput, pathEditing, surfaceActive]);

  const publishListing = useCallback((result: FsListing, hidden: boolean) => {
    loadedDirectory.current = { path: result.path, hidden };
    setCwd(result.path);
    setListing(result);
  }, []);

  const runNavigation = useCallback(
    async (request: NavigationRequest) => {
      // Resolution and listing belong to the same intent. Every completion
      // (including errors/finally) checks this one owner before publishing.
      const seq = ++navSeq.current;
      const current = () => mounted.current && seq === navSeq.current;
      pendingNavigation.current = request;
      lastNavigation.current = request;
      initialPathHandled.current = true;
      setPathError(null);
      cancelCreateFolder();
      setListingError(null);
      setLoading(true);
      try {
        if (request.resolve) {
          const resolved = await resolveDirectoryPath(request.path);
          if (!current()) return;
          // Refreshes reuse this canonical destination and the history intent.
          request = { ...request, path: resolved, resolve: false };
          pendingNavigation.current = request;
          lastNavigation.current = request;
        }
        const hidden = showHiddenRef.current;
        const result = await listDir(request.path, hidden);
        if (!current()) return;
        publishListing(result, hidden);
        setHistory((previous) => {
          if (request.history === 'refresh') return previous;
          if (typeof request.history === 'number') {
            const paths = [...previous.paths];
            paths[request.history] = result.path;
            return { paths, index: request.history };
          }
          if (previous.paths[previous.index] === result.path) return previous;
          const paths = [...previous.paths.slice(0, previous.index + 1), result.path];
          return { paths, index: paths.length - 1 };
        });
        if (request.editRevision !== undefined && request.editRevision === pathEditRevision.current) {
          setPathEditing(false);
        }
      } catch (cause: unknown) {
        if (!current()) return;
        if (request.editRevision !== undefined) {
          if (request.editRevision === pathEditRevision.current) {
            setPathError(fileBrowserErrorMessage(cause, t, t('directoryBrowser.pathNotFound')));
          }
        } else {
          setListingError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.listFailed')));
        }
        // The failed destination did not change cwd, but a hidden-file toggle
        // may have invalidated its retained source listing. Reconcile that
        // source once, under this same owner, without hiding the path error.
        const source = loadedDirectory.current;
        if (source && source.hidden !== showHiddenRef.current) {
          const hidden = showHiddenRef.current;
          pendingNavigation.current = { path: source.path, history: 'refresh' };
          try {
            const result = await listDir(source.path, hidden);
            if (current()) publishListing(result, hidden);
          } catch (refreshCause: unknown) {
            if (current()) setListingError(fileBrowserErrorMessage(refreshCause, t, t('apps.fileBrowser.errors.listFailed')));
          }
        }
      } finally {
        if (current()) {
          pendingNavigation.current = null;
          setLoading(false);
        }
      }
    },
    [cancelCreateFolder, publishListing, t],
  );

  const navigate = useCallback((path: string, resolve = false) => {
    changeQuery('');
    void runNavigation({ path, resolve, history: 'push' });
  }, [changeQuery, runNavigation]);

  const submitPath = useCallback(() => {
    const target = pathInput.trim();
    if (!target) return;
    changeQuery('');
    void runNavigation({ path: target, resolve: true, history: 'push', editRevision: pathEditRevision.current });
  }, [changeQuery, pathInput, runNavigation]);

  useEffect(() => {
    let cancelled = false;
    systemFavorites()
      .then((favorites) => { if (!cancelled) setSysFavs(favorites); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setSysFavsLoaded(true); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (initialPathHandled.current) return;
    if (!initialPath && projects === null && !projectsError) return;
    const recentProject = sortProjectsByRecent(projects || [])[0]?.folder_path;
    const home = sysFavs.find((favorite) => favorite.key === 'home')?.path;
    const start = initialPath || recentProject || (sysFavsLoaded ? home || '~' : undefined);
    if (!start) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled && !initialPathHandled.current) navigate(start, true);
    });
    return () => { cancelled = true; };
  }, [initialPath, navigate, projects, projectsError, sysFavs, sysFavsLoaded]);

  const refreshSearch = useCallback(() => {
    searchAbort.current?.abort();
    setSearchResult(null);
    setError(null);
    setSearchRevision((revision) => revision + 1);
  }, []);

  const refreshCurrent = useCallback(() => {
    // A resolver will read the latest hidden setting before starting its list.
    // Never reissue an unresolved external path directly to the Files API.
    if (pendingNavigation.current?.resolve) return;
    const request = pendingNavigation.current || (cwd ? { path: cwd, history: 'refresh' as const } : lastNavigation.current);
    if (request) void runNavigation(request);
    if (query.trim()) refreshSearch();
  }, [cwd, query, refreshSearch, runNavigation]);

  const cancelPathEdit = useCallback(() => {
    pathEditRevision.current += 1;
    // Editing is independent of navigation, but Escape cancels an admitted
    // manual submission in either its resolver or listing phase.
    if (pendingNavigation.current?.editRevision !== undefined) {
      navSeq.current += 1;
      pendingNavigation.current = null;
      lastNavigation.current = null;
      setLoading(false);
      const source = loadedDirectory.current;
      if (source && source.hidden !== showHiddenRef.current) {
        void runNavigation({ path: source.path, history: 'refresh' });
      }
    }
    setPathEditing(false);
    setPathError(null);
  }, [runNavigation]);

  const goToHistory = (index: number) => {
    changeQuery('');
    void runNavigation({ path: history.paths[index], history: index });
  };

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
          setSearchResult({ key: searchKey, rows: result.results.map(searchRow), truncated: result.truncated });
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          setSearchResult({
            key: searchKey, rows: [], truncated: false,
            error: fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.searchFailed')),
          });
        });
    }, 220);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [cwd, query, searchKey, showHidden, t]);

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
    !currentSearch?.error &&
    !creatingFolder &&
    (inSearch ? !searchBusy && searchRows !== null && searchRows.length === 0 : listing !== null && listing.entries.length === 0);

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
    const seq = ++createSeq.current;
    try {
      await makeDir(joinPath(cwd, trimmed));
      if (!mounted.current || seq !== createSeq.current) return;
      cancelCreateFolder();
      refreshCurrent();
    } catch (cause: unknown) {
      if (mounted.current && seq === createSeq.current) {
        setError(fileBrowserErrorMessage(cause, t, t('apps.fileBrowser.errors.createFolderFailed')));
      }
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        closeLabel={t('common.close')}
        mobileSheetHeight="tall"
        className="flex h-[min(84dvh,760px)] max-w-5xl flex-col gap-0 overflow-hidden border-border-strong bg-surface p-0 max-md:p-0 max-md:pb-0"
        onOpenAutoFocus={(event) => {
          if (pathEditing) {
            event.preventDefault();
            focusPathInput();
          }
        }}
        onEscapeKeyDown={(event) => {
          if (creatingFolder) {
            event.preventDefault();
            cancelCreateFolder();
          } else if (pathEditing) {
            event.preventDefault();
            cancelPathEdit();
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
          history={{
            onBack: history.index > 0 ? () => goToHistory(history.index - 1) : undefined,
            onForward: history.index < history.paths.length - 1 ? () => goToHistory(history.index + 1) : undefined,
          }}
          navigationControl={
            pathEditing ? (
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex min-w-0 items-center gap-1.5 rounded-lg border border-cyan/40 bg-cyan/[0.06] px-2 py-1">
                  <input
                    ref={pathInputRef}
                    type="text"
                    value={pathInput}
                    aria-label={t('directoryBrowser.editPath')}
                    onChange={(event) => {
                      capturePathSelection(event.currentTarget);
                      pathEditRevision.current += 1;
                      setPathInput(event.target.value);
                      setPathError(null);
                    }}
                    onSelect={(event) => capturePathSelection(event.currentTarget)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        void submitPath();
                      }
                    }}
                    placeholder={t('directoryBrowser.editPathPlaceholder')}
                    className="min-w-0 flex-1 bg-transparent font-mono text-[11px] text-foreground outline-none placeholder:text-muted"
                  />
                  <button
                    type="button"
                    onClick={() => void submitPath()}
                    className="shrink-0 rounded px-2 py-0.5 text-[10px] font-semibold text-cyan-ink hover:bg-foreground/[0.04]"
                  >
                    {t('directoryBrowser.editPathDone')}
                  </button>
                </div>
                {pathError && <div className="px-1 text-[10.5px] text-destructive-ink">{pathError}</div>}
              </div>
            ) : (
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-7 shrink-0 text-muted"
                aria-label={t('directoryBrowser.editPath')}
                title={t('directoryBrowser.editPath')}
                onClick={() => {
                  pathEditRevision.current += 1;
                  pathSelection.current = null;
                  setPathInput(cwd);
                  setPathError(null);
                  setPathEditing(true);
                }}
              >
                <Keyboard className="size-3.5" />
              </Button>
            )
          }
          showHidden={showHidden}
          onShowHiddenChange={(hidden) => {
            showHiddenRef.current = hidden;
            setShowHidden(hidden);
            refreshCurrent();
          }}
          onFavoriteNavigate={(path) => navigate(path, true)}
          error={listingError || error || currentSearch?.error}
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
                      onChange={(value) => {
                        createSeq.current += 1;
                        setNewFolderName(value);
                        setError(null);
                      }}
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
