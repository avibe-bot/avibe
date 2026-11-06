import { memo } from 'react';
import type { TFunction } from 'i18next';
import { ArrowRight, CheckCheck, MessageSquareReply } from 'lucide-react';
import clsx from 'clsx';

import type { InboxSession } from '../../context/ApiContext';
import type { Bot } from '../../lib/bots';
import { formatRelativeTime } from '../../lib/relativeTime';
import { BotAvatar } from '../ui/bot-avatar';
import { Presence } from '../ui/presence';
import { BotTag } from './bots/BotTag';
import { InboxPreview } from './InboxPreview';

type Props = {
  session: InboxSession;
  unread: number;
  /** In a muted category: still marked unread, without the mint that asks for attention. */
  quiet: boolean;
  /** Inside the Bots section, whose header already says Bot. */
  inBotSection: boolean;
  bot: Bot | undefined;
  botLabel: string;
  canChat: boolean;
  /** The minute it is now: a card's "3 minutes ago" moves on with it. */
  minute: number;
  /** The page's translator (one subscription for the list, not one per card). */
  t: TFunction;
  onOpen: (session: InboxSession) => void;
  onMarkRead: (sessionId: string) => void;
};

/** The same row, field for field: a live update hands the page new objects for rows that did not change. */
const sameRow = (a: InboxSession, b: InboxSession) => {
  if (a === b) return true;
  const keys = Object.keys(a) as (keyof InboxSession)[];
  return keys.length === Object.keys(b).length && keys.every((key) => Object.is(a[key], b[key]));
};

/**
 * One conversation card on the Inbox page. Drawn again only when what it shows changed, so a live update
 * re-renders the card it is about and not the whole list (with #3256's per-preview memo, that keeps one
 * reply's cost to one card, including the motion each card carries). A Bot's conversation reads as the
 * Bot: avatar, name, and a small Bot tag in place of "project · title" (docs/plans/bots.md D5).
 */
export const InboxCard = memo(function InboxCard({
  session: s,
  unread,
  quiet,
  inBotSection,
  bot,
  botLabel,
  canChat,
  onOpen,
  onMarkRead,
  t,
}: Props) {
  const projectLabel = s.project_name || s.project_id || 'avibe';
  const sessionLabel = s.title?.trim() || s.session_id;
  return (
    <article
      data-inbox-session-id={s.session_id}
      data-flip-id={s.session_id}
      // Read eases the wash away (one card, colour only; M7). Nothing else on a card transitions.
      className={clsx(
        'flex flex-col gap-3 rounded-xl border p-4 transition-[background-color,border-color,box-shadow] duration-(--motion-duration-fast) ease-(--motion-ease-exit)',
        unread > 0
          ? quiet
            ? 'border-border-strong bg-foreground/[0.03]'
            : 'border-mint/30 bg-mint/[0.05] shadow-glow-md-mint'
          : 'border-border bg-surface',
      )}
    >
      <div className="flex items-center gap-2 text-[11px]">
        {bot ? (
          <>
            <BotAvatar avatar={bot.avatar} name={botLabel} size={24} inList />
            <span className="min-w-0 truncate text-[13px] font-semibold text-foreground">{botLabel}</span>
            {!inBotSection && <BotTag />}
            <span className="flex-1" />
          </>
        ) : (
          <>
            <span className="inline-flex max-w-[40%] items-center gap-1 truncate rounded-md border border-border-strong bg-surface-2 px-2 py-0.5 font-semibold text-cyan-ink">
              {projectLabel}
            </span>
            <span className="text-muted">·</span>
            <span className="flex-1 truncate text-[13px] font-semibold text-foreground">
              {sessionLabel}
            </span>
          </>
        )}
        {s.replied && (
          <span className="inline-flex items-center gap-1 rounded-md border border-cyan/30 bg-cyan/[0.08] px-1.5 py-0.5 text-[10px] font-semibold text-cyan-ink">
            <MessageSquareReply className="size-2.5" />
            {t('workbench.inbox.replied')}
          </span>
        )}
        <span className="shrink-0 font-mono text-muted">{formatRelativeTime(s.last_activity_at, t)}</span>
      </div>

      <div className="flex flex-col gap-1">
        <div className="text-[10px] font-bold uppercase tracking-wider text-mint-ink">
          {t('workbench.inbox.agent')}
        </div>
        {s.preview_text ? (
          <InboxPreview
            text={s.preview_text}
            className={clsx(
              'line-clamp-3 text-[13px] leading-relaxed transition-colors duration-(--motion-duration-fast) ease-(--motion-ease-exit)',
              unread > 0 ? 'text-foreground' : 'text-muted',
            )}
          />
        ) : (
          <p className="text-[13px] leading-relaxed text-muted">—</p>
        )}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onOpen(s)}
          className="press inline-flex items-center gap-1.5 rounded-md border border-mint/30 bg-mint/[0.06] px-3 py-1.5 text-[11px] font-semibold text-mint-ink hover:bg-mint/[0.12] active:bg-mint/[0.12]"
        >
          {/* Read: the count shrinks away and the label slides into its room (M7). */}
          <Presence show={unread > 0} value={unread}>
            {(count) => (
              <span
                className={clsx(
                  'inline-flex min-w-[1.1rem] items-center justify-center rounded-full px-1.5 font-mono text-[9px] font-bold',
                  quiet ? 'bg-foreground/[0.18] text-foreground' : 'bg-mint text-primary-foreground',
                )}
              >
                {count > 99 ? '99+' : count}
              </span>
            )}
          </Presence>
          <span>{t('workbench.inbox.openSession')}</span>
          <ArrowRight className="size-3" />
        </button>
        {canChat && (
          <Presence show={unread > 0} kind="fade">
            {() => (
              <button
                type="button"
                onClick={() => onMarkRead(s.session_id)}
                className="press inline-flex items-center gap-1.5 rounded-md border border-border-strong px-3 py-1.5 text-[11px] font-medium text-foreground hover:bg-foreground/[0.04] active:bg-foreground/[0.04]"
              >
                <CheckCheck className="size-3" />
                {t('workbench.inbox.markRead')}
              </button>
            )}
          </Presence>
        )}
      </div>
    </article>
  );
}, (a, b) => {
  const keys = Object.keys(a) as (keyof Props)[];
  return keys.every((key) => (key === 'session' ? sameRow(a.session, b.session) : Object.is(a[key], b[key])));
});
