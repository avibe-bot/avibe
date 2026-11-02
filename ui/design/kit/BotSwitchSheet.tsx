// Every Bot in one sheet, the main Bot first: what each is doing or last said,
// unread, and running. Opened by tapping the name in a Bot's conversation
// (BotConversationHeader); picking a row opens that Bot's conversation.
import { Check, UserPlus } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { formatRelativeTime } from '@/lib/relativeTime';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

export function BotSwitchSheet({ current }: { current?: string }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={draft('伙伴', 'Bots')}>
      <div className="fixed inset-0 z-40 bg-background/70 backdrop-blur-sm" />
      <div className="fixed inset-x-0 bottom-0 z-50 max-h-3/4 overflow-y-auto rounded-t-3xl border border-border bg-surface px-3 pb-4 pt-2 shadow-xl">
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-border" />
        <div className="mb-2 flex items-center justify-between gap-3 px-1">
          <h2 className="text-lg font-bold text-foreground">{draft('伙伴', 'Bots')}</h2>
          <Button type="button" variant="outline" size="xs" className="gap-1 rounded-full">
            <UserPlus className="size-3.5" />
            {draft('添加', 'Add')}
          </Button>
        </div>
        <ul className="flex flex-col gap-1">
          {bots.map((bot) => {
            const name = displayName(bot);
            const row = inboxSessions.find((s) => s.session_id === bot.session_id);
            const running = statusBySession[bot.session_id] === 'running';
            const unread = unreadBySession[bot.session_id] ?? 0;
            const selected = bot.handle === current;
            return (
              <li key={bot.handle}>
                <button
                  type="button"
                  aria-current={selected ? 'page' : undefined}
                  className={clsx(
                    'flex w-full items-center gap-3 rounded-2xl px-2.5 py-2.5 text-left transition',
                    selected ? 'bg-mint-soft ring-1 ring-mint/40' : 'hover:bg-foreground/3',
                  )}
                >
                  <BotFace bot={bot} name={name} size={44} unread={unread} running={running} dotWell={selected ? 'bg-mint-soft' : 'bg-surface'} />
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-base font-semibold text-foreground">{name}</span>
                      {bot.builtin && (
                        <span className="shrink-0 rounded bg-mint-soft px-1 text-3xs font-semibold leading-3.5 text-mint-ink">
                          {draft('主伙伴', 'Main')}
                        </span>
                      )}
                    </span>
                    <span className={clsx('truncate text-xs', unread > 0 ? 'text-foreground' : 'text-muted')}>
                      {running && <span className="font-semibold text-mint-ink">{t('bots.running')} · </span>}
                      {row?.preview_text || bot.role}
                    </span>
                  </span>
                  <span className="flex shrink-0 flex-col items-end gap-1">
                    {row && <span className="font-mono text-2xs text-muted">{formatRelativeTime(row.last_activity_at, t)}</span>}
                    {selected && <Check className="size-4 text-mint-ink" />}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        <div className="h-(--safe-area-bottom)" />
      </div>
    </div>
  );
}
