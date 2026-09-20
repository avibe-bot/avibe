// 最近切换 — the human-readable resolution-event feed (design.pen 「产品改造 V6
// 01」). Shows the three most recent by default; 查看全部 opens the list and walks
// the endpoint's `before` cursor page by page, so 全部 means the whole feed and not
// just the rows the page happened to fetch first.
//
// AC-18: an event's sentence is rendered VERBATIM from the recorded human_zh /
// human_en. It is a historical record — the source it names may have been renamed
// or deleted since, and re-deriving the wording from today's inventory would
// silently rewrite history (or, worse, blank the row out). What the UI adds is a
// render-time observation, not a rewrite: when a canonical `src_*` endpoint no
// longer resolves against the live sources, the row gets a 已删除 marker so the
// reader knows why looking for that source in the list above will fail. No action
// is offered — there is nothing left to act on.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Dot } from './chips';
import { emptyFeed, eventAccent, type EventFeed } from './eventFeed';
import { localCalendarRelation } from './localCalendar';
import { foldRegionRead, type RegionRead } from './regionRead';
import type { ResolutionEvent, Source } from './types';

const COLLAPSED = 3;

/** Route configuration is visible on the model row; it is not a user event. */
function useEventTime() {
  const { t } = useTranslation();
  return (ts: string): string => {
    const d = new Date(ts);
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const relation = localCalendarRelation(d, now);
    let day: string;
    if (relation === 'today') day = t('settings.models.recent.today') as string;
    else if (relation === 'yesterday') day = t('settings.models.recent.yesterday') as string;
    else day = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    return `${day} ${hh}:${mm}`;
  };
}

