import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowRight, CheckCheck, ChevronDown, Filter, Inbox, MessageSquareReply, RefreshCw, Search } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { useBots } from '../../context/BotsContext';
import { useNotificationPreferences } from '../../context/NotificationPreferencesContext';
import { useBotRoster } from '../../context/BotRosterContext';
import { inboxCategory, isCategoryLoud, type InboxCategory } from '../../lib/notificationPreferences';
import type { InboxSession } from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import { InboxPreview } from './InboxPreview';
import { Button } from '../ui/button';
import { NotificationMenu } from './NotificationMenu';
import { useNotificationMode } from './useNotificationMode';
import { InboxSectionHeader } from './InboxSectionHeader';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { useInboxScrollRestoration } from './useInboxScrollRestoration';
import { useChatPrefetch } from './useChatPrefetch';
import { InboxCardsSkeleton } from './listSkeletons';
import { Skeleton } from '../ui/skeleton';
import { useSkeletonHandover } from '../ui/skeleton-handover';
import { BotAvatar } from '../ui/bot-avatar';
import { BotTag } from './bots/BotTag';
import {
  readInboxFilter,
  writeInboxFilter,
  resolveInboxFilter,
  INBOX_REVERT_AFTER_MS,
  type InboxFilter as FilterMode,
} from '../../lib/inboxFilterMemory';

