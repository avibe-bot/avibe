import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
import { BookmarkPlus, CalendarClock, FolderKanban, Rocket, UserPlus } from 'lucide-react';

import type { TipId } from './tips';

export type TimeOfDay = 'morning' | 'afternoon' | 'evening';

/** Morning from 5 to 12, afternoon to 18, evening after that, by the reader's own clock. */
export const timeOfDay = (date: Date): TimeOfDay => {
  const hour = date.getHours();
  if (hour >= 5 && hour < 12) return 'morning';
  if (hour >= 12 && hour < 18) return 'afternoon';
  return 'evening';
};

export type HomeAsk = { key: string; icon: LucideIcon; label: string; ask: string };

/** The four things to say (ui/design: home/home/*): a short label, and the whole ask it puts in
 *  the composer. The Owner's first asks the main Bot for a new Bot; a member, who has no Bots,
 *  asks to remember something instead. The project ask is the one that depends on the user: it
 *  only starts the sentence, in `project` (the most recent one) or by asking for a new project,
 *  and the user writes the task. */
export function useHomeAsks({ member, project }: { member: boolean; project: string | null }): HomeAsk[] {
  const { t } = useTranslation();
  return [
    member
      ? { key: 'remember', icon: BookmarkPlus, label: t('workbench.home.asks.remember'), ask: t('workbench.home.asks.rememberAsk') }
      : { key: 'addBot', icon: UserPlus, label: t('workbench.home.asks.addBot'), ask: t('workbench.home.asks.addBotAsk') },
    { key: 'projectStatus', icon: FolderKanban, label: t('workbench.home.asks.projectStatus'), ask: t('workbench.home.asks.projectStatusAsk') },
    {
      key: 'startInProject',
      icon: Rocket,
      label: t('workbench.home.asks.startInProject'),
      ask: project
        ? t('workbench.home.asks.startInProjectAsk', { project })
        : t('workbench.home.asks.startNewProjectAsk'),
    },
    { key: 'weeklyReport', icon: CalendarClock, label: t('workbench.home.asks.weeklyReport'), ask: t('workbench.home.asks.weeklyReportAsk') },
  ];
}

/** What "Try" does: put an ask in the composer, open where the capability is set up, or start
 *  adding a Bot. A tip without one only teaches (the phone's voice: the tab bar's center is where
 *  it is tried). */
export type TipTry = { kind: 'ask'; ask: string } | { kind: 'open'; to: string } | { kind: 'addBot' };

export type Tip = { id: TipId; title: string; why: string; tryIt?: TipTry };

/** The approved tips (ui/design: home/home/*), each one capability in one sentence. `name` is the
 *  main Bot's; `petShortcut` the desktop app's voice shortcut as the reader set it, if it can be read. */
export function useTips({ name, petShortcut }: { name: string; petShortcut: string | null }): Record<TipId, Tip> {
  const { t } = useTranslation();
  const tip = (id: TipId, tryIt?: TipTry, why?: string): Tip => ({
    id,
    title: t(`workbench.home.tips.${id}.title`),
    why: why ?? t(`workbench.home.tips.${id}.why`, { name }),
    tryIt,
  });
  return {
    bots: tip('bots', { kind: 'addBot' }),
    showPages: tip('showPages', { kind: 'ask', ask: t('workbench.home.tips.showPages.ask') }),
    tasks: tip('tasks', { kind: 'ask', ask: t('workbench.home.tips.tasks.ask') }),
    vault: tip('vault', { kind: 'open', to: '/vaults' }),
    skills: tip('skills', { kind: 'open', to: '/skills' }),
    chatApps: tip('chatApps', { kind: 'open', to: '/settings/platforms' }),
    desktopPet: tip(
      'desktopPet',
      { kind: 'open', to: '/settings/shortcuts' },
      petShortcut ? t('workbench.home.tips.desktopPet.whyShortcut', { name, shortcut: petShortcut }) : undefined,
    ),
    voice: tip('voice'),
  };
}
