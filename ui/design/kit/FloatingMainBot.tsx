// Variant C: the main Bot floats over every tabbed page, as the pet does on the
// desktop. It sits in the right thumb's reach above the tab bar (it can be dragged
// to the left edge), says its newest unread reply or that it is working in a
// bubble, and opens a peek sheet on tap. The tab bar and its new-session ＋ stay.
import { ChevronRight } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { useBots } from '@/context/BotsContext';
import { useWorkbenchInbox } from '@/context/WorkbenchInboxContext';
import { formatRelativeTime } from '@/lib/relativeTime';

import { BotFace } from './BotFace';
import { useDraft } from './draft';
import { MainBotQuickComposer } from './MainBotQuickComposer';

export function FloatingMainBot({ open = false }: { open?: boolean }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { mainBot, displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  if (!mainBot) return null;
  const name = displayName(mainBot);
  const running = statusBySession[mainBot.session_id] === 'running';
  const unread = unreadBySession[mainBot.session_id] ?? 0;
  const row = inboxSessions.find((s) => s.session_id === mainBot.session_id);
  // It speaks only when there is news: a reply you have not read, or work under way.
  const says = running ? t('bots.running') : unread > 0 ? row?.preview_text : null;
  return (
    <div className="fixed bottom-(--mobile-nav-height) right-4 z-40 flex items-end gap-2 pb-5 md:hidden">
      {says && !open && (
        <div className="mb-3 max-w-56 rounded-2xl rounded-br-md border border-border-strong bg-surface-2 px-3 py-2 text-xs leading-snug text-foreground shadow-xl">
          {running ? (
            <span className="flex items-center gap-1.5">
              <span className="font-semibold text-mint-ink">{says}</span>
              <span className="flex gap-0.5" aria-hidden="true">
                <span className="size-1 rounded-full bg-mint-ink" />
                <span className="size-1 rounded-full bg-mint-ink opacity-70" />
                <span className="size-1 rounded-full bg-mint-ink opacity-40" />
              </span>
            </span>
          ) : (
            <span className="line-clamp-3">{says}</span>
          )}
        </div>
      )}
      <button
        type="button"
        aria-label={draft(`和${name}对话`, `Talk to ${name}`)}
        className={clsx(
          'rounded-full bg-surface p-0.5 shadow-xl ring-2',
          running || open ? 'ring-mint shadow-glow-lg-mint' : 'ring-mint/45',
        )}
      >
        <BotFace bot={mainBot} name={name} size={52} unread={open ? 0 : unread} running={running} />
      </button>
    </div>
  );
}

/** What a tap on the floating Bot opens: every Bot as a chip row to switch between, the chosen
 *  Bot's latest reply, a one-line composer, and the way into the full conversation. */
export function MainBotPeekSheet({ current = 'main' }: { current?: string }) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { bots, botForHandle, displayName, statusBySession } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  const bot = botForHandle(current);
  if (!bot) return null;
  const name = displayName(bot);
  const row = inboxSessions.find((s) => s.session_id === bot.session_id);
  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={name}>
      <div className="fixed inset-x-0 top-0 bottom-(--mobile-nav-height) z-40 bg-background/70 backdrop-blur-sm" />
      <div className="fixed inset-x-0 bottom-(--mobile-nav-height) z-50 flex flex-col gap-3 rounded-t-3xl border border-border bg-surface px-4 pb-4 pt-2 shadow-xl">
        <div className="mx-auto h-1 w-10 rounded-full bg-border" />
        <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-1 pt-1">
          {bots.map((each) => {
            const eachName = displayName(each);
            const selected = each.handle === current;
            return (
              <button key={each.handle} type="button" aria-current={selected ? 'true' : undefined} className="flex w-13 shrink-0 flex-col items-center gap-1">
                <span className={clsx('rounded-full p-0.5', selected ? 'ring-2 ring-mint' : '')}>
                  <BotFace
                    bot={each}
                    name={eachName}
                    size={40}
                    unread={selected ? 0 : unreadBySession[each.session_id] ?? 0}
                    running={statusBySession[each.session_id] === 'running'}
                  />
                </span>
                <span className={clsx('w-full truncate text-center text-2xs leading-tight', selected ? 'font-semibold text-foreground' : 'text-muted')}>
                  {eachName}
                </span>
              </button>
            );
          })}
        </div>
        <div className="flex flex-col gap-1.5 rounded-2xl border border-mint/25 bg-mint/5 px-3.5 py-3">
          <div className="flex items-center justify-between gap-2 text-2xs">
            <span className="font-semibold text-mint-ink">
              {statusBySession[bot.session_id] === 'running' ? t('bots.running') : draft('最新回复', 'Latest reply')}
            </span>
            {row && <span className="font-mono text-muted">{formatRelativeTime(row.last_activity_at, t)}</span>}
          </div>
          <p className="line-clamp-4 text-sm leading-relaxed text-foreground">{row?.preview_text || bot.role}</p>
        </div>
        <MainBotQuickComposer handle={current} />
        <button type="button" className="flex items-center justify-center gap-1 text-xs font-semibold text-mint-ink">
          {draft(`打开和${name}的完整对话`, `Open the full conversation with ${name}`)}
          <ChevronRight className="size-3.5" />
        </button>
      </div>
    </div>
  );
}
