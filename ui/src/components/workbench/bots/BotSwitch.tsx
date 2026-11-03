import { useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Check, ChevronDown, UserPlus } from 'lucide-react';
import clsx from 'clsx';

import { useBots } from '../../../context/BotsContext';
import { useWorkbenchInbox } from '../../../context/WorkbenchInboxContext';
import { botRole, botSessionPath, type Bot } from '@/lib/bots';
import { formatRelativeTime } from '@/lib/relativeTime';
import { useIsDesktop } from '@/lib/useIsDesktop';
import { BotAvatar } from '../../ui/bot-avatar';
import { Button } from '../../ui/button';
import { Dialog, DialogContent, DialogTitle } from '../../ui/dialog';
import { BotFace } from './BotFace';

const countLabel = (count: number) => (count > 99 ? '99+' : String(count));

// A Bot conversation's title (docs/plans/bots.md D5): the Bot's avatar and name. On a
// phone it is also the way to the other Bots (ui/design: mobile/bot-conversation/header):
// a tap opens every Bot in a sheet (mobile/bot-conversation/switch), and a cyan count
// on the chevron says the others have unread replies. A desktop has the sidebar's strip
// for that, so there the title stays a title.
export function BotConversationTitle({ bot }: { bot: Bot }) {
  const { t } = useTranslation();
  const isDesktop = useIsDesktop();
  const [open, setOpen] = useState(false);
  const { bots, displayName, statusBySession } = useBots();
  const { unreadBySession } = useWorkbenchInbox({ feed: false });
  const name = displayName(bot);
  if (isDesktop) {
    return (
      <div className="flex min-w-0 items-center gap-2">
        <BotAvatar avatar={bot.avatar} name={name} size={26} />
        <span className="min-w-0 truncate text-base font-bold text-foreground">{name}</span>
      </div>
    );
  }
  const othersUnread = bots
    .filter((each) => each.handle !== bot.handle)
    .reduce((sum, each) => sum + (unreadBySession[each.session_id] ?? 0), 0);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={t('bots.switchBot', { name })}
        className={clsx(
          '-ml-1.5 flex min-w-0 items-center gap-2 rounded-full py-0.5 pl-1.5 pr-2 transition',
          open ? 'bg-mint-soft ring-1 ring-mint/40' : 'hover:bg-foreground/4',
        )}
      >
        <BotFace avatar={bot.avatar} name={name} size={26} running={statusBySession[bot.session_id] === 'running'} />
        <span className="min-w-0 truncate text-base font-bold text-foreground">{name}</span>
        <span className="relative shrink-0">
          <ChevronDown className={clsx('size-4 text-muted transition', open && 'rotate-180 text-mint-ink')} />
          {othersUnread > 0 && (
            <span
              aria-label={t('bots.othersUnread', { count: othersUnread })}
              className="absolute -right-2.5 -top-2 inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-cyan px-0.5 font-mono text-3xs font-bold text-accent-foreground shadow-glow-xs-cyan"
            >
              {countLabel(othersUnread)}
            </span>
          )}
        </span>
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="gap-2 max-md:px-3 max-md:pt-3" aria-describedby={undefined} closeLabel={t('common.close')}>
          <BotSwitchList current={bot.handle} onDone={() => setOpen(false)} />
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Every Bot, the main Bot first: what each is doing or last said, unread, and running.
 *  Picking one opens its conversation; Add opens the shared add dialog. Mounted only
 *  while the sheet is open, so only then does it follow the Inbox feed for the previews. */
function BotSwitchList({ current, onDone }: { current: string; onDone: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { bots, displayName, statusBySession, openProfile } = useBots();
  const { inboxSessions, unreadBySession } = useWorkbenchInbox();
  return (
    <>
      <div className="flex items-center justify-between gap-3 px-1 pr-8">
        <DialogTitle className="text-lg font-bold">{t('bots.sectionLabel')}</DialogTitle>
        <Button
          type="button"
          variant="outline"
          size="xs"
          className="gap-1 rounded-full"
          onClick={() => {
            onDone();
            openProfile({ kind: 'create' });
          }}
        >
          <UserPlus className="size-3.5" />
          {t('bots.add')}
        </Button>
      </div>
      <ul className="flex flex-col gap-1">
        {bots.map((bot) => {
          const name = displayName(bot);
          // The main Bot's stored role is written for Agents; a row shows its UI copy.
          const role = botRole(bot, t('bots.mainRole'));
          const row = inboxSessions.find((session) => session.session_id === bot.session_id);
          const running = statusBySession[bot.session_id] === 'running';
          const unread = unreadBySession[bot.session_id] ?? 0;
          const selected = bot.handle === current;
          return (
            <li key={bot.handle}>
              <button
                type="button"
                aria-current={selected ? 'page' : undefined}
                onClick={() => {
                  onDone();
                  if (!selected) navigate(botSessionPath(bot));
                }}
                className={clsx(
                  'flex w-full items-center gap-3 rounded-2xl px-2.5 py-2.5 text-left transition',
                  selected ? 'bg-mint-soft ring-1 ring-mint/40' : 'hover:bg-foreground/3',
                )}
              >
                <BotFace avatar={bot.avatar} name={name} size={44} unread={unread} running={running} dotWell={selected ? 'bg-mint-soft' : 'bg-surface'} />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-base font-semibold text-foreground">{name}</span>
                    {bot.builtin && (
                      <span className="shrink-0 rounded bg-mint-soft px-1 text-3xs font-semibold leading-3.5 text-mint-ink">
                        {t('bots.mainBadge')}
                      </span>
                    )}
                  </span>
                  <span className={clsx('truncate text-xs', unread > 0 ? 'text-foreground' : 'text-muted')}>
                    {running ? (
                      <Trans
                        i18nKey="bots.runningPreview"
                        values={{ preview: row?.preview_text || role }}
                        components={{ running: <span className="font-semibold text-mint-ink" /> }}
                      />
                    ) : (row?.preview_text || role)}
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
    </>
  );
}
