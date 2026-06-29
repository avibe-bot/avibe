import { Suspense, lazy } from 'react';
import { CodeXml, Folder, SquareTerminal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';

// The catalogue of windowed apps. The WindowManager + Dock are headless of any
// specific app; everything app-specific (title, icon, default window size, body)
// lives here so adding an app is one registry entry. App bodies are lazy-loaded
// so opening the workbench doesn't pull file-browser / xterm code into the main
// bundle — each loads only when its window first opens.

export type AppId = 'files' | 'terminal' | 'editor';

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
    Component: () => (
      <Suspense fallback={<Loading />}>
        {/* Each windowed terminal takes a bounded, reused session slot internally so
            it gets its own backend session without leaking ids (see AppsTerminalPage). */}
        <TerminalBody windowed />
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
};

export const APP_LIST: AppDefinition[] = [APP_REGISTRY.files, APP_REGISTRY.terminal, APP_REGISTRY.editor];
