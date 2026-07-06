import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CodeXml, FolderOpen } from 'lucide-react';

import { Button } from '../ui/button';
import { FileEditorPane } from './FileEditorPane';

// The Editor app as a full-page route (sibling of /apps/files and /apps/terminal). On desktop it
// mounts the same full Editor IDE the Dock window uses; on phones — where there is no window layer —
// it renders a slim single-file editor. Design: `dnYPx` (IDE) + `w0qoC` (welcome).
const EditorApp = lazy(() => import('./EditorApp').then((m) => ({ default: m.EditorApp })));

// The desktop IDE (dnYPx) is designed dark and forces Monaco dark; below this the phone gets the
// slim editor instead. Matches the File Browser's window-vs-page breakpoint so a tablet (≥768) gets
// the same full IDE it gets a resizable window for.
const DESKTOP_QUERY = '(min-width: 768px)';

// A file handed to the editor when navigating in from the File Browser (mobile) or a direct link.
// Carried in router state — like the window params `wm.openApp` passes — so absolute paths stay out
// of the URL; a refresh (no state) just lands on the empty/welcome state.
type LaunchFile = { path: string; filename: string; mtime: number | null };

function readLaunch(state: unknown): LaunchFile | null {
  if (!state || typeof state !== 'object') return null;
  const s = state as Record<string, unknown>;
  if (typeof s.path !== 'string') return null;
  return {
    path: s.path,
    filename: typeof s.filename === 'string' ? s.filename : s.path.split('/').filter(Boolean).pop() || s.path,
    mtime: typeof s.mtime === 'number' ? s.mtime : null,
  };
}

// Live desktop/phone flag, re-evaluated on resize/rotate so crossing the breakpoint re-picks the
// right surface (mirrors ThemeContext's system-theme media subscription).
function useDesktop(): boolean {
  const [desktop, setDesktop] = useState(() => window.matchMedia(DESKTOP_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => setDesktop(mq.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return desktop;
}

const PaneLoading: React.FC = () => {
  const { t } = useTranslation();
  return <div className="grid min-h-0 flex-1 place-items-center text-[12px] text-muted">{t('common.loading')}</div>;
};

export const AppsEditorPage: React.FC = () => {
  const { t } = useTranslation();
  const location = useLocation();
  const desktop = useDesktop();
  // Re-read whenever the router state changes (each navigation carries a fresh state object) so
  // opening another file while already on this route swaps the launch target.
  const launch = useMemo(() => readLaunch(location.state), [location.state]);

  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.editor.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.editor.tagline')}</p>
      </div>
      {desktop ? (
        // Full Editor IDE, forced dark like its Dock window (data-theme re-cascades the dark token
        // set to this subtree). No windowId: the window-only niceties (title, close guard, ⌘O/⌘N)
        // stay inert, but open/edit/save all work full-page.
        <div data-theme="dark" className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">
          <Suspense fallback={<PaneLoading />}>
            <EditorApp params={launch ? { path: launch.path, filename: launch.filename, mtime: launch.mtime } : undefined} />
          </Suspense>
        </div>
      ) : (
        <MobileEditor launch={launch} />
      )}
    </div>
  );
};

// Phone single-file editor: one file at a time (no activity bar / explorer). FileEditorPane already
// renders the filename + dirty dot + Save header and the Monaco touch accessory bar; opening/switching
// a file reuses the File Browser (the mobile file-picking surface), which owns the editable-vs-download
// decision. The name-only launch has no live cursor/search — that richness stays on the desktop IDE.
const MobileEditor: React.FC<{ launch: LaunchFile | null }> = ({ launch }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [file, setFile] = useState<LaunchFile | null>(launch);
  const [dirty, setDirty] = useState(false);

  // A fresh navigation from Files swaps the open file. Edits to a previous file were already
  // discarded when the user left this page to pick another (guarded in openAnother).
  useEffect(() => {
    if (launch) {
      setFile(launch);
      setDirty(false);
    }
  }, [launch]);

  // Open / switch a file via the File Browser. Confirm first when the current buffer is dirty, since
  // leaving unmounts this pane and drops the unsaved edits.
  const openAnother = () => {
    if (dirty && !window.confirm(t('apps.editor.confirmDiscardSwitch'))) return;
    navigate('/apps/files');
  };

  // Guard a hard unload (refresh / close / navigating out of the SPA) while there are unsaved edits.
  // In-app tab-bar navigation can't be blocked here (BrowserRouter has no navigation blocker); the
  // header's open button is the guarded in-app switch path.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-surface">
      {file ? (
        <FileEditorPane
          path={file.path}
          filename={file.filename}
          mtime={file.mtime}
          onOpenFile={openAnother}
          onDirtyChange={setDirty}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
          <span className="grid size-12 place-items-center rounded-2xl border border-violet/50 bg-violet/[0.1]">
            <CodeXml className="size-6 text-violet" />
          </span>
          <div className="flex flex-col gap-1">
            <div className="text-[15px] font-semibold text-foreground">{t('apps.editor.empty')}</div>
            <p className="max-w-[260px] text-[12.5px] text-muted">{t('apps.editor.emptyHint')}</p>
          </div>
          <Button type="button" variant="brand" size="sm" className="gap-1.5" onClick={() => navigate('/apps/files')}>
            <FolderOpen className="size-4" /> {t('apps.editor.browseFiles')}
          </Button>
        </div>
      )}
    </div>
  );
};
