import { useTranslation } from 'react-i18next';

import { TerminalTabs } from './TerminalTabs';

// The Terminal app. `windowed` fills its AppWindow body; the route adds the page header.
// The multi-tab UI + per-tab session/slot lifecycle lives in the reusable TerminalTabs
// (so the editor's integrated terminal can mount the same thing later). Design: `iwYIX`.
// windowId + params thread through for windowed mounts so the tab layout persists across reloads.
export const AppsTerminalPage: React.FC<{ windowed?: boolean; windowId?: string; params?: Record<string, unknown> }> = ({ windowed = false, windowId, params }) => {
  const { t } = useTranslation();

  if (windowed) {
    return (
      <div className="h-full w-full overflow-hidden bg-surface">
        <TerminalTabs windowed windowId={windowId} params={params} />
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.terminal.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.terminal.tagline')}</p>
      </div>
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">
        <TerminalTabs />
      </div>
    </div>
  );
};
