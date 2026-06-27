import { CodeXml, Folder, SquareTerminal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';

// The catalogue of windowed apps. The WindowManager + Dock are headless of any
// specific app; everything app-specific (title, icon, default window size, body)
// lives here so adding an app is one registry entry. Real app bodies are wired in
// P3 — P1 ships a shared placeholder so the windowing foundation is verifiable.

export type AppId = 'files' | 'terminal' | 'editor';

export interface AppDefinition {
  id: AppId;
  /** i18n key for the window title / Dock label. */
  titleKey: string;
  icon: LucideIcon;
  /** Tint used for the Dock tile + window title icon. A CSS var token name. */
  accent: string;
  defaultSize: { width: number; height: number };
  /** The window body. Receives the owning window id. */
  Component: React.FC<{ windowId: string }>;
}

// Temporary P1 body — centered app glyph + a muted line. Replaced per-app in P3.
const PlaceholderBody: React.FC<{ icon: LucideIcon; accent: string }> = ({ icon: Icon, accent }) => {
  const { t } = useTranslation();
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 bg-surface">
      <Icon className="size-10" style={{ color: `var(${accent})` }} />
      <span className="text-[12px] text-muted">{t('common.loading')}</span>
    </div>
  );
};

export const APP_REGISTRY: Record<AppId, AppDefinition> = {
  files: {
    id: 'files',
    titleKey: 'apps.fileBrowser.label',
    icon: Folder,
    accent: '--cyan',
    defaultSize: { width: 900, height: 600 },
    Component: () => <PlaceholderBody icon={Folder} accent="--cyan" />,
  },
  terminal: {
    id: 'terminal',
    titleKey: 'apps.terminal.label',
    icon: SquareTerminal,
    accent: '--mint',
    defaultSize: { width: 820, height: 540 },
    Component: () => <PlaceholderBody icon={SquareTerminal} accent="--mint" />,
  },
  editor: {
    id: 'editor',
    titleKey: 'apps.editor.label',
    icon: CodeXml,
    accent: '--violet',
    defaultSize: { width: 1000, height: 640 },
    Component: () => <PlaceholderBody icon={CodeXml} accent="--violet" />,
  },
};

export const APP_LIST: AppDefinition[] = [APP_REGISTRY.files, APP_REGISTRY.terminal, APP_REGISTRY.editor];
