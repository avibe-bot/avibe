import type { TranslationKey } from '@/i18n/types';
import React, { useEffect, useState, useCallback, useLayoutEffect, useRef } from 'react';
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  Eye,
  EyeOff,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
  Home,
  Keyboard,
  LayoutGrid,
  Monitor,
  RefreshCw,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import { Button } from './button';
import { Dialog, DialogContent, DialogTitle } from './dialog';
import { errorMessage } from '@/lib/errorMessage';
import { useRouteSurfaceActive, useRouteSurfaceWindowEvent } from '@/lib/routeSurfaceActivity';

interface DirectoryBrowserProps {
  /** Initial path to show when opening */
  initialPath?: string;
  /** Called when user confirms selection */
  onSelect: (path: string) => void;
  /** Called when user cancels / closes */
  onClose: () => void;
}

// Per-OS quick-access shortcuts come from the backend (it knows the platform
// and verifies each path exists). Well-known shortcuts get a localized label;
// OS roots like /tmp, /data or a Windows drive are shown by their path. Both
// label and icon are keyed off the backend's stable ``key``.
const FAVORITE_I18N: Record<string, TranslationKey> = {
  home: 'directoryBrowser.favoritesHome',
  desktop: 'directoryBrowser.favoritesDesktop',
  documents: 'directoryBrowser.favoritesDocuments',
  downloads: 'directoryBrowser.favoritesDownloads',
  applications: 'directoryBrowser.favoritesApplications',
};

const favoriteIcon = (key: string): React.ReactNode => {
  const cls = 'size-3.5';
  switch (key) {
    case 'home':
      return <Home className={cls} />;
    case 'desktop':
      return <Monitor className={cls} />;
    case 'documents':
      return <FileText className={cls} />;
    case 'downloads':
      return <Download className={cls} />;
    case 'applications':
      return <LayoutGrid className={cls} />;
    case 'data':
      return <Database className={cls} />;
    default:
      // root / mnt / media / drive_* → a volume; anything else a plain folder.
      if (key === 'root' || key === 'mnt' || key === 'media' || key.startsWith('drive_')) {
        return <HardDrive className={cls} />;
      }
      return <Folder className={cls} />;
  }
};