export const InboxPage: React.FC<{ onOpenSearch: () => void }> = ({ onOpenSearch }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const listRef = useRef<HTMLDivElement>(null);
  const { enabled: hasBots, botForSession, displayName: botName } = useBots();
  const { preferences } = useNotificationPreferences();
  const { roster } = useBotRoster();
  const { mode: notification, icon: NotificationIcon } = useNotificationMode();
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const {
    capabilities,
  } = useInstanceAuthorization();
  const {
    inboxSessions,
    unreadBySession,
    totalUnread,
    unreadSessions,
    unreadByCategory,
    nextCursor,
    loading,
    loadingMore,
    unreadLoaded,
    refresh,
    loadMore,
    markRead,
  } = useWorkbenchInbox();
  // Refresh spins only for the refresh the reader asked for: the first load and the
  // feed's own revalidations are not an action they are waiting on.
  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = () => {
    setRefreshing(true);
    void refresh().finally(() => setRefreshing(false));
  };
  // Warm the conversations the reader is most likely to open next.
  useChatPrefetch(inboxSessions, !loading);
  // Remember the tab across visits instead of always snapping back to Unread.
  // On (re)entry resume the last tab, unless the user has been away long enough
  // that defaulting to Unread is more useful (see resolveInboxFilter).
  const [filter, setFilterState] = useState<FilterMode>(() => resolveInboxFilter(readInboxFilter(), Date.now()));

  const setFilter = useCallback((next: FilterMode) => {
    setFilterState(next);
    // Persist the choice immediately; keep leftAt (it's stamped on leave below).
    writeInboxFilter(next, readInboxFilter().leftAt);
  }, []);

  // Entering /inbox while this page is already mounted — the user is on the inbox
  // and something navigates to it again — keeps the same element, so the resolve
  // in the initializer above never runs a second time and the tab would simply
  // not move. Re-entry has to mean what entry means, so redo exactly what mount
  // does: resolve from the store, and align the store with what is now shown.
  // Keyed on location.key, which changes once per navigation and not on renders.
  const entryKeyRef = useRef(location.key);
  useEffect(() => {
    if (entryKeyRef.current === location.key) return;
    entryKeyRef.current = location.key;
    const resumed = resolveInboxFilter(readInboxFilter(), Date.now());
    setFilterState(resumed);
    writeInboxFilter(resumed, 0);
  }, [location.key]);

  // Stamp when the user leaves the inbox (route change → unmount, app/tab
  // backgrounded, reload/close) so the next entry can measure the absence. If
  // they background the page itself for a long time, revert to Unread on return.
  useEffect(() => {
    // On entry, align storage with the tab actually shown (persists a
    // revert-to-Unread) and clear leftAt — the user is present, not away, so a
    // brief background-and-return isn't measured against a stale pre-entry stamp.
    writeInboxFilter(filter, 0);
    const markLeft = () => writeInboxFilter(readInboxFilter().tab, Date.now());
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        markLeft();
      } else if (document.visibilityState === 'visible') {
        const { tab, leftAt } = readInboxFilter();
        if (leftAt === 0) return; // already marked present
        if (Date.now() - leftAt > INBOX_REVERT_AFTER_MS) {
          setFilterState('unread');
          writeInboxFilter('unread', 0);
        } else {
          // Returned within the window → present again; clear the stale stamp so a
          // later kill-without-pagehide can't measure against this old background time.
          writeInboxFilter(tab, 0);
        }
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', markLeft);
    return () => {
      markLeft();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', markLeft);
    };
    // Mount/unmount only: listeners are stable and `filter` is read once on entry.
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
  }, []);

  // The unread map is the single source of truth (loaded pagination-independent
  // and kept in sync by realtime upserts + mark-read). A session drops out of
  // the map once its count hits zero, so a missing key means 0 — never fall
  // back to the card's own (now stale) unread_count, or a marked-read session
  // would stay badged / stuck in the Unread tab.
  const unreadOf = (s: InboxSession) => unreadBySession[s.session_id] ?? 0;

  const visible = useMemo(() => {
    if (filter === 'all') return inboxSessions;
    return inboxSessions.filter((s) => unreadOf(s) > 0);
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
  }, [filter, inboxSessions, unreadBySession]);

  const capturePosition = useInboxScrollRestoration(listRef, location.key, filter, visible);

  const openSession = (s: InboxSession) => {
    capturePosition();
    navigate(`/chat/${encodeURIComponent(s.session_id)}`);
  };

  const onMarkAllRead = async () => {
    const ids = Object.entries(unreadBySession)
      .filter(([, n]) => (n || 0) > 0)
      .map(([id]) => id);
    await Promise.all(ids.map((id) => markRead(id)));
  };

  // One conversation card. In a muted category an unread card stays marked but
  // drops the mint that asks for attention, and its count turns grey. Inside the
  // Bots section the header already says Bot, so the card carries no Bot tag.
  const renderCard = (s: InboxSession, quiet: boolean, inBotSection: boolean) => {
    const unread = unreadOf(s);
    const projectLabel = s.project_name || s.project_id || 'avibe';
    const sessionLabel = s.title?.trim() || s.session_id;
    // A Bot's conversation reads as the Bot: avatar, name, and a small Bot
    // tag in place of "project · title" (docs/plans/bots.md D5).
    const bot = botForSession(s.session_id);
    return (
      <article
        key={s.session_id}
        data-inbox-session-id={s.session_id}
        className={clsx(
          'flex flex-col gap-3 rounded-xl border p-4 transition',
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
              <BotAvatar avatar={bot.avatar} name={botName(bot)} size={24} inList />
              <span className="min-w-0 truncate text-[13px] font-semibold text-foreground">{botName(bot)}</span>
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
                'line-clamp-3 text-[13px] leading-relaxed',
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
            onClick={() => openSession(s)}
            className="inline-flex items-center gap-1.5 rounded-md border border-mint/30 bg-mint/[0.06] px-3 py-1.5 text-[11px] font-semibold text-mint-ink transition hover:bg-mint/[0.12]"
          >
            {unread > 0 && (
              <span
                className={clsx(
                  'inline-flex min-w-[1.1rem] items-center justify-center rounded-full px-1.5 font-mono text-[9px] font-bold',
                  quiet ? 'bg-foreground/[0.18] text-foreground' : 'bg-mint text-primary-foreground',
                )}
              >
                {unread > 99 ? '99+' : unread}
              </span>
            )}
            {t('workbench.inbox.openSession')}
            <ArrowRight className="size-3" />
          </button>
          {capabilities.can_chat && unread > 0 && (
            <button
              type="button"
              onClick={() => markRead(s.session_id)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border-strong px-3 py-1.5 text-[11px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
            >
              <CheckCheck className="size-3" />
              {t('workbench.inbox.markRead')}
            </button>
          )}
        </div>
      </article>
    );
  };

  // With Bots the feed splits into two sections, Bots first
  // (docs/plans/workbench-refinements.md §1); each keeps the feed's activity order.
  // A section with nothing to show is left out. Without Bots there is one list.
  const sections = useMemo<{ category?: InboxCategory; rows: InboxSession[] }[]>(() => {
    if (!hasBots) return [{ rows: visible }];
    return (['bot', 'session'] as const)
      .map((category) => ({
        category,
        rows: visible.filter((row) => inboxCategory(row.session_id, roster.sessionIds) === category),
      }))
      .filter((section) => section.rows.length > 0);
  }, [hasBots, roster.sessionIds, visible]);

  const hasMore = !!nextCursor;
  // Only declare "all clear" when nothing is visible AND nothing left to load
  // could match. On the Unread tab that means no unread sessions exist anywhere
  // (unreadSessions is pagination-independent) — otherwise unread sessions on a
  // later page would be hidden behind a false "all caught up". On All it means
  // there are simply no more pages.
  // Nothing known yet: no rows (from the server or the local snapshot) and no
  // answer that said there are none. Skeleton cards, not an empty state that the
  // first page would then replace.
  const showSkeleton = inboxSessions.length === 0 && (loading || !unreadLoaded);
  const cardsHandover = useSkeletonHandover(showSkeleton);
  const showEmpty = !showSkeleton && visible.length === 0 && (filter === 'unread' ? unreadSessions === 0 : !hasMore);

  return (
    <div ref={listRef} className="mx-auto flex w-full max-w-4xl flex-col gap-6 pb-2 md:pt-2">
      {/* Header. A phone's tab page has none: the brand header and the tab bar say
          where you are, and Refresh rides at the end of the toolbar below. */}
      <div className="hidden flex-wrap items-center gap-4 md:flex">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint-ink shadow-glow-md-mint">
          <Inbox className="size-5" />
        </div>
        {/* No eyebrow: it only repeated the title. */}
        <div className="flex flex-1 flex-col">
          <h1 className="text-2xl font-bold text-foreground">{t('workbench.inbox.title')}</h1>
          {/* Counts that are not known yet are not zero. */}
          {showSkeleton ? (
            <span className="flex h-[1lh] items-center text-[13px]">
              <Skeleton className="h-2 w-28" />
            </span>
          ) : (
            <p className="text-[13px] text-muted">
              {t('workbench.inbox.headerCount', { unread: unreadSessions, total: inboxSessions.length })}
            </p>
          )}
        </div>
        <div className="flex w-full justify-end sm:w-auto">
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className={clsx(
              'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-medium transition',
              refreshing
                ? 'cursor-wait border-border bg-foreground/[0.02] text-muted'
                : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
            )}
          >
            <RefreshCw className={clsx('size-3.5', refreshing && 'animate-spin')} />
            {t('workbench.inbox.refresh')}
          </button>
        </div>
      </div>

      {/* Mobile search entry: the search bar a phone app keeps at the top of a list
          (iOS, WeChat, Telegram, Lark) -- a 36px rounded field one shade off the page,
          no border or shadow, that opens the full-screen search page (ui/design:
          inbox/inbox-page/phone). It scrolls away with the list: search is an
          occasional act, and the list keeps the screen. Desktop searches via the
          sidebar field and ⌘K (md:hidden). */}
      <Button
        type="button"
        variant="ghost"
        onClick={onOpenSearch}
        className="h-9 w-full justify-start gap-2 rounded-[10px] bg-foreground/[0.06] px-3 text-left font-normal hover:bg-foreground/[0.09] md:hidden"
      >
        <Search className="size-4 shrink-0 text-muted" />
        <span className="flex-1 truncate text-[14px] text-muted">
          {t('workbench.search.entryAll')}
        </span>
      </Button>

      {/* Toolbar */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-2">
        <div className="inline-flex w-full items-center gap-1 rounded-lg border border-border-strong bg-surface-2 p-0.5 sm:w-auto">
          {([
            { key: 'unread', label: t('workbench.inbox.filterUnread') },
            { key: 'all', label: t('workbench.inbox.filterAll') },
          ] as { key: FilterMode; label: string }[]).map(({ key, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => setFilter(key)}
              className={clsx(
                'flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-[12px] font-semibold transition sm:flex-none',
                filter === key
                  ? 'bg-mint/[0.10] text-mint-ink shadow-glow-xs-mint'
                  : 'text-muted hover:text-foreground',
              )}
            >
              <Filter className="size-3" />
              {label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto sm:justify-end">
          <Popover open={notificationsOpen} onOpenChange={setNotificationsOpen}>
            <PopoverTrigger asChild>
              <button
                type="button"
                className="flex shrink-0 items-center gap-1.5 rounded-md border border-border-strong px-3 py-1.5 text-[12px] font-semibold text-foreground transition hover:bg-foreground/[0.04]"
              >
                <NotificationIcon className={clsx('size-3.5', notification === 'off' && 'text-muted')} />
                {t('workbench.notifications.button', { mode: t(`workbench.notifications.mode.${notification}`) })}
                <ChevronDown className="size-3 text-muted" />
              </button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-[320px] p-3">
              <NotificationMenu onNavigate={() => setNotificationsOpen(false)} />
            </PopoverContent>
          </Popover>
          {capabilities.can_chat && (
            <button
              type="button"
              onClick={onMarkAllRead}
              disabled={totalUnread === 0}
              className={clsx(
                'flex shrink-0 items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-semibold transition',
                totalUnread === 0
                  ? 'cursor-not-allowed border-border bg-foreground/[0.02] text-muted'
                  : 'border-mint/30 bg-mint/[0.06] text-mint-ink hover:bg-mint/[0.12]',
              )}
            >
              <CheckCheck className="size-3.5" />
              {t('workbench.inbox.markAllRead')}
            </button>
          )}
          {/* A phone's Refresh: the feed stays live on its own, so this is the quiet fallback. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onRefresh}
            disabled={loading}
            aria-label={t('workbench.inbox.refresh')}
            title={t('workbench.inbox.refresh')}
            className="ml-auto size-8 text-muted hover:text-foreground md:hidden"
          >
            <RefreshCw className={clsx('size-4', refreshing && 'animate-spin')} />
          </Button>
        </div>
      </div>

      {/* Empty state */}
      {showSkeleton ? (
        <InboxCardsSkeleton rows={3} sections={hasBots} />
      ) : showEmpty ? (
        <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-border bg-surface px-6 py-16 text-center">
          <CheckCheck className="size-8 text-mint-ink" />
          <div className="text-[15px] font-semibold text-foreground">
            {filter === 'unread' ? t('workbench.inbox.allClearTitle') : t('workbench.inbox.emptyTitle')}
          </div>
          <div className="max-w-md text-[12.5px] text-muted">
            {filter === 'unread' ? t('workbench.inbox.allClearBody') : t('workbench.inbox.emptyBody')}
          </div>
        </div>
      ) : (
        <div className={clsx('flex flex-col gap-3', cardsHandover)}>
          {/* Unread tab, current pages hold no unread but more exist further
              down — prompt to load instead of rendering nothing. */}
          {visible.length === 0 && (
            <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12.5px] text-muted">
              {t('workbench.inbox.moreUnreadBeyond')}
            </div>
          )}
          {sections.map((section) => {
            const quiet = section.category ? !isCategoryLoud(preferences, section.category) : false;
            const sectionUnreadIds = section.category
              ? Object.entries(unreadBySession)
                .filter(([id, n]) => n > 0 && inboxCategory(id, roster.sessionIds) === section.category)
                .map(([id]) => id)
              : [];
            return (
              <section key={section.category ?? 'all'} className="flex flex-col gap-3">
                {section.category && (
                  <InboxSectionHeader
                    category={section.category}
                    unread={unreadByCategory[section.category]}
                    quiet={quiet}
                    className="px-1 pt-1"
                    action={
                      section.category === 'session' && capabilities.can_chat && sectionUnreadIds.length > 0 ? (
                        <button
                          type="button"
                          onClick={() => void Promise.all(sectionUnreadIds.map((id) => markRead(id)))}
                          className="inline-flex items-center gap-1 rounded-md border border-border-strong px-2 py-0.5 text-[11px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
                        >
                          <CheckCheck className="size-3" />
                          {t('workbench.inbox.sections.markRead')}
                        </button>
                      ) : null
                    }
                  />
                )}
                {section.rows.map((s) => renderCard(s, quiet, section.category === 'bot'))}
              </section>
            );
          })}

          {/* Load more walks the full feed by activity; the active tab then
              re-derives ``visible``. Shown on both tabs (and even when the
              Unread tab's current page is empty) so unread sessions deeper in
              history stay reachable. */}
          {hasMore && loadingMore && <InboxCardsSkeleton rows={1} />}
          {hasMore && !loadingMore && (
            <button
              type="button"
              onClick={() => loadMore()}
              className="flex items-center justify-center gap-1.5 self-center rounded-md border border-border-strong px-4 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
            >
              {t('workbench.inbox.loadMore')}
            </button>
          )}
        </div>
      )}
    </div>
  );
};
