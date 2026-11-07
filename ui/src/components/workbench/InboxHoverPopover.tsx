import { Suspense, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, MessageSquareReply, RefreshCw } from 'lucide-react';
import clsx from 'clsx';

import type { InboxSession } from '../../context/ApiContext';
import type { InboxFeedStatus } from '../../context/WorkbenchInboxContext';
import type { Bot } from '../../lib/bots';
import { useBots } from '../../context/BotsContext';
import { useNotificationPreferences } from '../../context/NotificationPreferencesContext';
import { useBotRoster } from '../../context/BotRosterContext';
import { writeInboxFilter } from '../../lib/inboxFilterMemory';
import { inboxCategory, isCategoryLoud, type InboxCategory } from '../../lib/notificationPreferences';
import { formatRelativeTime } from '../../lib/relativeTime';
import { BotAvatar } from '../ui/bot-avatar';
import { PopoverContent } from '../ui/popover';
import { NotificationMenu } from './NotificationMenu';
import { useNotificationMode } from './useNotificationMode';
import { BotTag } from './bots/BotTag';
import { InboxSectionHeader } from './InboxSectionHeader';
import { chatHref, useCardOpen } from './useCardOpen';
import { InboxPopoverRowsSkeleton } from './listSkeletons';
import { FlipList } from '../ui/flip-list';
import { useSkeletonHandover } from '../ui/skeleton-handover';
import { lazyPage } from '../../lib/lazyPage';

/** A reply's Markdown preview: the Markdown renderer loads with the chat page, not
 *  the entry, and the first-screen prefetch (routePages.ts) has it ready before a hover. */
export const DeferredInboxPreview = lazyPage(() => import('./InboxPreview').then((m) => m.InboxPreview));

// How many rows the peek holds: five in one list, or three per section once the
// caller has Bots. Named because two things depend on it: which rows are picked,
// and whether the peek had room it could not fill.
const PEEK_ROWS = 5;
const SECTION_ROWS = 3;

/**
 * The rows a peek shows: unread first, then the rest. `sort` is stable, so within
 * each group the feed keeps the backend's activity order untouched.
 *
 * This decides *which* rows appear, not only how they are stacked, and that is the
 * point: the popover is a short peek at a feed that is usually longer, so a
 * strictly chronological slice hides the unread conversation the user opened it to
 * act on behind ones they have already read. The full Inbox page keeps the
 * chronological order on purpose: it has unread/all tabs and keyset pagination,
 * which a client-side reorder would fight.
 */
const peek = (rows: InboxSession[], unreadBySession: Record<string, number>, limit: number) =>
  [...rows]
    .sort((a, b) => Number((unreadBySession[b.session_id] ?? 0) > 0) - Number((unreadBySession[a.session_id] ?? 0) > 0))
    .slice(0, limit);

type Section = {
  /** Absent for the single list a caller without Bots sees. */
  category?: InboxCategory;
  rows: InboxSession[];
  limit: number;
  /** Unread conversations in the whole feed, including pages not loaded. */
  unreadSessions: number;
  /** Unread replies, for the section header. */
  unread: number;
};

/**
 * One conversation in the peek, opened by a press on the row itself (`useCardOpen` has the rule): its
 * title is the link, and the rest of the row follows it. Unread stays marked in a muted category; only
 * the mint that asks for attention is withheld.
 */
