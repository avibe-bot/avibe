import { useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { SquareTerminal } from 'lucide-react';

import { useStandaloneAppTab } from '../../context/StandaloneAppTabContext';
import { useIsDesktop } from '../../lib/useIsDesktop';
import { MobileAppHeader } from '../apps/MobileAppHeader';
import { TerminalTabs } from './TerminalTabs';

// A start directory handed to the terminal when navigating in from the File Browser on mobile
// ("Open Terminal Here"). Carried in router state — like the window params `wm.openApp` passes —
// so absolute paths stay out of the URL. Opening the route directly (tab bar / link, no such
// navigation) carries no cwd and just opens the default terminal.
function readCwd(state: unknown): string | null {
  if (!state || typeof state !== 'object') return null;
  const cwd = (state as Record<string, unknown>).cwd;
  return typeof cwd === 'string' && cwd ? cwd : null;
}

// The Terminal app. `windowed` fills its AppWindow body; the route adds the page header.
// The multi-tab UI + per-tab session/slot lifecycle lives in the reusable TerminalTabs
// (so the editor's integrated terminal can mount the same thing later). Design: `iwYIX`.
// windowId + params thread through for windowed mounts so the tab layout persists across reloads.
export const AppsTerminalPage: React.FC<{ windowed?: boolean; windowId?: string; params?: Record<string, unknown> }> = ({ windowed = false, windowId, params }) => {
  const { t } = useTranslation();
  const location = useLocation();
  // "Open Terminal Here" on mobile: the target dir rides in router state (windowed mounts read it
  // from `params.cwd` instead). location.key is unique per navigation, so TerminalTabs opens
  // exactly one tab per launch and leaves the persistent first tab as a reattach.
  const launchCwd = useMemo(() => readCwd(location.state), [location.state]);
  // A single-app browser tab (`/apps/terminal?standalone=1`): the shell drops its chrome, so the
  // terminal fills the whole web area — no page header, no card border, no padding around it.
  // Still the ROUTE mount, so the first tab keeps its persistent reattachable session (unlike
  // `windowed`, whose tabs are ephemeral).
  const standalone = useStandaloneAppTab();
  const isDesktop = useIsDesktop();
  const mobileRoute = !windowed && !standalone && !isDesktop;

  if (windowed) {
    return (
      <div className="h-full w-full overflow-hidden bg-surface">
        <TerminalTabs windowed windowId={windowId} params={params} />
      </div>
    );
  }

  if (standalone) {
    return (
      <div className="flex h-full w-full overflow-hidden bg-surface">
        <TerminalTabs launchCwd={launchCwd} launchKey={launchCwd ? location.key : undefined} />
      </div>
    );
  }

  if (mobileRoute) {
    return (
      <div className="flex h-full min-h-0 w-full flex-col overflow-hidden bg-surface pb-[env(safe-area-inset-bottom)]">
        <MobileAppHeader title={t('apps.terminal.label')} icon={SquareTerminal} />
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <TerminalTabs launchCwd={launchCwd} launchKey={launchCwd ? location.key : undefined} />
        </div>
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
        <TerminalTabs launchCwd={launchCwd} launchKey={launchCwd ? location.key : undefined} />
      </div>
    </div>
  );
};