// Mirrors design.pen y5cQ5 — macOS Finder-style folder picker: traffic
// lights + toolbar (history nav + breadcrumb + show-hidden + new-folder)
// + favorites sidebar + folder list + footer (path + cancel + select).
export const DirectoryBrowser: React.FC<DirectoryBrowserProps> = ({
  initialPath,
  onSelect,
  onClose,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const foreground = useRef(surfaceActive);
  useLayoutEffect(() => { foreground.current = surfaceActive; }, [surfaceActive]);

  const [currentPath, setCurrentPath] = useState('');
  // Resolved user home — captured on the first browse('~') response so the
  // breadcrumb collapse to ⌂ has a real prefix to match against. Falls back
  // to '' (no collapse) if the user opened the picker with an absolute path.
  const [homePath, setHomePath] = useState<string>('');
  const [parent, setParent] = useState<string | null>(null);
  const [dirs, setDirs] = useState<{ name: string; path: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  // Linear history so the < / > arrows behave like a real file browser
  // instead of just walking the directory tree.
  const [history, setHistory] = useState<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  // Inline "create new folder" prompt — shown at the end of the list so
  // it feels like a continuation of the directory rather than a modal.
  const [creating, setCreating] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);
  const newFolderInputRef = useRef<HTMLInputElement | null>(null);

  // Manual-path mode — flips the breadcrumb into an editable text input
  // (mirrors Finder's Cmd+Shift+G behavior) so users can paste an
  // arbitrary path and navigate to it directly.
  const [pathEditing, setPathEditing] = useState(false);
  const [pathInput, setPathInput] = useState('');
  const [pathError, setPathError] = useState<string | null>(null);
  const pathInputRef = useRef<HTMLInputElement | null>(null);
  const pathEditRevision = useRef(0);
  const pathSelection = useRef<{ start: number; end: number; direction: 'forward' | 'backward' | 'none' } | null>(null);

  // OS-appropriate quick-access shortcuts, resolved + existence-checked by the
  // backend (macOS Finder entries, Linux /tmp·/data·roots, Windows drives…).
  const [favorites, setFavorites] = useState<{ key: string; path: string }[]>([]);

  const mountedRef = useRef(true);
  const reqIdRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useRouteSurfaceWindowEvent('keydown', (event) => {
    if (!event.defaultPrevented && (event.metaKey || event.ctrlKey)
      && event.key.toLowerCase() === 'n' && !creating) {
      event.preventDefault();
      setCreating(true);
    }
  });

  useEffect(() => {
    if (surfaceActive && creating) newFolderInputRef.current?.focus();
  }, [creating, surfaceActive]);

  const fetchPath = useCallback(
    async (path: string, hidden?: boolean) => {
      const id = ++reqIdRef.current;
      setLoading(true);
      setError(null);
      const useHidden = hidden ?? showHidden;
      try {
        const result = await api.browseDirectory(path, useHidden);
        if (!mountedRef.current || reqIdRef.current !== id) {
          return null;
        }
        if (result.ok) {
          const resolvedPath = result.path ?? path;
          setCurrentPath(resolvedPath);
          setParent(result.parent ?? null);
          setDirs(result.dirs ?? []);
          // Snapshot the resolved user home the first time the backend
          // expands a tilde-prefixed path. Backend behavior: passing "~"
          // returns the home directory in result.path; passing "~/x"
          // returns "<home>/x". We derive home by trimming the requested
          // suffix off the resolved path.
          if (!homePath && path.startsWith('~')) {
            const suffix = path === '~' ? '' : path.slice(1);
            const derived = suffix && resolvedPath.endsWith(suffix)
              ? resolvedPath.slice(0, resolvedPath.length - suffix.length)
              : resolvedPath;
            const cleaned = derived.replace(/\/+$/, '');
            if (cleaned) setHomePath(cleaned);
          }
          return resolvedPath;
        }
        setError(result.error ?? 'Unknown error');
        return null;
      } catch (e) {
        if (!mountedRef.current || reqIdRef.current !== id) return null;
        setError(errorMessage(e) ?? String(e));
        return null;
      } finally {
        if (mountedRef.current && reqIdRef.current === id) {
          setLoading(false);
        }
      }
    },
    [api, showHidden, homePath],
  );

  const navigate = useCallback(
    async (path: string, hidden?: boolean) => {
      // Reset the inline new-folder state whenever the user moves around —
      // a half-typed name shouldn't survive navigating away.
      setCreating(false);
      setNewFolderName('');
      setCreateError(null);
      const resolved = await fetchPath(path, hidden);
      if (resolved) {
        setHistory((prev) => [...prev.slice(0, historyIndex + 1), resolved]);
        setHistoryIndex((prev) => prev + 1);
      }
    },
    [fetchPath, historyIndex],
  );

  const goBack = useCallback(() => {
    if (historyIndex <= 0) return;
    const next = historyIndex - 1;
    setHistoryIndex(next);
    fetchPath(history[next]);
  }, [history, historyIndex, fetchPath]);

  const goForward = useCallback(() => {
    if (historyIndex >= history.length - 1) return;
    const next = historyIndex + 1;
    setHistoryIndex(next);
    fetchPath(history[next]);
  }, [history, historyIndex, fetchPath]);

  useEffect(() => {
    navigate(initialPath || '~');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load OS-appropriate shortcuts once on open. The backend knows the platform
  // and verifies each path, so we render exactly what it returns (no dead rows,
  // no client-side OS guesswork).
  useEffect(() => {
    let cancelled = false;
    api
      .browseFavorites()
      .then((res) => {
        if (!cancelled && res.ok && res.favorites) setFavorites(res.favorites);
      })
      .catch(() => {
        /* leave favorites empty on failure — the folder list still works */
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    if (pathEditing && foreground.current) {
      pathInputRef.current?.focus();
      pathInputRef.current?.select();
    }
  }, [pathEditing]);

  const togglePathEditing = () => {
    if (!foreground.current) return;
    pathEditRevision.current++;
    if (!pathEditing) {
      // Snapshot only when an edit begins. Later browse responses update the
      // directory and history, not the user's unconfirmed text or selection.
      setPathInput(currentPath);
      setPathError(null);
      pathSelection.current = null;
    }
    setPathEditing(!pathEditing);
  };

  const cancelPathEditing = () => {
    pathEditRevision.current++;
    setPathEditing(false);
  };

  const submitManualPath = async () => {
    if (!foreground.current) return;
    const target = pathInput.trim();
    if (!target) return;
    const editRevision = pathEditRevision.current;
    setPathError(null);
    const request = fetchPath(target);
    const requestId = reqIdRef.current;
    const resolved = await request;
    if (!mountedRef.current || reqIdRef.current !== requestId) return;
    if (resolved) {
      // Mirror `navigate` history bookkeeping so the back arrow works.
      setHistory((prev) => [...prev.slice(0, historyIndex + 1), resolved]);
      setHistoryIndex((prev) => prev + 1);
      // A submitted target may resolve after the user starts another edit.
      // Keep its navigation result without dismissing that newer draft.
      if (pathEditRevision.current === editRevision) setPathEditing(false);
    } else if (pathEditRevision.current === editRevision) {
      setPathError(t('directoryBrowser.pathNotFound'));
    }
  };

  const toggleHidden = () => {
    const next = !showHidden;
    setShowHidden(next);
    fetchPath(currentPath, next);
  };

  const submitNewFolder = async () => {
    if (!foreground.current) return;
    const name = newFolderName.trim();
    if (!name) return;
    setCreateError(null);
    try {
      const target = currentPath ? `${currentPath.replace(/\/$/, '')}/${name}` : name;
      await api.browseMkdir(target);
      setCreating(false);
      setNewFolderName('');
      await fetchPath(currentPath);
    } catch (err) {
      const message: string = errorMessage(err) ?? String(err);
      if (/exists/i.test(message)) {
        setCreateError(t('directoryBrowser.newFolderExists'));
      } else {
        setCreateError(message);
      }
    }
  };

  // Breadcrumb: collapse the home prefix to ⌂ so deeply-nested paths still fit
  // in the toolbar without scrolling. ``homePath`` is captured on the first
  // tilde-expanded browse response (see fetchPath).
  const breadcrumbs = (() => {
    if (!currentPath) return [] as { label: string; path: string; isHome?: boolean }[];
    const startsAtHome = !!homePath && currentPath.startsWith(homePath);
    const segments = (startsAtHome ? currentPath.slice(homePath.length) : currentPath)
      .split('/')
      .filter(Boolean);
    const out: { label: string; path: string; isHome?: boolean }[] = [];
    if (startsAtHome) {
      // At home itself, show the full path — a bare ⌂ is cryptic. Collapse to
      // ⌂ only once you're deeper in, where it keeps long paths readable (the
      // full path still lives in the footer and the home crumb's tooltip).
      out.push({ label: segments.length === 0 ? homePath : '⌂', path: homePath, isHome: true });
    } else {
      out.push({ label: '/', path: '/' });
    }
    let acc = startsAtHome ? homePath : '';
    for (const seg of segments) {
      acc = acc === '/' || acc === '' ? `${acc}${seg}` : `${acc}/${seg}`;
      if (!acc.startsWith('/')) acc = `/${acc}`;
      out.push({ label: seg, path: acc });
    }
    return out;
  })();

  const canConfirm = !!currentPath && !loading && !error;
  const canBack = historyIndex > 0;
  const canForward = historyIndex < history.length - 1;

  // Keep this owner (including unconfirmed inputs/history) mounted, while
  // withdrawing the complete modal layer and its focus/pointer/scroll effects.
  if (!surfaceActive) return null;

  return (
    <Dialog open onOpenChange={(open) => { if (!open && foreground.current) onClose(); }}>
      <DialogContent
        onOpenAutoFocus={(event) => {
          if (!foreground.current) { event.preventDefault(); return; }
          // The portal mounts after its retained owner's effects. Resume the
          // editor here, without reinitializing or selecting its draft text.
          const editor = creating ? newFolderInputRef.current : pathEditing ? pathInputRef.current : null;
          if (editor) {
            event.preventDefault();
            editor.focus();
            if (editor === pathInputRef.current && pathSelection.current) {
              const { start, end, direction } = pathSelection.current;
              editor.setSelectionRange(start, end, direction);
            }
          }
        }}
        onCloseAutoFocus={(event) => { if (!foreground.current) event.preventDefault(); }}
        aria-describedby={undefined}
        closeLabel={t('directoryBrowser.cancel')}
        onEscapeKeyDown={(event) => {
          if (creating || pathEditing) {
            event.preventDefault();
            setCreating(false);
            setNewFolderName('');
            setCreateError(null);
            cancelPathEditing();
          }
        }}
        className="flex h-[80dvh] max-h-[720px] min-h-0 w-full max-w-3xl flex-col gap-0 overflow-hidden rounded-2xl border-border-strong bg-surface p-0 max-md:h-[90dvh] max-md:p-0 max-md:pb-0"
      >
        {/* Traffic-light header */}
        <div className="flex items-center gap-3 border-b border-border bg-surface-2 px-4 py-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-label={t('directoryBrowser.cancel')}
              onClick={onClose}
              className="size-3 rounded-full bg-[#FF5F57] transition hover:brightness-110"
            />
            <span className="size-3 rounded-full bg-[#FFBD2E]" />
            <span className="size-3 rounded-full bg-[#28C840]" />
          </div>
          <DialogTitle className="flex flex-1 items-center justify-center gap-2 pr-8 text-[13px] font-semibold text-foreground">
            <FolderOpen className="size-3.5 text-mint-ink" />
            {t('directoryBrowser.title')}
          </DialogTitle>

        </div>

        {/* Toolbar — history arrows + breadcrumb + show-hidden + new-folder */}
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
          <button
            type="button"
            onClick={goBack}
            disabled={!canBack}
            aria-label={t('directoryBrowser.back')}
            className={clsx(
              'flex size-7 items-center justify-center rounded-md border border-border-strong transition',
              canBack ? 'text-foreground hover:bg-foreground/[0.06]' : 'cursor-not-allowed text-muted opacity-50',
            )}
          >
            <ChevronLeft className="size-3.5" />
          </button>
          <button
            type="button"
            onClick={goForward}
            disabled={!canForward}
            aria-label={t('directoryBrowser.forward')}
            className={clsx(
              'flex size-7 items-center justify-center rounded-md border border-border-strong transition',
              canForward ? 'text-foreground hover:bg-foreground/[0.06]' : 'cursor-not-allowed text-muted opacity-50',
            )}
          >
            <ChevronRight className="size-3.5" />
          </button>

          {pathEditing ? (
            // Manual path input — replaces the breadcrumb for free-form
            // entry (paste a long path, use an absolute target outside
            // the usual breadcrumb chain, etc.). Esc reverts to the
            // breadcrumb without navigating.
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="flex items-center gap-1.5 rounded-md border border-cyan/40 bg-cyan/[0.06] px-2 py-1">
                <input
                  ref={pathInputRef}
                  type="text"
                  value={pathInput}
                  onChange={(e) => { pathEditRevision.current++; setPathInput(e.target.value); }}
                  onSelect={(event) => {
                    const { selectionStart, selectionEnd, selectionDirection } = event.currentTarget;
                    pathSelection.current = { start: selectionStart ?? 0, end: selectionEnd ?? 0, direction: selectionDirection ?? 'none' };
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      submitManualPath();
                    }
                    // Escape is handled by the window-level listener, which
                    // reverts path editing before it would close the picker.
                  }}
                  placeholder={t('directoryBrowser.editPathPlaceholder')}
                  className="flex-1 bg-transparent font-mono text-[11px] text-foreground outline-none placeholder:text-muted"
                />
                <button
                  type="button"
                  onClick={submitManualPath}
                  className="rounded px-2 py-0.5 text-[10px] font-semibold text-cyan-ink hover:bg-foreground/[0.04]"
                >
                  {t('directoryBrowser.editPathDone')}
                </button>
              </div>
              {pathError && <div className="px-1 text-[10.5px] text-destructive-ink">{pathError}</div>}
            </div>
          ) : (
            <div className="flex min-w-[100px] flex-1 items-center gap-1 overflow-x-auto rounded-md border border-border-strong bg-surface-2 px-2 py-1 font-mono text-[11px]">
              {breadcrumbs.map((crumb, i) => (
                <React.Fragment key={`${crumb.path}-${i}`}>
                  {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                  <button
                    type="button"
                    onClick={() => navigate(crumb.path)}
                    title={crumb.isHome ? homePath : undefined}
                    className={clsx(
                      'shrink-0 rounded px-1 py-0.5 transition hover:bg-foreground/[0.04]',
                      i === breadcrumbs.length - 1 ? 'font-semibold text-cyan-ink' : 'text-muted',
                    )}
                  >
                    {crumb.label}
                  </button>
                </React.Fragment>
              ))}
              {loading && <RefreshCw className="ml-auto size-3 shrink-0 animate-spin text-muted" />}
            </div>
          )}

          <button
            type="button"
            onClick={togglePathEditing}
            aria-label={t('directoryBrowser.editPath')}
            title={t('directoryBrowser.editPath')}
            className={clsx(
              'flex size-7 items-center justify-center rounded-md border transition',
              pathEditing
                ? 'border-cyan/40 bg-cyan/[0.08] text-cyan-ink'
                : 'border-border-strong text-muted hover:text-foreground',
            )}
          >
            <Keyboard className="size-3.5" />
          </button>

          <button
            type="button"
            onClick={toggleHidden}
            aria-pressed={showHidden}
            title={t('directoryBrowser.hiddenFiles')}
            className={clsx(
              'flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-semibold transition',
              showHidden
                ? 'border-cyan/40 bg-cyan/[0.08] text-cyan-ink'
                : 'border-border-strong text-muted hover:text-foreground',
            )}
          >
            {/* Stable label — on/off is conveyed by the Eye/EyeOff icon and the
                cyan (on) vs muted (off) treatment, not by changing the words. */}
            {showHidden ? <Eye className="size-3" /> : <EyeOff className="size-3" />}
            {t('directoryBrowser.hiddenFiles')}
          </button>

          <button
            type="button"
            onClick={() => {
              setCreating(true);
              setNewFolderName('');
              setCreateError(null);
            }}
            className="flex items-center gap-1.5 rounded-md border border-border-strong px-2.5 py-1.5 text-[11px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
          >
            <FolderPlus className="size-3" />
            {t('directoryBrowser.newFolder')}
          </button>
        </div>

        {/* Body — favorites sidebar + folder list */}
        <div className="flex min-h-0 flex-1">
          {/* Favorites sidebar */}
          <aside className="hidden w-[180px] shrink-0 flex-col gap-1 border-r border-border bg-surface-2 px-2 py-3 sm:flex">
            <div className="px-2 pb-1 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">
              {t('directoryBrowser.favorites')}
            </div>
            {favorites.map((fav) => {
              // Localized label for well-known shortcuts; the raw path for OS
              // roots (/tmp, /data, drives) so they read clearly.
              const label = FAVORITE_I18N[fav.key] ? t(FAVORITE_I18N[fav.key]) : fav.path;
              return (
                <button
                  key={fav.path}
                  type="button"
                  onClick={() => navigate(fav.path)}
                  title={fav.path}
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
                >
                  <span className="shrink-0 text-muted">{favoriteIcon(fav.key)}</span>
                  <span className="truncate">{label}</span>
                </button>
              );
            })}
          </aside>

          {/* Folder list */}
          <div className="flex min-w-0 flex-1 flex-col overflow-y-auto px-2 py-2">
            {error && (
              <div className="mx-2 mb-2 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-sm text-destructive-ink">
                {error}
              </div>
            )}

            {parent && (
              <button
                onClick={() => navigate(parent)}
                className="flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] text-muted transition hover:bg-foreground/[0.04]"
              >
                <ChevronLeft className="size-3.5" />
                <span className="font-mono">..</span>
              </button>
            )}

            {dirs.map((dir) => (
              <button
                key={dir.path}
                onClick={() => navigate(dir.path)}
                className="group flex items-center gap-2.5 rounded-md px-3 py-2 text-left text-[13px] text-foreground transition hover:bg-foreground/[0.04]"
              >
                <Folder className="size-4 shrink-0 text-gold-ink group-hover:hidden" />
                <FolderOpen className="hidden size-4 shrink-0 text-gold-ink group-hover:block" />
                <span className="truncate">{dir.name}</span>
              </button>
            ))}

            {!loading && !error && dirs.length === 0 && !creating && (
              <div className="px-3 py-6 text-center text-[12px] italic text-muted">
                {t('directoryBrowser.empty')}
              </div>
            )}

            {/* Inline new-folder prompt — sits at the bottom of the list so
                the user keeps context of which directory they're creating in */}
            {creating ? (
              <div className="mt-2 flex flex-col gap-1.5 rounded-md border border-dashed border-border-strong bg-foreground/[0.03] px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <FolderPlus className="size-4 shrink-0 text-mint-ink" />
                  <input
                    ref={newFolderInputRef}
                    value={newFolderName}
                    onChange={(e) => setNewFolderName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') submitNewFolder();
                    }}
                    placeholder={t('directoryBrowser.newFolderPlaceholder')}
                    className="flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setCreating(false);
                      setNewFolderName('');
                      setCreateError(null);
                    }}
                    className="rounded-md px-2 py-0.5 text-[11px] text-muted transition hover:text-foreground"
                  >
                    {t('directoryBrowser.cancel')}
                  </button>
                  <Button
                    type="button"
                    variant="default"
                    size={null}
                    onClick={submitNewFolder}
                    disabled={!newFolderName.trim()}
                    className="rounded-md px-2.5 py-0.5 text-[11px] font-semibold"
                  >
                    {t('directoryBrowser.newFolder')}
                  </Button>
                </div>
                {createError && <div className="pl-6 text-[11px] text-destructive-ink">{createError}</div>}
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="mt-2 flex items-center gap-2.5 rounded-md border border-dashed border-border-strong bg-foreground/[0.02] px-3 py-2 text-[12px] italic text-muted transition hover:bg-foreground/[0.04]"
              >
                <FolderPlus className="size-4 shrink-0" />
                {t('directoryBrowser.createHere')}
              </button>
            )}
          </div>
        </div>

        {/* Footer — current path + cancel + select */}
        <div className="flex items-center gap-3 border-t border-border bg-surface-2 px-4 py-3">
          <code className="flex-1 truncate rounded-md border border-border-strong bg-surface-3 px-2.5 py-1.5 font-mono text-[11px] text-foreground">
            {currentPath || '—'}
          </code>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border-strong px-4 py-1.5 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
          >
            {t('directoryBrowser.cancel')}
          </button>
          <Button
            type="button"
            variant="brand"
            size={null}
            onClick={() => canConfirm && onSelect(currentPath)}
            disabled={!canConfirm}
            className="gap-1.5 rounded-md px-4 py-1.5 text-[12px] font-semibold"
          >
            <Check className="size-3.5" />
            {t('directoryBrowser.select')}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};
