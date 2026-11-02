import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Route, Routes } from 'react-router-dom';
import { FolderTree, Grid2x2, Inbox, LayoutGrid, Plus } from 'lucide-react';
import clsx from 'clsx';

import { MobileBrandHeader, MobileTabBar, type ShellNavItem } from '@/components/MobileChrome';
import { ChatPage } from '@/components/workbench/ChatPage';

/** A tabbed page as AppShell draws it below md: the brand header, the page in the
 *  scrolling main (clear of the tab bar), and the tab bar. `header`, `tabBar`, and
 *  `overlay` take a design's own chrome; the defaults are the shipped ones. */
export function PhoneScreen({
  children,
  header,
  tabBar,
  overlay,
  flat = false,
}: {
  children: ReactNode;
  header?: ReactNode;
  tabBar?: ReactNode;
  overlay?: ReactNode;
  /** The Workbench home draws on flat background; every other page on the console glow. */
  flat?: boolean;
}) {
  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-foreground">
      {header ?? <MobileBrandHeader />}
      <main
        className={clsx(
          'min-h-0 flex-1 overflow-y-auto pb-[var(--mobile-nav-clearance)]',
          flat ? 'bg-background' : 'page-glow-console',
        )}
      >
        <div className="mx-auto w-full px-4 py-5">{children}</div>
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
    { to: '/projects', label: t('nav.projects'), icon: FolderTree },
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

/** Today's tab bar: the four tabs around the raised new-session ＋. */
export function ShippedTabBar({ inboxBadge = 0 }: { inboxBadge?: number }) {
  const { t } = useTranslation();
  const items = useShippedTabs(inboxBadge);
  return <MobileTabBar items={items} center={{ onClick: () => {}, label: t('appShell.newSession'), icon: Plus }} />;
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
