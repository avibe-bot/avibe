import { memo, type Ref } from 'react';
import { Link } from 'react-router-dom';
import type { TFunction } from 'i18next';
import { CheckCheck, MessageSquareReply } from 'lucide-react';
import clsx from 'clsx';

import type { InboxSession } from '../../context/ApiContext';
import type { Bot } from '../../lib/bots';
import { formatRelativeTime } from '../../lib/relativeTime';
import { BotAvatar } from '../ui/bot-avatar';
import { Presence } from '../ui/presence';
import { BotTag } from './bots/BotTag';
import { InboxPreview } from './InboxPreview';
import { chatHref, useCardOpen } from './useCardOpen';
import { useCardSwipe } from './useCardSwipe';

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
 *
 * The card itself opens the conversation (`useCardOpen` has the rule): its title is the link, and a press
 * anywhere on it that is not on 「Mark read」 or a drag follows that link. The unread count sits in the
 * header beside the time; 「Mark read」 keeps a row while the card is unread, at its right end, away from
 * where a press that opens the card lands, and the row goes with it. On a touch screen a finger swipes an
 * unread card either way to mark it read (`useCardSwipe` has the rule), uncovering 「Read」 beside it.
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
  const href = chatHref(s.session_id);
  const opens = useCardOpen(href, () => onOpen(s));
  const markable = canChat && unread > 0;
  const { row: swipeRow, sliderRef, actionRef, settling } = useCardSwipe(markable, () => onMarkRead(s.session_id));
  return (
    // The list's row (it moves in the list, M7). The card inside it moves sideways under a finger; the
    // row stays, and holds what the card uncovers. A markable row leaves vertical moves to the page's
    // scroll and takes sideways ones itself.
    <div
      data-flip-id={s.session_id}
      {...swipeRow}
      className={clsx('relative', markable && 'touch-pan-y touch-pinch-zoom')}
    >
      {(markable || settling) && <SwipeAction ref={actionRef} label={t('workbench.inbox.swipeRead')} />}
      <div ref={sliderRef} className="relative">
        <article
          data-inbox-session-id={s.session_id}
          {...opens.card}
          // A surface under the finger (M8): it sinks, and its fill steps once (read eases the wash
          // away the same way, colour only). Not while the press is on 「Mark read」, which presses
          // itself. Its title link's keyboard focus rings the whole card.
          className={clsx(
            'press press-surface flex cursor-pointer flex-col gap-3 rounded-xl border p-4',
            '[&:has(button:active)]:[--press-scale:1]',
            'has-[a:focus-visible]:ring-2 has-[a:focus-visible]:ring-ring has-[a:focus-visible]:ring-offset-2 has-[a:focus-visible]:ring-offset-background',
            unread > 0
              ? quiet
                ? 'border-edge bg-foreground/[0.03] hover:bg-foreground/[0.05] [&:active:not(:has(button:active))]:bg-foreground/[0.05] shadow-elevation-0'
                : 'border-mint/45 bg-mint/[0.05] hover:bg-mint/[0.08] [&:active:not(:has(button:active))]:bg-mint/[0.08]'
              : 'border-edge bg-surface hover:bg-foreground/[0.03] [&:active:not(:has(button:active))]:bg-foreground/[0.03] shadow-elevation-0',
          )}
        >
          <div className="flex items-center gap-2 text-[11px]">
            {bot ? (
              <>
                <BotAvatar avatar={bot.avatar} name={botLabel} size={24} inList />
                <Link to={href} {...opens.link} className="min-w-0 truncate text-[13px] font-semibold text-foreground focus-visible:outline-none">
                  {botLabel}
                </Link>
                {!inBotSection && <BotTag />}
                <span className="flex-1" />
              </>
            ) : (
              <>
                <span className="inline-flex max-w-[40%] items-center gap-1 truncate rounded-md border border-edge bg-surface-2 px-2 py-0.5 font-semibold text-cyan-ink shadow-elevation-0">
                  {projectLabel}
                </span>
                <span className="text-muted">·</span>
                <Link to={href} {...opens.link} className="flex-1 truncate text-[13px] font-semibold text-foreground focus-visible:outline-none">
                  {sessionLabel}
                </Link>
              </>
            )}
            {s.replied && (
              <span className="inline-flex items-center gap-1 rounded-md border border-cyan/30 bg-cyan/[0.08] px-1.5 py-0.5 text-[10px] font-semibold text-cyan-ink">
                <MessageSquareReply className="size-2.5" />
                {t('workbench.inbox.replied')}
              </span>
            )}
            {/* Read: the count shrinks away (M7). */}
            <Presence show={unread > 0} value={unread}>
              {(count) => (
                <span
                  data-inbox-unread-count=""
                  className={clsx(
                    'inline-flex min-w-[1.1rem] shrink-0 items-center justify-center rounded-full px-1.5 font-mono text-[9px] font-bold',
                    quiet ? 'bg-foreground/[0.18] text-foreground' : 'bg-mint text-primary-foreground',
                  )}
                >
                  {count > 99 ? '99+' : count}
                </span>
              )}
            </Presence>
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

          {/* Only while there is something to mark: the row goes in the same change, so the list's
              motion closes the room (M7) instead of a fade leaving it empty first. */}
          {markable && (
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => onMarkRead(s.session_id)}
                className="press inline-flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-[11px] font-medium text-foreground bg-field hover:bg-field-hover active:bg-field-hover"
              >
                <CheckCheck className="size-3" />
                {t('workbench.inbox.markRead')}
              </button>
            </div>
          )}
        </article>
      </div>
    </div>
  );
}, (a, b) => {
  const keys = Object.keys(a) as (keyof Props)[];
  return keys.every((key) => (key === 'session' ? sameRow(a.session, b.session) : Object.is(a[key], b[key])));
});

/** What a swipe uncovers beside an unread card (ui/design: motion/inbox/phone-swipe-*): 「Read」 on soft
 *  mint at each side of the row, out of sight until the card moves. `useCardSwipe` brings the side the
 *  card uncovers in with it, a small gap behind the card's edge, and fills it once letting go would mark
 *  the card read (`swipe-action-face` in index.css). A picture of the gesture, not a control: the
 *  button and the swipe are the ways to mark read. */
function SwipeAction({ label, ref }: { label: string; ref: Ref<HTMLDivElement> }) {
  const side = (at: 'start' | 'end') => (
    <div
      data-swipe-side={at}
      className={clsx(
        'swipe-action-face absolute inset-y-0 flex w-full items-center rounded-xl',
        at === 'start' ? '-left-2 justify-end [transform:translateX(-100%)]' : '-right-2 justify-start [transform:translateX(100%)]',
      )}
    >
      <div className="flex w-16 flex-col items-center gap-1 text-[11px] font-semibold">
        <CheckCheck className="size-5" />
        {label}
      </div>
    </div>
  );
  return (
    <div ref={ref} aria-hidden="true" className="swipe-action pointer-events-none absolute inset-0 overflow-hidden rounded-xl">
      {side('start')}
      {side('end')}
    </div>
  );
}
