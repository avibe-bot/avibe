import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
import { BookmarkPlus, CalendarClock, FolderKanban, Rocket, UserPlus } from 'lucide-react';

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