export const RecentSwitchesCard: React.FC<{
  events: RegionRead<EventFeed>;
  /** Live inventory — read only to tell a still-present source from a deleted one. */
  sources: RegionRead<Source[]>;
  /** The feed has older pages than the ones held here (`/events` pages by cursor). */
  hasMore?: boolean;
  loadingMore?: boolean;
  /** Fetch the next older page. Required for 查看全部 to mean 全部. */
  onLoadMore?: () => void | Promise<void>;
  onRetry?: () => void | Promise<void>;
}> = ({ events: eventsRead, sources: sourcesRead, hasMore, loadingMore = false, onLoadMore, onRetry }) => {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const formatTime = useEventTime();
  const zh = i18n.language.startsWith('zh');
  const feed = foldRegionRead(eventsRead, {
    loading: () => emptyFeed,
    ready: (data) => data,
    unread: () => emptyFeed,
    degraded: (staleData) => staleData,
  });
  const events = feed.events;
  const sources = foldRegionRead<Source[], Source[]>(sourcesRead, {
    loading: () => [],
    ready: (data) => data,
    unread: () => [],
    degraded: (staleData) => staleData,
  });
  const sourceInventoryCurrent = sourcesRead.kind === 'ready';
  const hasOlder = hasMore ?? !feed.exhausted;
  const liveIds = React.useMemo(() => new Set(sources.map((s) => s.id)), [sources]);
  const namesDeletedSource = (e: ResolutionEvent) =>
    sourceInventoryCurrent && [e.from_source, e.to_source].some((id) => typeof id === 'string' && id !== '' && !liveIds.has(id));

  const visibleEvents = events;
  const shown = expanded ? visibleEvents : visibleEvents.slice(0, COLLAPSED);
  const rawTailId = events.at(-1)?.id ?? '';
  const backfilledTailRef = React.useRef<string | null>(null);

  // Walk older pages until the collapsed feed has three events (or the server
  // says there are no older rows). The raw tail is the generation key: it
  // advances after every useful page and keeps a
  // failed/overlapping response from becoming an automatic retry loop.
  React.useEffect(() => {
    if (eventsRead.kind !== 'ready' || visibleEvents.length >= COLLAPSED || !hasOlder || loadingMore || !onLoadMore) return;
    if (backfilledTailRef.current === rawTailId) return;
    backfilledTailRef.current = rawTailId;
    void onLoadMore();
  }, [eventsRead.kind, hasOlder, loadingMore, onLoadMore, rawTailId, visibleEvents.length]);

  // 查看全部 opens the fetched rows AND asks for the next page, so the label is
  // true the moment it is pressed rather than only for feeds under one page.
  const canExpand = eventsRead.kind === 'ready' && (visibleEvents.length > COLLAPSED || hasOlder);
  const expand = () => {
    setExpanded(true);
    if (hasOlder && !loadingMore) onLoadMore?.();
  };

  return (
    <section className="rounded-xl border border-border bg-background">
      <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-4 sm:px-5">
        <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.recent.title')}</h2>
        {canExpand && (
          <button
            type="button"
            onClick={() => (expanded ? setExpanded(false) : expand())}
            className="model-hub-action-mint inline-flex min-h-10 items-center text-[13px] font-medium transition-colors sm:min-h-0"
          >
            {expanded ? t('settings.models.recent.collapse') : t('settings.models.recent.viewAll')}
          </button>
        )}
      </div>
      {((eventsRead.kind === 'degraded' && eventsRead.cause === 'read_failed') || eventsRead.kind === 'unread') && (
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 text-[12px] text-destructive-ink sm:px-5">
          <span>{t('settings.models.toast.refreshFailed')}</span>
          <button type="button" onClick={() => void onRetry?.()} className="model-hub-action-mint shrink-0 font-semibold">{t('settings.models.upstream.retry')}</button>
        </div>
      )}
      {((eventsRead.kind === 'degraded' && eventsRead.cause === 'read_failed') || eventsRead.kind === 'unread') && shown.length === 0 ? null : shown.length === 0 ? (
        <div className="flex flex-col items-center gap-2 px-4 py-8 text-center text-[13px] text-muted sm:px-5">
          <span>{eventsRead.kind === 'loading' || hasOlder ? t('settings.models.recent.loadingMore') : t('settings.models.recent.empty')}</span>
          {eventsRead.kind === 'ready' && hasOlder && !loadingMore && (
            <button
              type="button"
              onClick={() => void onLoadMore?.()}
              className="model-hub-action-mint min-h-8 font-medium transition-colors"
            >
              {t('settings.models.recent.loadMore')}
            </button>
          )}
        </div>
      ) : (
        <div className="flex flex-col">
          {/* Phones stack the timestamp above the sentence (design.pen M01 m01Ev):
              the desktop 92px time column left the message ~200px at 360, so a
              one-line event wrapped to three or four. */}
          {shown.map((event) => (
            <div
              key={event.id}
              className="flex flex-col gap-1 border-b border-border px-4 py-2.5 last:border-b-0 sm:flex-row sm:items-start sm:gap-3 sm:py-3 sm:px-5"
            >
              <span className="flex items-center gap-2 sm:contents">
                <Dot accent={eventAccent(event)} className="sm:mt-[7px] sm:order-2" />
                <span className="font-mono text-[11px] text-muted sm:order-1 sm:w-[92px] sm:shrink-0 sm:pt-0.5 sm:text-[12px]">
                  {formatTime(event.ts)}
                </span>
              </span>
              <span className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-foreground sm:order-3 sm:text-[13px]">
                {zh ? event.human_zh : event.human_en}
                {namesDeletedSource(event) && (
                  <Badge
                    variant="secondary"
                    className="model-hub-fill-08 ml-1.5 translate-y-[-1px] px-2 py-0 text-[10px] font-medium"
                  >
                    {t('settings.models.recent.deletedSource')}
                  </Badge>
                )}
              </span>
            </div>
          ))}
          {expanded && hasOlder && (
            <button
              type="button"
              onClick={() => void onLoadMore?.()}
              disabled={loadingMore}
              className="model-hub-action-mint min-h-11 border-t border-border px-4 py-3 text-[12.5px] font-medium transition-colors disabled:text-muted sm:px-5 sm:text-[13px]"
            >
              {loadingMore ? t('settings.models.recent.loadingMore') : t('settings.models.recent.loadMore')}
            </button>
          )}
        </div>
      )}
    </section>
  );
};
