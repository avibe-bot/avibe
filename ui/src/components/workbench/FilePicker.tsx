import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight, File as FileIcon, Folder, FolderPlus, Loader2, X } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import {
  fileBrowserErrorMessage,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  pathCrumbs,
  systemFavorites,
  type Favorite,
  type FsEntry,
} from '../../lib/filesApi';
import { Button } from '../ui/button';

export type FilePickerMode = 'open-directory' | 'save-file';

function sortEntries(entries: FsEntry[]): FsEntry[] {
  return [...entries].sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
}

// An in-app file/folder picker that browses the agent machine's filesystem via /api/files. A
// browser can't open a native OS dialog that returns a *server-side* path, so this is the web
// equivalent of VS Code's file dialog: navigate folders, then pick a folder (open-directory) or a
// folder + filename (save-file). Name-only — no size/modified columns — so it stays a lean
// navigator and doesn't duplicate the Finder's display helpers. Rendered inside the owning
// (dark-locked) Editor window, so it inherits that theme and stays scoped to the window.
export const FilePicker: React.FC<{
  mode: FilePickerMode;
  /** Folder to start in; falls back to the first project, then the home favorite. */
  initialPath?: string | null;
  /** save-file: pre-filled filename. */
  defaultName?: string;
  onCancel: () => void;
  /**
   * open-directory → result = { path: <folder> }; save-file → { path: <folder>, name }.
   * Async: THROW to keep the dialog open and surface the message (e.g. the name already exists).
   */
  onConfirm: (result: { path: string; name?: string }) => Promise<void> | void;
}> = ({ mode, initialPath, defaultName, onCancel, onConfirm }) => {
  const { t } = useTranslation();
  const { projects } = useWorkbenchProjectsTree();
  const [cwd, setCwd] = useState('');
  const [entries, setEntries] = useState<FsEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sysFavs, setSysFavs] = useState<Favorite[]>([]);
  const [name, setName] = useState(defaultName ?? '');
  const [submitting, setSubmitting] = useState(false);
  // Inline new-folder input (rather than a window.prompt, matching the "no manual typing" intent).
  const [newFolder, setNewFolder] = useState<string | null>(null);
  // Hidden entries are off by default but toggleable, so dot-dirs (.config, .git, …) stay reachable
  // — the picker is the only way to navigate now (there's no manual path field).
  const [showHidden, setShowHidden] = useState(false);

  const navSeq = useRef(0);
  const navigate = useCallback(
    (path: string) => {
      const seq = ++navSeq.current;
      setLoading(true);
      setError(null);
      setNewFolder(null);
      listDir(path, showHidden)
        .then((r) => {
          if (seq !== navSeq.current) return;
          setCwd(r.path);
          setEntries(sortEntries(r.entries));
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

  // Pick a sensible starting folder once: the caller's initial path, else the first project, else
  // the home favorite.
  useEffect(() => {
    let cancelled = false;
    if (initialPath) {
      navigate(initialPath);
      return;
    }
    systemFavorites()
      .then((favs) => {
        if (cancelled) return;
        setSysFavs(favs);
        const start = projects?.[0]?.folder_path || favs.find((f) => f.key === 'home')?.path;
        if (start) navigate(start);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Favorites for the rail (loaded above when there's no initialPath; fetch here otherwise).
  useEffect(() => {
    if (sysFavs.length === 0) systemFavorites().then(setSysFavs).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // Re-list the current folder when the hidden toggle flips.
  useEffect(() => {
    if (cwd) navigate(cwd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showHidden]);

  const crumbs = cwd ? pathCrumbs(cwd) : [];
  const projectFavs = (projects || []).filter((p) => !!p.folder_path).map((p) => ({ label: p.display_name, path: p.folder_path as string }));

  const createFolder = async (folderName: string) => {
    const trimmed = folderName.trim();
    if (!trimmed) {
      setNewFolder(null);
      return;
    }
    if (!isPlainEntryName(trimmed)) {
      setError(t('apps.fileBrowser.errors.invalid_name'));
      return;
    }
    try {
      await makeDir(joinPath(cwd, trimmed));
      setNewFolder(null);
      navigate(cwd);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.createFolderFailed')));
    }
  };

  const runConfirm = async (result: { path: string; name?: string }) => {
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(result);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
    } finally {
      setSubmitting(false);
    }
  };

  const confirm = () => {
    if (!cwd || submitting) return;
    if (mode === 'open-directory') {
      void runConfirm({ path: cwd });
      return;
    }
    const trimmed = name.trim();
    if (!trimmed) return;
    if (!isPlainEntryName(trimmed)) {
      setError(t('apps.fileBrowser.errors.invalid_name'));
      return;
    }
    void runConfirm({ path: cwd, name: trimmed });
  };

  const canConfirm = !!cwd && !submitting && (mode === 'open-directory' || name.trim() !== '');

  return (
    // Scoped to the owning window (absolute, not a body portal) so it inherits the window's theme.
    <div className="absolute inset-0 z-30 grid place-items-center bg-black/50 p-5" onClick={onCancel} role="presentation">
      <div
        className="flex h-[420px] w-[600px] max-w-full max-h-full flex-col overflow-hidden rounded-xl border border-border-strong bg-surface-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.7)]"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        {/* Title + close */}
        <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5">
          <span className="flex-1 truncate text-[13px] font-semibold text-foreground">
            {t(mode === 'open-directory' ? 'apps.picker.openFolderTitle' : 'apps.picker.saveFileTitle')}
          </span>
          <Button type="button" size="icon" variant="ghost" className="size-7 text-muted" aria-label={t('common.close')} onClick={onCancel}>
            <X className="size-4" />
          </Button>
        </div>

        {/* Breadcrumb + New Folder */}
        <div className="flex items-center gap-2 border-b border-border bg-surface-2/60 px-3 py-1.5">
          <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
            {crumbs.map((c, i) => (
              <span key={c.path} className="flex shrink-0 items-center">
                {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                <button
                  type="button"
                  onClick={() => navigate(c.path)}
                  className="max-w-[150px] truncate rounded px-1.5 py-0.5 text-[12px] text-muted transition hover:bg-foreground/[0.06] hover:text-foreground"
                >
                  {c.label}
                </button>
              </span>
            ))}
          </div>
          <label className="flex shrink-0 items-center gap-1 text-[11px] text-muted" title={t('apps.fileBrowser.showHidden')}>
            <input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} className="size-3" />
            {t('apps.fileBrowser.showHidden')}
          </label>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-7 shrink-0 gap-1.5 px-2 text-[12px] text-muted"
            disabled={!cwd}
            onClick={() => setNewFolder('')}
          >
            <FolderPlus className="size-3.5" /> {t('apps.picker.newFolder')}
          </Button>
        </div>

        {error && <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive">{error}</div>}

        <div className="flex min-h-0 flex-1 overflow-hidden">
          {/* Rail: favorites + projects */}
          <aside className="hidden w-[150px] shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-surface-2/40 p-2 sm:flex">
            {sysFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.favorites')}</RailTitle>}
            {sysFavs.map((f) => (
              <RailRow key={f.path} label={f.path.split('/').filter(Boolean).pop() || f.path} active={cwd === f.path} onClick={() => navigate(f.path)} />
            ))}
            {projectFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.projects')}</RailTitle>}
            {projectFavs.map((f) => (
              <RailRow key={f.path} label={f.label} active={cwd === f.path} onClick={() => navigate(f.path)} />
            ))}
          </aside>

          {/* Listing — folders navigate on click; files are shown (dimmed) for context only. */}
          <div className="min-h-0 flex-1 overflow-y-auto p-1">
            {newFolder !== null && (
              <input
                autoFocus
                value={newFolder}
                onChange={(e) => setNewFolder(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void createFolder(newFolder);
                  else if (e.key === 'Escape') setNewFolder(null);
                }}
                // Blur abandons the inline input — only Enter creates the folder, so cancelling the
                // picker (which blurs the input first) can't silently makeDir a folder.
                onBlur={() => setNewFolder(null)}
                placeholder={t('apps.picker.newFolderName')}
                className="mb-1 w-full rounded-md border border-cyan/50 bg-surface px-2 py-1.5 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
              />
            )}
            {loading && entries === null && (
              <div className="grid place-items-center py-8">
                <Loader2 className="size-4 animate-spin text-muted" />
              </div>
            )}
            {entries && entries.length === 0 && newFolder === null && (
              <div className="px-3 py-8 text-center text-[12px] text-muted">{t('apps.fileBrowser.empty')}</div>
            )}
            {entries?.map((e) => {
              const isDir = e.kind === 'dir';
              return (
                <button
                  key={e.name}
                  type="button"
                  disabled={!isDir}
                  onClick={() => isDir && navigate(joinPath(cwd, e.name))}
                  className={clsx(
                    'flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-[12.5px] transition',
                    isDir ? 'text-foreground hover:bg-foreground/[0.06]' : 'cursor-default text-muted/60',
                  )}
                >
                  {isDir ? <Folder className="size-4 shrink-0 text-cyan" /> : <FileIcon className="size-4 shrink-0 text-muted" />}
                  <span className="truncate">{e.name}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Footer: (save) filename input + Cancel / Confirm */}
        <div className="flex items-center gap-2 border-t border-border px-3.5 py-2.5">
          {mode === 'save-file' && (
            <input
              // Autofocus so ⌘S-from-Monaco lands here, not behind the modal in the editor buffer.
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') confirm();
              }}
              placeholder={t('apps.picker.fileName')}
              className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2.5 py-1.5 font-mono text-[12.5px] text-foreground placeholder:text-muted focus:border-cyan/60 focus:outline-none"
            />
          )}
          {mode === 'open-directory' && <span className="flex-1" />}
          <Button type="button" size="sm" variant="ghost" className="h-8 px-3 text-[12.5px]" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button type="button" size="sm" variant="brand" className="h-8 gap-1.5 px-3.5 text-[12.5px]" disabled={!canConfirm} onClick={confirm}>
            {submitting && <Loader2 className="size-3.5 animate-spin" />}
            {t(mode === 'open-directory' ? 'apps.picker.selectFolder' : 'apps.picker.save')}
          </Button>
        </div>
      </div>
    </div>
  );
};

const RailTitle: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="px-1 pb-0.5 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">{children}</div>
);

const RailRow: React.FC<{ label: string; active: boolean; onClick: () => void }> = ({ label, active, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12px] transition',
      active ? 'bg-cyan-soft text-foreground' : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
    )}
  >
    <Folder className="size-3.5 shrink-0 text-muted" />
    <span className="truncate">{label}</span>
  </button>
);
