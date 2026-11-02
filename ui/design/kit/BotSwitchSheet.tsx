// Every Bot in one sheet, the main Bot first: what each is doing or last said,
// unread, and running. Opened by a long press on the center (variant A) and by
// tapping the name in any Bot's conversation (all variants); picking a row opens
// that Bot's conversation.
import { Check, UserPlus } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { formatRelativeTime } from '@/lib/relativeTime';

import { BotFace } from './BotFace';
import { useDraft } from './draft';

export function BotSwitchSheet({ current, aboveTabBar = false }: { current?: string; aboveTabBar?: boolean }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  const bottom = aboveTabBar ? 'bottom-[calc(4.75rem+env(safe-area-inset-bottom))]' : 'bottom-0 pb-[calc(1rem+env(safe-area-inset-bottom))]';
  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={draft('伙伴', 'Bots')}>
      <div className={clsx('fixed inset-x-0 top-0 z-40 bg-background/70 backdrop-blur-sm', aboveTabBar ? bottom : 'bottom-0')} />
      <div className={clsx('fixed inset-x-0 z-50 max-h-[72dvh] overflow-y-auto rounded-t-3xl border border-border bg-surface px-3 pt-2 shadow-xl', bottom, aboveTabBar && 'pb-3')}>
        <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-border" />
        <div className="mb-2 flex items-center justify-between gap-3 px-1">
          <h2 className="text-[17px] font-bold text-foreground">{draft('伙伴', 'Bots')}</h2>
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
                    selected ? 'bg-mint-soft ring-1 ring-mint/40' : 'hover:bg-foreground/[0.03]',
                  )}
                >
                  <BotFace bot={bot} name={name} size={44} unread={unread} running={running} dotWell={selected ? 'bg-mint-soft' : 'bg-surface'} />
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-[15px] font-semibold text-foreground">{name}</span>
                      {bot.builtin && (
                        <span className="shrink-0 rounded bg-mint-soft px-1 text-[9px] font-semibold leading-[14px] text-mint-ink">
                          {draft('主伙伴', 'Main')}
                        </span>
                      )}
                    </span>
                    <span className={clsx('truncate text-[12px]', unread > 0 ? 'text-foreground' : 'text-muted')}>
                      {running && <span className="font-semibold text-mint-ink">{t('bots.running')} · </span>}
                      {row?.preview_text || bot.role}
                    </span>
                  </span>
                  <span className="flex shrink-0 flex-col items-end gap-1">
                    {row && <span className="font-mono text-[10.5px] text-muted">{formatRelativeTime(row.last_activity_at, t)}</span>}
                    {selected && <Check className="size-4 text-mint-ink" />}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
