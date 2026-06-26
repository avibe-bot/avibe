import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, CheckCheck, Filter, Inbox, Loader2, MessageSquareReply, RefreshCw, Search } from 'lucide-react';
import clsx from 'clsx';

import { useWorkbenchInbox } from '../../context/WorkbenchInboxContext';
import type { InboxSession } from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import { Markdown } from '../ui/markdown';
import { Button } from '../ui/button';
import { WebPushControl } from './WebPushControl';
import {
  readInboxFilter,
  writeInboxFilter,
  resolveInboxFilter,
  INBOX_REVERT_AFTER_MS,
  type InboxFilter as FilterMode,
} from '../../lib/inboxFilterMemory';

export const InboxPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const {
    inboxSessions,
    unreadBySession,
    totalUnread,
    unreadSessions,
    nextCursor,
    loading,
    loadingMore,
    refresh,
    loadMore,
    markRead,
  } = useWorkbenchInbox();
  // Remember the tab across visits instead of always snapping back to Unread.
  // On (re)entry resume the last tab, unless the user has been away long enough
  // that defaulting to Unread is more useful (see resolveInboxFilter).
  const [filter, setFilterState] = useState<FilterMode>(() => resolveInboxFilter(readInboxFilter(), Date.now()));

  const setFilter = useCallback((next: FilterMode) => {
    setFilterState(next);
    // Persist the choice immediately; keep leftAt (it's stamped on leave below).
    writeInboxFilter(next, readInboxFilter().leftAt);
  }, []);

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, inboxSessions, unreadBySession]);

  const openSession = (s: InboxSession) => {
    if (unreadOf(s) > 0) markRead(s.session_id);
    navigate(`/chat/${encodeURIComponent(s.session_id)}`);
  };

  const onMarkAllRead = async () => {
    const ids = Object.entries(unreadBySession)
      .filter(([, n]) => (n || 0) > 0)
      .map(([id]) => id);
    await Promise.all(ids.map((id) => markRead(id)));
  };

  const hasMore = !!nextCursor;
  // Only declare "all clear" when nothing is visible AND nothing left to load
  // could match. On the Unread tab that means no unread sessions exist anywhere
  // (unreadSessions is pagination-independent) — otherwise unread sessions on a
  // later page would be hidden behind a false "all caught up". On All it means
  // there are simply no more pages.
  const showEmpty = visible.length === 0 && (filter === 'unread' ? unreadSessions === 0 : !hasMore);

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 py-2">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-2xl border border-mint/30 bg-mint/[0.08] text-mint shadow-[0_0_24px_-6px_rgba(91,255,160,0.5)]">
          <Inbox className="size-5" />
        </div>
        <div className="flex flex-1 flex-col">
          <div className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-mint">
            {t('workbench.inbox.eyebrow')}
          </div>
          <h1 className="text-2xl font-bold text-foreground">{t('workbench.inbox.title')}</h1>
          <p className="text-[13px] text-muted">
            {t('workbench.inbox.headerCount', { unread: unreadSessions, total: inboxSessions.length })}
          </p>
        </div>
        <div className="flex w-full justify-end sm:w-auto">
          <button
            type="button"
            onClick={() => refresh()}
            disabled={loading}
            className={clsx(
              'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-medium transition',
              loading
                ? 'cursor-wait border-border bg-foreground/[0.02] text-muted'
                : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
            )}
          >
            <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            {t('workbench.inbox.refresh')}
          </button>
        </div>
      </div>

      {/* Mobile search entry — a full-width field-style button that opens the
          full-screen search page (design.pen S5H9R "M · Inbox · Search entry";
          SearchField). Mobile-only: desktop searches via the sidebar field + ⌘K
          (md:hidden). */}
      <Button
        type="button"
        variant="ghost"
        onClick={() => navigate('/search')}
        className="h-auto w-full justify-start gap-2.5 rounded-xl border border-border-strong bg-foreground/[0.04] px-3.5 py-2.5 text-left font-normal transition hover:bg-foreground/[0.06] md:hidden"
      >
        <Search className="size-4 shrink-0 text-muted" />
        <span className="flex-1 truncate text-[14px] text-muted">
          {t('workbench.search.entry')}
        </span>
        <span className="shrink-0 text-[11px] text-muted">{t('workbench.search.scopeAll')}</span>
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
                  ? 'bg-mint/[0.10] text-mint shadow-[0_0_12px_-4px_rgba(91,255,160,0.5)]'
                  : 'text-muted hover:text-foreground',
              )}
            >
              <Filter className="size-3" />
              {label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto sm:justify-end">
          <WebPushControl />
          <button
            type="button"
            onClick={onMarkAllRead}
            disabled={totalUnread === 0}
            className={clsx(
              'flex shrink-0 items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-semibold transition',
              totalUnread === 0
                ? 'cursor-not-allowed border-border bg-foreground/[0.02] text-muted'
                : 'border-mint/30 bg-mint/[0.06] text-mint hover:bg-mint/[0.12]',
            )}
          >
            <CheckCheck className="size-3.5" />
            {t('workbench.inbox.markAllRead')}
          </button>
        </div>
      </div>

      {/* Empty state */}
      {showEmpty ? (
        <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-border bg-surface px-6 py-16 text-center">
          <CheckCheck className="size-8 text-mint" />
          <div className="text-[15px] font-semibold text-foreground">
            {filter === 'unread' ? t('workbench.inbox.allClearTitle') : t('workbench.inbox.emptyTitle')}
          </div>
          <div className="max-w-md text-[12.5px] text-muted">
            {filter === 'unread' ? t('workbench.inbox.allClearBody') : t('workbench.inbox.emptyBody')}
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {/* Unread tab, current pages hold no unread but more exist further
              down — prompt to load instead of rendering nothing. */}
          {visible.length === 0 && (
            <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6 text-center text-[12.5px] text-muted">
              {t('workbench.inbox.moreUnreadBeyond')}
            </div>
          )}
          {visible.map((s) => {
            const unread = unreadOf(s);
            const projectLabel = s.project_name || s.project_id || 'avibe';
            const sessionLabel = s.title?.trim() || s.session_id;
            return (
              <article
                key={s.session_id}
                className={clsx(
                  'flex flex-col gap-3 rounded-xl border p-4 transition',
                  unread > 0
                    ? 'border-mint/30 bg-mint/[0.05] shadow-[0_0_24px_-12px_rgba(91,255,160,0.4)]'
                    : 'border-border bg-surface',
                )}
              >
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="inline-flex max-w-[40%] items-center gap-1 truncate rounded-md border border-border-strong bg-surface-2 px-2 py-0.5 font-semibold text-cyan">
                    {projectLabel}
                  </span>
                  <span className="text-muted">·</span>
                  <span className="flex-1 truncate text-[13px] font-semibold text-foreground">
                    {sessionLabel}
                  </span>
                  {s.replied && (
                    <span className="inline-flex items-center gap-1 rounded-md border border-cyan/30 bg-cyan/[0.08] px-1.5 py-0.5 text-[10px] font-semibold text-cyan">
                      <MessageSquareReply className="size-2.5" />
                      {t('workbench.inbox.replied')}
                    </span>
                  )}
                  <span className="shrink-0 font-mono text-muted">{formatRelativeTime(s.last_activity_at, t)}</span>
                </div>

                <div className="flex flex-col gap-1">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-mint">
                    {t('workbench.inbox.agent')}
                  </div>
                  {s.preview_text ? (
                    <div
                      className={clsx(
                        'line-clamp-3 text-[13px] leading-relaxed',
                        unread > 0 ? 'text-foreground' : 'text-muted',
                      )}
                    >
                      <Markdown content={s.preview_text} interactive={false} className="vr-markdown--preview" />
                    </div>
                  ) : (
                    <p className="text-[13px] leading-relaxed text-muted">—</p>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => openSession(s)}
                    className="inline-flex items-center gap-1.5 rounded-md border border-mint/30 bg-mint/[0.06] px-3 py-1.5 text-[11px] font-semibold text-mint transition hover:bg-mint/[0.12]"
                  >
                    {unread > 0 && (
                      <span className="inline-flex min-w-[1.1rem] items-center justify-center rounded-full bg-mint px-1.5 font-mono text-[9px] font-bold text-[#080812]">
                        {unread > 99 ? '99+' : unread}
                      </span>
                    )}
                    {t('workbench.inbox.openSession')}
                    <ArrowRight className="size-3" />
                  </button>
                  {unread > 0 && (
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
          })}

          {/* Load more walks the full feed by activity; the active tab then
              re-derives ``visible``. Shown on both tabs (and even when the
              Unread tab's current page is empty) so unread sessions deeper in
              history stay reachable. */}
          {hasMore && (
            <button
              type="button"
              onClick={() => loadMore()}
              disabled={loadingMore}
              className={clsx(
                'flex items-center justify-center gap-1.5 self-center rounded-md border px-4 py-2 text-[12px] font-medium transition',
                loadingMore
                  ? 'cursor-wait border-border bg-foreground/[0.02] text-muted'
                  : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
              )}
            >
              {loadingMore && <Loader2 className="size-3.5 animate-spin" />}
              {t('workbench.inbox.loadMore')}
            </button>
          )}
        </div>
      )}
    </div>
  );
};
