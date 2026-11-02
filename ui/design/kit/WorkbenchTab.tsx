// The owner's direction (2026-11-02): the Projects tab becomes 工作台 / Workbench and
// reads like the desktop sidebar — the Bots strip first, the projects below. The
// strip and the project list are the shipped components; this kit only stacks them
// and names the tab. The center of the bar is the main Bot (BotCenterTabBar).
import { FolderTree, Grid2x2, Inbox, LayoutGrid } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { ShellNavItem } from '@/components/MobileChrome';
import { BotStrip } from '@/components/workbench/bots/BotStrip';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';

import { BotCenterTabBar, type CenterPhase } from './BotCenterTabBar';
import { useDraft } from './draft';
import { NewSessionTip } from './MainBotVoice';

/** The renamed tab's label: one key when it ships (`nav.workbench`), so the name is a one-line change. */
function useWorkbenchTabLabel(): string {
  const draft = useDraft();
  return draft('工作台', 'Workbench');
}

/** The bar's tabs around the main Bot: Inbox · 工作台 | Capabilities · Apps. */
function useWorkbenchTabs(inboxBadge: number): ShellNavItem[] {
  const { t } = useTranslation();
  const workbench = useWorkbenchTabLabel();
  return [
    { to: '/inbox', label: t('nav.inbox'), icon: Inbox, badge: inboxBadge },
    { to: '/projects', label: workbench, icon: FolderTree },
    {
      to: '/agents',
      label: t('nav.capabilities'),
      icon: LayoutGrid,
      match: (p: string) => ['/agents', '/skills', '/harness', '/vaults'].some((x) => p.startsWith(x)),
    },
    { label: t('nav.apps'), icon: Grid2x2, onClick: () => {} },
  ];
}

/** The bar: Inbox · 工作台 · the main Bot · Capabilities · Apps. */
export function WorkbenchTabBar({ inboxBadge, phase = 'idle' }: { inboxBadge: number; phase?: CenterPhase }) {
  return <BotCenterTabBar items={useWorkbenchTabs(inboxBadge)} phase={phase} />;
}

/** The 工作台 page: the shipped Bots strip, then the shipped project list (whose rows carry 新建会话). */
export function WorkbenchTabPage({ tip = false }: { tip?: boolean }) {
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  return (
    <div className="mx-auto flex max-w-xl flex-col gap-5">
      <BotStrip unreadBySession={unreadBySession} />
      {tip && <NewSessionTip />}
      {/* A block wrapper: the page centers itself with auto margins, which a flex column would not stretch. */}
      <div>
        <ProjectsPage />
      </div>
    </div>
  );
}
