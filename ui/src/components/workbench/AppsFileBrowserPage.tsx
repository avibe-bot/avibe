import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Download, File as FileIcon, Folder, FolderPlus, Loader2, RefreshCw, Star } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';
import { previewKind } from '../../lib/filePreview';
import {
  contentUrl,
  fileBrowserErrorMessage,
  fileMeta,
  joinPath,
  listDir,
  makeDir,
  pathCrumbs,
  systemFavorites,
  type Favorite,
  type FsEntry,
  type FsListing,
} from '../../lib/filesApi';
import { Button } from '../ui/button';

// Lazy-load the editor so CodeMirror + @codemirror/language-data stay out of the
// main bundle until a text file is actually opened.
const FileEditorPane = lazy(() => import('./FileEditorPane').then((m) => ({ default: m.FileEditorPane })));

type Selected = { path: string; name: string; kind: string; mime: string | null; mtime: number | null; size: number | null };

// Above this size, offer a download instead of loading the file into CodeMirror
// (mirrors the in-page preview cap; the backend allows /api/files/content up to 25MB).
const MAX_EDIT_BYTES = 1024 * 1024;

// Whole-machine Finder: favorites rail (pinned projects + OS defaults), a
// breadcrumb + dir/file list (left), and a content pane (right) that views or
// edits the selected file. Backend contract: src/lib/filesApi.ts → /api/files/*.
export const AppsFileBrowserPage: React.FC = () => {
  const { t } = useTranslation();
  const { projects } = useWorkbenchProjectsTree();
  const [cwd, setCwd] = useState('');
  const [listing, setListing] = useState<FsListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [sysFavs, setSysFavs] = useState<Favorite[]>([]);
  const [selected, setSelected] = useState<Selected | null>(null);

  const navigate = useCallback(
    (path: string) => {
      setLoading(true);
      setError(null);
      listDir(path, showHidden)
        .then((r) => {
          setCwd(r.path);
          setListing(r);
        })
        .catch((e: unknown) => setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.listFailed'))))
        .finally(() => setLoading(false));
    },
    [showHidden],
  );

  useEffect(() => {
    systemFavorites().then(setSysFavs).catch(() => {});
  }, []);

  // Pick an initial directory once: first pinned project, else the home favorite.
  useEffect(() => {
    if (cwd) return;
    if (projects === null) return;
    const initial = projects?.[0]?.folder_path || sysFavs.find((f) => f.key === 'home')?.path;
    if (initial) navigate(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects, sysFavs]);

  // Re-list the current dir when the hidden toggle flips.
  useEffect(() => {
    if (cwd) navigate(cwd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showHidden]);

  const openEntry = (entry: FsEntry) => {
    const full = joinPath(cwd, entry.name);
    if (entry.kind === 'dir') {
      setSelected(null);
      navigate(full);
    } else {
      fileMeta(full)
        .then((m) => setSelected({ path: full, name: entry.name, kind: m.kind, mime: m.mime, mtime: m.mtime, size: m.size }))
        .catch((e: unknown) => setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.openFailed'))));
    }
  };

  const newFolder = async () => {
    const name = window.prompt(t('apps.fileBrowser.newFolderPrompt'));
    if (!name) return;
    try {
      await makeDir(joinPath(cwd, name));
      navigate(cwd);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.createFolderFailed')));
    }
  };

  const projectFavs = useMemo(
    () =>
      (projects || [])
        .filter((p) => !!p.folder_path)
        .map((p) => ({ label: p.display_name, path: p.folder_path as string })),
    [projects],
  );
  const crumbs = cwd ? pathCrumbs(cwd) : [];

  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.fileBrowser.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.fileBrowser.tagline')}</p>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">
        {/* Left: breadcrumb toolbar + favorites + listing */}
        <div className="flex w-full min-w-0 flex-col md:w-[320px] md:border-r md:border-border">
          <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-2">
            <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
              {crumbs.map((c, i) => (
                <span key={c.path} className="flex shrink-0 items-center">
                  {i > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                  <button
                    type="button"
                    onClick={() => {
                      setSelected(null);
                      navigate(c.path);
                    }}
                    className="max-w-[120px] truncate rounded px-1 py-0.5 text-[12px] text-muted transition hover:bg-foreground/[0.06] hover:text-foreground"
                  >
                    {c.label}
                  </button>
                </span>
              ))}
            </div>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0 text-muted"
              aria-label={t('apps.fileBrowser.refresh')}
              onClick={() => cwd && navigate(cwd)}
            >
              <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            </Button>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0 text-muted"
              aria-label={t('apps.fileBrowser.newFolder')}
              disabled={!cwd}
              onClick={() => void newFolder()}
            >
              <FolderPlus className="size-3.5" />
            </Button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="flex flex-col gap-0.5 border-b border-border p-2">
              {projectFavs.length > 0 && (
                <div className="px-1 pb-0.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">
                  {t('apps.fileBrowser.projects')}
                </div>
              )}
              {projectFavs.map((f) => (
                <FavRow
                  key={f.path}
                  icon={<Star className="size-3.5 text-mint" />}
                  label={f.label}
                  active={cwd === f.path}
                  onClick={() => {
                    setSelected(null);
                    navigate(f.path);
                  }}
                />
              ))}
              {sysFavs.length > 0 && (
                <div className="px-1 pb-0.5 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">
                  {t('apps.fileBrowser.system')}
                </div>
              )}
              {sysFavs.map((f) => (
                <FavRow
                  key={f.path}
                  icon={<Folder className="size-3.5 text-muted" />}
                  label={f.path.split('/').filter(Boolean).pop() || f.path}
                  active={cwd === f.path}
                  onClick={() => {
                    setSelected(null);
                    navigate(f.path);
                  }}
                />
              ))}
            </div>

            {error && (
              <div className="m-2 rounded-md border border-destructive/40 bg-destructive/[0.06] px-2.5 py-1.5 text-[11.5px] text-destructive">
                {error}
              </div>
            )}
            <div className="flex flex-col gap-0.5 p-2">
              {loading && !listing && (
                <div className="grid place-items-center py-6">
                  <Loader2 className="size-4 animate-spin text-muted" />
                </div>
              )}
              {listing?.entries.length === 0 && !loading && (
                <div className="px-2 py-6 text-center text-[12px] text-muted">{t('apps.fileBrowser.empty')}</div>
              )}
              {listing?.entries.map((entry) => (
                <button
                  key={entry.name}
                  type="button"
                  onClick={() => openEntry(entry)}
                  className={clsx(
                    'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
                    selected?.path === joinPath(cwd, entry.name)
                      ? 'bg-mint/[0.08] text-foreground'
                      : 'text-foreground hover:bg-foreground/[0.04]',
                  )}
                >
                  {entry.kind === 'dir' ? (
                    <Folder className="size-4 shrink-0 text-muted" />
                  ) : (
                    <FileIcon className="size-4 shrink-0 text-muted" />
                  )}
                  <span className="flex-1 truncate">{entry.name}</span>
                  {entry.kind === 'dir' && <ChevronRight className="size-3.5 shrink-0 text-muted" />}
                </button>
              ))}
            </div>
          </div>

          <label className="flex items-center gap-2 border-t border-border px-3 py-2 text-[11.5px] text-muted">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
              className="size-3.5"
            />
            {t('apps.fileBrowser.showHidden')}
          </label>
        </div>

        {/* Right: content pane (desktop) */}
        <div className="hidden min-w-0 flex-1 md:flex">
          <ContentPane selected={selected} />
        </div>
      </div>

      {/* Mobile: content opens below the list when a file is selected */}
      {selected && (
        <div className="flex min-h-[50vh] flex-col overflow-hidden rounded-xl border border-border bg-surface md:hidden">
          <ContentPane selected={selected} />
        </div>
      )}
    </div>
  );
};

const FavRow: React.FC<{ icon: React.ReactNode; label: string; active: boolean; onClick: () => void }> = ({
  icon,
  label,
  active,
  onClick,
}) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
      active ? 'bg-mint/[0.08] text-foreground' : 'text-foreground hover:bg-foreground/[0.04]',
    )}
  >
    <span className="shrink-0">{icon}</span>
    <span className="flex-1 truncate">{label}</span>
  </button>
);

const ContentPane: React.FC<{ selected: Selected | null }> = ({ selected }) => {
  const { t } = useTranslation();
  if (!selected) {
    return (
      <div className="grid flex-1 place-items-center p-6 text-center text-[12.5px] text-muted">
        {t('apps.fileBrowser.selectHint')}
      </div>
    );
  }
  const mime = selected.mime || '';
  if (mime.startsWith('image/')) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center overflow-auto bg-foreground/[0.02] p-4">
        <img src={contentUrl(selected.path)} alt={selected.name} className="max-h-full max-w-full object-contain" />
      </div>
    );
  }
  // A symlink reports size:null (server lstat()s the link, not its target), but
  // /api/files/content follows it — so a symlink pointing at a large file would slip past
  // the size cap and load its whole target into CodeMirror. Treat symlinks and any
  // unknown-size entry as non-editable; the download link below can still follow them.
  const tooLargeToEdit =
    selected.kind === 'symlink' || selected.size == null || selected.size > MAX_EDIT_BYTES;
  if (previewKind(selected.name, selected.mime || undefined) && !tooLargeToEdit) {
    return (
      <Suspense
        fallback={<div className="grid flex-1 place-items-center text-[12px] text-muted">{t('common.loading')}</div>}
      >
        <FileEditorPane path={selected.path} filename={selected.name} mtime={selected.mtime} />
      </Suspense>
    );
  }
  return (
    <div className="grid flex-1 place-items-center p-6 text-center">
      <div className="flex flex-col items-center gap-3">
        <FileIcon className="size-8 text-muted" />
        <div className="text-[12.5px] text-muted">
          {tooLargeToEdit ? t('apps.fileBrowser.tooLarge') : t('apps.fileBrowser.noPreview')}
        </div>
        <a
          href={contentUrl(selected.path, true)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border-strong px-3 py-1.5 text-[12px] text-foreground transition hover:bg-foreground/[0.04]"
        >
          <Download className="size-3.5" /> {t('apps.fileBrowser.download')}
        </a>
      </div>
    </div>
  );
};
