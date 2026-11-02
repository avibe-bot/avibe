// The owner's direction (2026-11-02): the Projects tab becomes 工作台 / Workbench and
// reads like the desktop sidebar — the Bots strip first, the projects below. The
// strip is the shipped component. Each project row gains a visible ＋ to the right of
// its ⋯ that starts a Session in that project (owner decision, round 2); the ⋯ menu
// keeps its 新建会话. The row with the ＋ is not shipped yet, so it is drawn here with
// the shipped row's classes. The center of the bar is the main Bot (BotCenterTabBar).
import { ChevronRight, Ellipsis, Folder, FolderPlus, FolderTree, Grid2x2, Inbox, LayoutGrid, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { ShellNavItem } from '@/components/MobileChrome';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { BotStrip } from '@/components/workbench/bots/BotStrip';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { useWorkbenchProjectsTree } from '@/context/WorkbenchProjectsContext';

import { BotCenterTabBar, type CenterPhase } from './BotCenterTabBar';
import { useDraft } from './draft';

/** The renamed tab's label: one key when it ships (`nav.workbench`). */
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

/** The project list with a ＋ on every row (the shipped mobile project list's header and row, plus the ＋). */
function ProjectList() {
  const { t } = useTranslation();
  const { projects, sessionsOf } = useWorkbenchProjectsTree();
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">{t('projects.title')}</h1>
        <Button type="button" variant="outline" size="icon" aria-label={t('projects.newProject')} className="border-mint/35 bg-mint/8 text-mint-ink hover:bg-mint/14">
          <FolderPlus className="size-4" />
        </Button>
      </div>
      {(projects ?? []).map((project) => {
        const sessions = sessionsOf(project.id).sessions;
        return (
          <div key={project.id} className="overflow-hidden rounded-xl border border-border bg-surface">
            <div className="flex items-center pr-1.5">
              <button type="button" className="flex min-w-0 flex-1 items-center gap-2.5 px-4 py-3.5 text-left">
                <Folder className="size-4 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate text-sm font-semibold">{project.display_name}</span>
                {sessions !== null && <Badge variant="secondary" className="font-mono text-2xs">{sessions.length}</Badge>}
                <ChevronRight className="size-4 shrink-0 text-muted" />
              </button>
              <Button type="button" variant="ghost" size="icon" aria-label={t('workbench.projectActions')} className="size-8 shrink-0 text-muted">
                <Ellipsis className="size-4" />
              </Button>
              <Button type="button" variant="ghost" size="icon" aria-label={t('newSession.title')} className="size-8 shrink-0 text-mint-ink">
                <Plus className="size-4" />
              </Button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** The 工作台 page: the shipped Bots strip, then the projects, each with its ＋. */
export function WorkbenchTabPage() {
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  return (
    <div className="mx-auto flex max-w-xl flex-col gap-5">
      <BotStrip unreadBySession={unreadBySession} />
      <ProjectList />
    </div>
  );
}