function InboxPeekRow({
  session: s,
  unread,
  quiet,
  inBotSection,
  bot,
  botLabel,
  onOpen,
}: {
  session: InboxSession;
  unread: number;
  quiet: boolean;
  inBotSection: boolean;
  bot: Bot | undefined;
  botLabel: string;
  onOpen: (session: InboxSession) => void;
}) {
  const { t } = useTranslation();
  const href = chatHref(s.session_id);
  const opens = useCardOpen(href, () => onOpen(s));
  const replied = s.replied ? (
    <MessageSquareReply className="size-3 shrink-0 text-cyan-ink" aria-label={t('workbench.inbox.replied')} />
  ) : null;
  const preview = s.preview_text ? (
    <Suspense fallback={null}>
      <DeferredInboxPreview
        text={s.preview_text}
        className={clsx('line-clamp-2 text-[11.5px] leading-relaxed', unread > 0 ? 'text-foreground' : 'text-muted')}
      />
    </Suspense>
  ) : null;
  // A surface under the pointer (M8): it sinks, and its fill steps to the one it hovers to.
  const shell = clsx(
    'press press-surface cursor-pointer rounded-lg px-3 py-2.5 text-left',
    'has-[a:focus-visible]:ring-2 has-[a:focus-visible]:ring-ring',
    unread > 0
      ? quiet
        ? 'border-l-2 border-border-strong bg-foreground/[0.04] hover:bg-foreground/[0.07] active:bg-foreground/[0.07]'
        : 'border-l-2 border-mint bg-mint/[0.06] hover:bg-mint/[0.10] active:bg-mint/[0.10]'
      : 'hover:bg-foreground/[0.04] active:bg-foreground/[0.04]',
  );
  if (bot) {
    // A Bot's conversation reads as the Bot: its avatar and name instead of "project · title"
    // (docs/plans/bots.md D5). Inside the Bots section the header already says so, which is why the
    // row carries no Bot tag there.
    return (
      <div data-flip-id={s.session_id} data-inbox-peek-id={s.session_id} {...opens.card} className={clsx('flex gap-2.5', shell)}>
        <BotAvatar avatar={bot.avatar} name={botLabel} size={28} inList className="mt-0.5" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-center gap-1.5 text-[11px]">
            <Link to={href} {...opens.link} className="truncate font-bold text-foreground focus-visible:outline-none">{botLabel}</Link>
            {!inBotSection && <BotTag />}
            <span className="flex-1" />
            {replied}
            <span className="shrink-0 font-mono text-[10px] text-muted">{formatRelativeTime(s.last_activity_at, t)}</span>
          </div>
          {preview}
        </div>
      </div>
    );
  }
  const projectLabel = s.project_name || s.project_id;
  return (
    <div data-flip-id={s.session_id} data-inbox-peek-id={s.session_id} {...opens.card} className={clsx('flex flex-col gap-1.5', shell)}>
      <div className="flex items-center gap-1.5 text-[10px]">
        {projectLabel && (
          <span className="truncate font-semibold text-cyan-ink after:ml-1.5 after:font-normal after:text-muted after:content-['·']">
            {projectLabel}
          </span>
        )}
        <Link to={href} {...opens.link} className="flex-1 truncate font-semibold text-foreground focus-visible:outline-none">
          {s.title?.trim() || s.session_id}
        </Link>
        {replied}
        <span className="font-mono text-muted">{formatRelativeTime(s.last_activity_at, t)}</span>
      </div>
      {preview}
    </div>
  );
}

