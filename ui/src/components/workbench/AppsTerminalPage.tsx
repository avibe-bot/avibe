import { Suspense, lazy, useMemo } from 'react';
import { useTranslation } from 'react-i18next';

// Lazy so xterm.js stays out of the main bundle until the Terminal is opened.
const TerminalView = lazy(() => import('./TerminalView').then((m) => ({ default: m.TerminalView })));

// A stable per-browser session id so the tmux-backed session reconnects to the
// same shell after a refresh / network drop (persistence). Falls back to a fixed
// id if localStorage is unavailable.
function getSessionId(): string {
  const KEY = 'avibe.terminal.sessionId';
  try {
    let id = window.localStorage.getItem(KEY);
    if (!id) {
      id = `wb-${Math.random().toString(36).slice(2, 10)}`;
      window.localStorage.setItem(KEY, id);
    }
    return id;
  } catch {
    return 'workbench';
  }
}

export const AppsTerminalPage: React.FC = () => {
  const { t } = useTranslation();
  const sessionId = useMemo(() => getSessionId(), []);
  return (
    <div className="flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]">
      <div>
        <h1 className="text-[18px] font-semibold text-foreground">{t('apps.terminal.label')}</h1>
        <p className="text-[12px] text-muted">{t('apps.terminal.tagline')}</p>
      </div>
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface">
        <Suspense
          fallback={<div className="grid flex-1 place-items-center text-[12px] text-muted">{t('common.loading')}</div>}
        >
          <TerminalView sessionId={sessionId} />
        </Suspense>
      </div>
    </div>
  );
};
