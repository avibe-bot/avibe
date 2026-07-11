import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CodeXml, FolderOpen } from 'lucide-react';

import { Button } from '../ui/button';
import { useUnsavedChanges } from '../../context/useUnsavedChanges';
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

// Pick the surface ONCE at mount, deliberately NOT reactive: swapping between the mobile pane and the
// desktop IDE on a mid-edit resize/rotate would unmount whichever holds the buffer and silently drop
// unsaved edits. A phone that rotates keeps the surface it opened with.
function useDesktopAtMount(): boolean {
  return useState(() => window.matchMedia(DESKTOP_QUERY).matches)[0];
}

// Warn before a hard unload (refresh / tab close / leaving the SPA) while there are unsaved edits.
// React Router's blocker handles in-app navigation separately.
function useUnloadWarning(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [active]);
}

const PaneLoading: React.FC = () => {
  const { t } = useTranslation();
  return <div className="grid min-h-0 flex-1 place-items-center text-[12px] text-muted">{t('common.loading')}</div>;
};

export const AppsEditorPage: React.FC = () => {
  const { t } = useTranslation();
  const location = useLocation();
  const desktop = useDesktopAtMount();
  const [dirty, setDirty] = useState(false);
  // Re-read whenever the router state changes (each navigation carries a fresh state object) so
  // opening another file while already on this route swaps the launch target.
  const launch = useMemo(() => readLaunch(location.state), [location.state]);
  useUnsavedChanges(dirty ? t('apps.editor.confirmDiscardSwitch') : null);
  useUnloadWarning(dirty);

  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.editor.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.editor.tagline')}</p>
      </div>
      {desktop ? (
        <DesktopEditor launch={launch} onDirtyChange={setDirty} />
      ) : (
        <MobileEditor launch={launch} onDirtyChange={setDirty} />
      )}
    </div>
  );
};

// Desktop / tablet: the full Editor IDE, forced dark like its Dock window (data-theme re-cascades the
// dark token set to this subtree). No windowId, so the window-only niceties (title, close guard,
// ⌘O/⌘N) stay inert; open/edit/save all work full-page. `useWindowCloseGuard` is a no-op without a
// window, so the route page owns its navigation and unload guards.
const DesktopEditor: React.FC<{
  launch: LaunchFile | null;
  onDirtyChange: (dirty: boolean) => void;
}> = ({ launch, onDirtyChange }) => {
  return (
    <div data-theme="dark" className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">
      <Suspense fallback={<PaneLoading />}>
        <EditorApp
          onDirtyChange={onDirtyChange}
          params={launch ? { path: launch.path, filename: launch.filename, mtime: launch.mtime } : undefined}
        />
      </Suspense>
    </div>
  );
};

// Phone single-file editor: one file at a time (no activity bar / explorer). FileEditorPane already
// renders the filename + dirty dot + Save header and the Monaco touch accessory bar; opening/switching
// a file reuses the File Browser (the mobile file-picking surface, which owns the editable-vs-download
// decision). The name-only launch has no live cursor/search — that richness stays on the desktop IDE.
const MobileEditor: React.FC<{
  launch: LaunchFile | null;
  onDirtyChange: (dirty: boolean) => void;
}> = ({ launch, onDirtyChange }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [file, setFile] = useState<LaunchFile | null>(launch);

  // A fresh navigation from Files swaps the open file. The router-wide blocker already confirmed
  // before a dirty editor could leave this page to pick another.
  useEffect(() => {
    if (launch) {
      setFile(launch);
      onDirtyChange(false);
    }
  }, [launch, onDirtyChange]);

  // This imperative navigation uses the same router-level blocker as links and browser Back.
  const openAnother = () => navigate('/apps/files');

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-surface">
      {file ? (
        // Key by path so switching to a different file remounts the pane and reads it fresh —
        // FileEditorPane treats a live path change as a rename and skips the reread otherwise, which
        // would show the previous file's buffer under the new name.
        <FileEditorPane
          key={file.path}
          path={file.path}
          filename={file.filename}
          mtime={file.mtime}
          onOpenFile={openAnother}
          onDirtyChange={onDirtyChange}
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