// 360px floating popover that opens when the user hovers the Inbox icon
// (ui/design: sidebar/inbox-popover/open): header, a few conversation cards, an "open full inbox" link,
// split into Bots and Sessions for a caller with Bots
// (docs/plans/workbench-refinements.md §1). The bell swaps the body for the
// notification switches in place, so the hover popover never has to host a second
// floating layer. Data comes from <WorkbenchInboxProvider>.
export const InboxHoverPopover: React.FC<{
  sessions: InboxSession[];
  unreadBySession: Record<string, number>;
  unreadSessions: number;
  totalUnread: number;
  canMarkRead: boolean;
  /** The feed's first read: before it answers the peek draws skeleton rows, and a
   *  failed one says so with a retry, rather than "no conversations". */
  feedStatus?: InboxFeedStatus;
  onRetry?: () => void;
  onItemClick: (session: InboxSession) => void;
  onMarkAllRead: () => void;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  /** Close the popover: leaving for Settings must not leave it floating over the page. */
  onClose?: () => void;
}> = ({
  sessions,
  unreadBySession,
  unreadSessions,
  totalUnread,
  canMarkRead,
  feedStatus = 'loaded',
  onRetry,
  onItemClick,
  onMarkAllRead,
  onMouseEnter,
  onMouseLeave,
  onClose,
}) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { enabled: hasBots, botForSession, displayName: botName } = useBots();
  const { preferences } = useNotificationPreferences();
  const { roster } = useBotRoster();
  const { mode, icon: BellIcon } = useNotificationMode();
  const [view, setView] = useState<'inbox' | 'notifications'>('inbox');
  // The unread map is authoritative; a session absent from it has 0 unread
  // (don't fall back to the card's stale unread_count — see InboxPage).
  const unreadOf = (s: InboxSession) => unreadBySession[s.session_id] ?? 0;

  const sections = useMemo<Section[]>(() => {
    if (!hasBots) {
      return [{ rows: peek(sessions, unreadBySession, PEEK_ROWS), limit: PEEK_ROWS, unreadSessions, unread: totalUnread }];
    }
    return (['bot', 'session'] as const).flatMap((category) => {
      const rows = sessions.filter((s) => inboxCategory(s.session_id, roster.sessionIds) === category);
      const unreadIds = Object.entries(unreadBySession)
        .filter(([id, n]) => n > 0 && inboxCategory(id, roster.sessionIds) === category);
      const unread = unreadIds.reduce((sum, [, n]) => sum + n, 0);
      // A section with nothing in it says nothing; Bots appear once one has spoken.
      if (category === 'bot' && rows.length === 0 && unread === 0) return [];
      return [{ category, rows: peek(rows, unreadBySession, SECTION_ROWS), limit: SECTION_ROWS, unreadSessions: unreadIds.length, unread }];
    });
  }, [hasBots, roster.sessionIds, sessions, unreadBySession, unreadSessions, totalUnread]);

  const openFullInbox = (filter?: 'unread') => {
    // The Inbox page resolves its tab from this store on entry, so writing it
    // first is how a caller asks for a tab without a new route or prop.
    if (filter) writeInboxFilter(filter, 0);
    navigate('/inbox');
  };

  const renderRow = (s: InboxSession, quiet: boolean, inBotSection: boolean) => {
    const bot = botForSession(s.session_id);
    return (
      <InboxPeekRow
        key={s.session_id}
        session={s}
        unread={unreadOf(s)}
        quiet={quiet}
        inBotSection={inBotSection}
        bot={bot}
        botLabel={bot ? botName(bot) : ''}
        onOpen={onItemClick}
      />
    );
  };

  const anyRows = sections.some((section) => section.rows.length > 0);
  // Nothing to show and the feed's first answer still owed, or failed (§6).
  const showSkeleton = !anyRows && feedStatus === 'pending';
  const showFailed = !anyRows && feedStatus === 'failed';
  const rowsHandover = useSkeletonHandover(showSkeleton);
  // What the peek draws, in order, for its motion (M7): a reply moves its row up.
  const flipIds = sections.flatMap((section, index) => [
    ...(index > 0 ? [`divider:${section.category}`] : []),
    ...(section.category ? [`section:${section.category}`] : []),
    ...section.rows.map((row) => row.session_id),
    `beyond:${section.category ?? 'all'}`,
  ]);

  return (
    // Portaled (PopoverContent) so it escapes the sidebar's stacking context and
    // floats above the chat panel — a plain `absolute z-50` div was painted under
    // the chat's own stacking context, so the two visually overlapped.
    <PopoverContent
      side="right"
      align="start"
      sideOffset={12}
      data-settings-interaction-owner="true"
      onOpenAutoFocus={(e) => e.preventDefault()}
      role="dialog"
      aria-label={t('workbench.inbox.title')}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className="flex w-[360px] flex-col gap-2.5 rounded-2xl border-border-strong bg-surface-2 p-3.5 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.6)]"
    >
      {view === 'notifications' ? (
        <>
          <button
            type="button"
            onClick={() => setView('inbox')}
            className="flex items-center gap-1.5 self-start rounded-md text-[11px] font-medium text-muted hover:text-foreground"
          >
            <ArrowLeft className="size-3.5" />
            {t('workbench.notifications.back')}
          </button>
          <NotificationMenu onNavigate={onClose} />
        </>
      ) : (
        <>
          <div className="flex items-start gap-2">
            <div className="flex flex-1 flex-col">
              <div className="text-[13px] font-bold text-foreground">{t('workbench.inbox.title')}</div>
              <div className="text-[10px] text-muted">
                {t('workbench.inbox.headerCount', { unread: unreadSessions, total: sessions.length })}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setView('notifications')}
              aria-label={t('workbench.notifications.open')}
              title={t('workbench.notifications.open')}
              className="inline-flex h-[22px] items-center gap-1 rounded-md border border-border-strong px-1.5 text-[10px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
            >
              <BellIcon className={clsx('size-3.5', mode === 'off' && 'text-muted')} />
              {mode !== 'all' && <span>{t(`workbench.notifications.mode.${mode}`)}</span>}
            </button>
            {canMarkRead && <button
              type="button"
              onClick={onMarkAllRead}
              disabled={totalUnread === 0}
              className={clsx(
                'rounded-md border px-2 py-1 text-[10px] font-medium transition',
                totalUnread === 0
                  ? 'cursor-not-allowed border-border bg-foreground/[0.02] text-muted'
                  : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
              )}
            >
              {t('workbench.inbox.markAllRead')}
            </button>}
          </div>

          {showSkeleton ? (
            <InboxPopoverRowsSkeleton rows={3} />
          ) : showFailed ? (
            <div role="alert" className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border px-3 py-6 text-center text-[12px] text-muted">
              {t('workbench.inbox.loadError')}
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  className="inline-flex items-center gap-1 rounded-md border border-border-strong px-2 py-1 text-[10px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
                >
                  <RefreshCw className="size-3" />
                  {t('common.retry')}
                </button>
              )}
            </div>
          ) : !anyRows ? (
            <div className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-[12px] text-muted">
              {t('workbench.inbox.empty')}
            </div>
          ) : (
            <FlipList ids={flipIds} className={clsx('flex flex-col gap-2.5', rowsHandover)}>
            {sections.map((section, index) => {
              const quiet = section.category ? !isCategoryLoud(preferences, section.category) : false;
              // `unreadSessions` counts the whole feed; the rows hold only the pages the
              // provider has loaded, so an unread conversation older than the loaded
              // window is in the count but not in the rows, and no ordering can
              // surface it. Say so rather than letting a header that reads "3 unread"
              // sit above read rows. The condition is deliberately "the peek had room
              // and could not fill it": a peek already full of unread hides nothing
              // the header does not state.
              const unreadShown = section.rows.filter((s) => unreadOf(s) > 0).length;
              const unreadBeyondPeek = unreadShown < section.limit && section.unreadSessions > unreadShown;
              return (
                <section key={section.category ?? 'all'} className="flex flex-col gap-1">
                  {index > 0 && <div data-flip-id={`divider:${section.category}`} className="mx-1 mb-1 h-px bg-border" />}
                  {section.category && (
                    <InboxSectionHeader
                      category={section.category}
                      unread={section.unread}
                      quiet={quiet}
                      className="px-1"
                      flipId={`section:${section.category}`}
                    />
                  )}
                  {section.rows.map((s) => renderRow(s, quiet, section.category === 'bot'))}
                  {unreadBeyondPeek && (
                    <button
                      type="button"
                      data-flip-id={`beyond:${section.category ?? 'all'}`}
                      onClick={() => openFullInbox('unread')}
                      className="rounded-lg border border-dashed border-border px-3 py-2 text-left text-[11px] leading-relaxed text-muted transition hover:bg-foreground/[0.04] hover:text-foreground"
                    >
                      {t('workbench.inbox.moreUnreadInFull')}
                    </button>
                  )}
                </section>
              );
            })}
            </FlipList>
          )}

          <button
            type="button"
            onClick={() => openFullInbox()}
            className="flex items-center justify-center gap-1.5 rounded-md pt-1 text-[11px] font-medium text-cyan-ink hover:underline"
          >
            {t('workbench.inbox.viewAll')}
            <ArrowRight className="size-3" />
          </button>
        </>
      )}
    </PopoverContent>
  );
};
