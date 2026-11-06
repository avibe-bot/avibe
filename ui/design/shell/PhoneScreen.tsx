import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Route, Routes } from 'react-router-dom';
import { FolderTree, Grid2x2, Inbox, LayoutGrid } from 'lucide-react';
import clsx from 'clsx';

import { MobileBrandHeader, MobileTabBar, type BotCenterPhase, type ShellNavItem } from '@/components/MobileChrome';
import { MainBotHoldOverlay, MainBotVoiceToast, type HoldView } from '@/components/voice/MainBotVoice';
import { WorkbenchMobileTabBar } from '@/components/WorkbenchMobileTabBar';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { ChatPage } from '@/components/workbench/ChatPage';
import { meterFor } from './meter';

/** A tabbed page as AppShell draws it below md: the brand header, the page in the
 *  scrolling main (clear of the tab bar), and the tab bar. `header`, `tabBar`, and
 *  `overlay` take a design's own chrome; the defaults are the shipped ones. */
export function PhoneScreen({
  children,
  header,
  tabBar,
  overlay,
  flat = false,
  bare = false,
}: {
  children: ReactNode;
  header?: ReactNode;
  tabBar?: ReactNode;
  overlay?: ReactNode;
  /** The Workbench home draws on flat background; every other page on the console glow. */
  flat?: boolean;
  /** The page lays itself out as a column filling the main, clear of the tab bar, and scrolls itself (the home centers its group there). */
  bare?: boolean;
}) {
  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
      {header ?? <MobileBrandHeader />}
      <main
        className={clsx(
          'min-h-0 flex-1 pb-(--mobile-nav-clearance)',
          bare ? 'flex flex-col' : 'overflow-y-auto',
          flat ? 'bg-background' : 'page-glow-console',
        )}
      >
        {bare ? children : <div className="mx-auto w-full px-4 pt-4 pb-5">{children}</div>}
      </main>
      {tabBar ?? <ShippedTabBar />}
      {overlay}
    </div>
  );
}

/** The shipped Workbench tabs (AppShell `workbenchTabs`), with the Inbox badge the frame's unread gives. */
function useShippedTabs(inboxBadge = 0): ShellNavItem[] {
  const { t } = useTranslation();
  return [
    { to: '/inbox', label: t('nav.inbox'), icon: Inbox, badge: inboxBadge },
    { to: '/projects', label: t('nav.workbench'), icon: FolderTree },
    {
      to: '/agents',
      label: t('nav.capabilities'),
      icon: LayoutGrid,
      match: (p: string) => ['/agents', '/skills', '/harness', '/vaults'].some((x) => p.startsWith(x)),
    },
    { label: t('nav.apps'), icon: Grid2x2, onClick: () => {} },
  ];
}

/** The shipped tab items, for a design that rearranges the bar around them. */
export function WithShippedTabs({ inboxBadge = 0, children }: { inboxBadge?: number; children: (items: ShellNavItem[]) => ReactNode }) {
  return <>{children(useShippedTabs(inboxBadge))}</>;
}

/** The shipped tab bar: the four tabs around the main Bot for the Owner, around the new-session ＋ for a member. */
export function ShippedTabBar({ inboxBadge = 0 }: { inboxBadge?: number }) {
  const items = useShippedTabs(inboxBadge);
  return <WorkbenchMobileTabBar items={items} onNewSession={() => {}} />;
}

/** The Owner's bar with the main Bot in one phase of a press (WorkbenchMobileTabBar draws the same
 *  center from a live hold). */
export function BotCenterTabBar({ inboxBadge = 0, phase }: { inboxBadge?: number; phase: BotCenterPhase }) {
  const { t } = useTranslation();
  const items = useShippedTabs(inboxBadge);
  const { mainBot, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  if (!mainBot) return null;
  const name = displayName(mainBot);
  return (
    <MobileTabBar
      items={items}
      bot={{
        avatar: mainBot.avatar,
        name,
        label: t('appShell.openMainBot', { name }),
        unread: unreadBySession[mainBot.session_id] ?? 0,
        running: statusBySession[mainBot.session_id] === 'running',
        onClick: () => {},
        phase,
      }}
    />
  );
}

/** The shipped take card and toast over a page, in the state `hold` describes. */
export function MainBotVoiceLayer({ hold }: { hold: Partial<HoldView> }) {
  const { mainBot, displayName } = useBots();
  if (!mainBot) return null;
  const view: HoldView = {
    phase: 'idle',
    interruptReason: null,
    text: '',
    stash: '',
    seconds: 0,
    outcome: null,
    canRetry: true,
    sendInterrupted: () => {},
    retry: () => {},
    discard: () => {},
    dismiss: () => {},
    cancelSettling: () => {},
    ...hold,
    level: hold.level ?? meterFor(hold.phase ?? 'idle'),
  };
  const bot = { name: displayName(mainBot), avatar: mainBot.avatar };
  return (
    <>
      <MainBotHoldOverlay hold={view} bot={bot} />
      <MainBotVoiceToast hold={view} bot={bot} onView={() => {}} />
    </>
  );
}

/** A conversation, full screen as on a phone (no brand header, no tab bar), drawn by the shipped
 *  chat page from the frame's `botConversationApi` fixtures. */
export function PhoneConversation({ overlay }: { overlay?: ReactNode }) {
  return (
    <div className="relative h-full overflow-hidden bg-background text-foreground page-glow-console">
      <Routes>
        <Route path="/chat/:sessionId" element={<ChatPage />} />
      </Routes>
      {overlay}
    </div>
  );
}
