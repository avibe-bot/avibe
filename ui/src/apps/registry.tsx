import { Suspense, lazy } from 'react';
import { CodeXml, Eye, Folder, MonitorPlay, SquareTerminal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';

import { showPagePrivatePath } from './showPageAvatar';

// The catalogue of windowed apps. The WindowManager + Dock are headless of any
// specific app; everything app-specific (title, icon, default window size, body)
// lives here so adding an app is one registry entry. App bodies are lazy-loaded
// so opening the workbench doesn't pull file-browser / xterm code into the main
// bundle — each loads only when its window first opens.

export type AppId = 'files' | 'terminal' | 'editor' | 'preview' | 'showpage';

export interface AppDefinition {
  id: AppId;
  /** i18n key for the window title / Dock label. */
  titleKey: string;
  icon: LucideIcon;
  /** Tint for the Dock tile + window title icon — a CSS var token name. */
  accent: string;
  defaultSize: { width: number; height: number };
  /** The window body. Receives the owning window id and its launch params. */
  Component: React.FC<{ windowId: string; params?: Record<string, unknown> }>;
  /**
   * Lock this app's window to a fixed theme regardless of the global light/dark. The Editor and
   * Terminal stay dark (a code editor and a shell are conventionally dark); the File Browser omits
   * this and follows the workbench theme.
   */
  lockTheme?: 'dark';
  /**
   * When set, the window title bar shows an "open in new tab" button that opens this URL — the app
   * has a standalone browser surface (a Show Page's own `/show/<id>/`). Returns undefined when the
   * params can't resolve one. Only `showpage` defines this in v1.
   */
  externalHref?: (params?: Record<string, unknown>) => string | undefined;
}

const Loading: React.FC = () => {
  const { t } = useTranslation();
  return <div className="grid h-full w-full place-items-center bg-surface text-[12px] text-muted">{t('common.loading')}</div>;
};

const FilesBody = lazy(() =>
  import('../components/workbench/AppsFileBrowserPage').then((m) => ({ default: m.AppsFileBrowserPage })),
);
const TerminalBody = lazy(() =>
  import('../components/workbench/AppsTerminalPage').then((m) => ({ default: m.AppsTerminalPage })),
);
const EditorBody = lazy(() =>
  import('../components/workbench/EditorApp').then((m) => ({ default: m.EditorApp })),
);
const PreviewBody = lazy(() =>
  import('../components/workbench/AppsPreviewPage').then((m) => ({ default: m.AppsPreviewPage })),
);
const ShowPageBody = lazy(() => import('./ShowPageApp').then((m) => ({ default: m.ShowPageApp })));

export const APP_REGISTRY: Record<AppId, AppDefinition> = {
  files: {
    id: 'files',
    titleKey: 'apps.fileBrowser.label',
    icon: Folder,
    accent: '--cyan',
    defaultSize: { width: 920, height: 600 },
    Component: ({ windowId }) => (
      <Suspense fallback={<Loading />}>
        <FilesBody windowed windowId={windowId} />
      </Suspense>
    ),
  },
  terminal: {
    id: 'terminal',
    titleKey: 'apps.terminal.label',
    icon: SquareTerminal,
    accent: '--mint',
    defaultSize: { width: 820, height: 540 },
    lockTheme: 'dark',
    Component: ({ windowId, params }) => (
      <Suspense fallback={<Loading />}>
        {/* Each windowed terminal takes a bounded, reused session slot internally so
            it gets its own backend session without leaking ids (see AppsTerminalPage).
            windowId + params thread through so the tab layout persists across reloads. */}
        <TerminalBody windowed windowId={windowId} params={params} />
      </Suspense>
    ),
  },
  editor: {
    id: 'editor',
    titleKey: 'apps.editor.label',
    icon: CodeXml,
    accent: '--violet',
    defaultSize: { width: 1000, height: 640 },
    lockTheme: 'dark',
    Component: ({ windowId, params }) => (
      <Suspense fallback={<Loading />}>
        <EditorBody windowId={windowId} params={params} />
      </Suspense>
    ),
  },
  // On-demand viewer opened by the File Browser (double-click an image/PDF/Office/Markdown file).
  // Deliberately NOT in APP_LIST, so it has no permanent Dock tile — it only appears while a preview
  // window is open (and in the Dock's minimized strip if minimized). No lockTheme: images and docs
  // should render on the workbench's own light/dark, not be forced dark.
  preview: {
    id: 'preview',
    titleKey: 'apps.preview.label',
    icon: Eye,
    accent: '--gold',
    defaultSize: { width: 900, height: 640 },
    Component: ({ windowId, params }) => (
      <Suspense fallback={<Loading />}>
        <PreviewBody windowId={windowId} params={params} />
      </Suspense>
    ),
  },
  // A pinned session Show Page opened as an app. Like `preview` it is NOT in APP_LIST — it has no
  // permanent launcher tile; a Dock tile appears only while the page is pinned (see DockContext /
  // Dock.tsx), and the window is param-driven by { sessionId, title }. The window body always frames
  // the private /show/<id>/ surface, and the title bar offers an open-in-new-tab to the same url.
  showpage: {
    id: 'showpage',
    titleKey: 'apps.showPage.label',
    icon: MonitorPlay,
    accent: '--mint',
    defaultSize: { width: 1040, height: 720 },
    Component: ({ windowId, params }) => (
      <Suspense fallback={<Loading />}>
        <ShowPageBody windowId={windowId} params={params} />
      </Suspense>
    ),
    externalHref: (params) => {
      const sessionId = params?.sessionId;
      return typeof sessionId === 'string' && sessionId ? showPagePrivatePath(sessionId) : undefined;
    },
  },
};

// Dock launcher tiles — the resident apps. `preview` and `showpage` are intentionally excluded
// (opened on demand / while pinned).
export const APP_LIST: AppDefinition[] = [APP_REGISTRY.files, APP_REGISTRY.terminal, APP_REGISTRY.editor];
