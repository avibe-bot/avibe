import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CodeXml } from 'lucide-react';
import clsx from 'clsx';

import { useUnsavedChanges } from '../../context/useUnsavedChanges';
import { useStandaloneAppTab } from '../../context/StandaloneAppTabContext';
import { isDesktopViewport } from '../../lib/useIsDesktop';
import { MobileAppHeader } from '../apps/MobileAppHeader';

// The Editor app as a full-page route (sibling of /apps/files and /apps/terminal). The same IDE is
// used on desktop and phones so explorer, search, tabs, recent files, and save-as do not disappear
// on the smaller surface. The mobile shell starts with the explorer collapsed to leave the editor
// readable, and the activity bar can reopen it when needed. Design: `dnYPx` (IDE) + `w0qoC` (welcome).
const EditorApp = lazy(() => import('./EditorApp').then((m) => ({ default: m.EditorApp })));

// A file handed to the editor when navigating in from the File Browser (mobile) or a direct link.
// Carried in router state — like the window params `wm.openApp` passes — so absolute paths stay out
// of the URL; a refresh (no state) just lands on the empty/welcome state.
type LaunchFile = {
  path: string;
  filename: string;
  mtime: number | null;
  line?: number;
  column?: number;
  endColumn?: number;
};

function readLaunch(state: unknown): LaunchFile | null {
  if (!state || typeof state !== 'object') return null;
  const s = state as Record<string, unknown>;
  if (typeof s.path !== 'string') return null;
  const line = typeof s.line === 'number' && Number.isInteger(s.line) && s.line > 0 ? s.line : undefined;
  const column = typeof s.column === 'number' && Number.isInteger(s.column) && s.column >= 0 ? s.column : 0;
  const endColumn = typeof s.endColumn === 'number' && Number.isInteger(s.endColumn) && s.endColumn >= column
    ? s.endColumn
    : column;
  return {
    path: s.path,
    filename: typeof s.filename === 'string' ? s.filename : s.path.split('/').filter(Boolean).pop() || s.path,
    mtime: typeof s.mtime === 'number' ? s.mtime : null,
    ...(line ? { line, column, endColumn } : {}),
  };
}

// Pick the surface ONCE at mount, deliberately NOT reactive: swapping between the mobile pane and the
// desktop IDE on a mid-edit resize/rotate would unmount whichever holds the buffer and silently drop
// unsaved edits. A phone that rotates keeps the surface it opened with.
function useDesktopAtMount(): boolean {
  return useState(isDesktopViewport)[0];
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
  const standalone = useStandaloneAppTab();
  // The shell drops its chrome for standalone tabs. Ordinary phone routes also
  // fill the viewport and render a compact app-local header instead.
  const mobileRoute = !desktop && !standalone;
  const fullBleed = standalone || !desktop;
  useUnsavedChanges(dirty ? t('apps.editor.confirmDiscardSwitch') : null);
  useUnloadWarning(dirty);

  return (
    <div
      className={
        fullBleed
          ? 'flex h-full w-full flex-col bg-surface pb-[env(safe-area-inset-bottom)]'
          : 'flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]'
      }
    >
      {mobileRoute && <MobileAppHeader title={t('apps.editor.label')} icon={CodeXml} />}
      {!fullBleed && (
        <div>
          <h1 className="text-[18px] font-semibold text-foreground">{t('apps.editor.label')}</h1>
          <p className="text-[12px] text-muted">{t('apps.editor.tagline')}</p>
        </div>
      )}
      <EditorSurface launch={launch} onDirtyChange={setDirty} fullBleed={fullBleed} mobile={!desktop} />
    </div>
  );
};

// Full Editor IDE, forced dark like its Dock window (data-theme re-cascades the dark token set to
// this subtree). No windowId, so the window-only niceties (title, close guard, ⌘O/⌘N) stay inert;
// open/edit/save all work full-page. `useWindowCloseGuard` is a no-op without a window, so the route
// page owns its navigation and unload guards.
const EditorSurface: React.FC<{
  launch: LaunchFile | null;
  onDirtyChange: (dirty: boolean) => void;
  fullBleed?: boolean;
  mobile?: boolean;
}> = ({ launch, onDirtyChange, fullBleed = false, mobile = false }) => {
  return (
    <div
      data-theme="dark"
      className={clsx('flex min-h-0 flex-1 overflow-hidden bg-surface', !fullBleed && 'rounded-xl border border-border')}
    >
      <Suspense fallback={<PaneLoading />}>
        <EditorApp
          onDirtyChange={onDirtyChange}
          params={launch ?? undefined}
          mobile={mobile}
        />
      </Suspense>
    </div>
  );
};
