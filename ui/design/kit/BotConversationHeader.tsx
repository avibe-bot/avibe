// A Bot conversation's header on a phone, with the Bot as a switch: tapping the
// avatar and name opens every Bot (BotSwitchSheet), and a cyan count on the
// chevron says other Bots have unread replies. The rest is the shipped header
// (back, Show Page, the ⋯ menu that holds Edit profile since #3152), drawn over the shipped page at
// the same size. It stays above the switch sheet's scrim, so the sheet reads as its.
import { ArrowLeft, ChevronDown, MoreHorizontal, Presentation } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

export function BotConversationHeader({ current, open = false }: { current: string; open?: boolean }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, botForHandle, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const bot = botForHandle(current);
  if (!bot) return null;
  const name = displayName(bot);
  const othersUnread = bots
    .filter((each) => each.handle !== current)
    .reduce((sum, each) => sum + (unreadBySession[each.session_id] ?? 0), 0);
  return (
    <div className="fixed inset-x-0 top-0 z-50 border-b border-border bg-surface px-4 py-2.5 md:hidden">
      <div className="flex items-center gap-3">
        <Button type="button" variant="outline" size="icon" aria-label={t('chat.back')} className="size-7 shrink-0">
          <ArrowLeft className="size-3.5" />
        </Button>
        <button
          type="button"
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={draft(`${name}，切换伙伴`, `${name}, switch Bot`)}
          className={clsx(
            '-ml-1.5 flex min-w-0 items-center gap-2 rounded-full py-0.5 pl-1.5 pr-2 transition',
            open ? 'bg-mint-soft ring-1 ring-mint/40' : 'hover:bg-foreground/4',
          )}
        >
          <BotFace bot={bot} name={name} size={26} running={statusBySession[bot.session_id] === 'running'} />
          <span className="min-w-0 truncate text-base font-bold text-foreground">{name}</span>
          <span className="relative shrink-0">
            <ChevronDown className={clsx('size-4 text-muted transition', open && 'rotate-180 text-mint-ink')} />
            {othersUnread > 0 && (
              <span className="absolute -right-2.5 -top-2 inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-cyan px-0.5 font-mono text-3xs font-bold text-accent-foreground shadow-glow-xs-cyan">
                {othersUnread}
              </span>
            )}
          </span>
        </button>
        <div className="ml-auto flex items-center gap-1.5">
          <Button type="button" variant="ghost" size="sm" aria-label={draft('可视化', 'Visualize')} className="h-7 shrink-0 gap-1.5 px-2">
            <Presentation className="size-3.5" />
          </Button>
          <Button type="button" variant="ghost" size="sm" aria-label={t('workbench.sessionActions')} className="h-7 shrink-0 px-2">
            <MoreHorizontal className="size-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
