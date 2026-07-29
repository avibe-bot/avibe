// 最近切换 — the human-readable resolution-event feed (design.pen 「产品改造 V6
// 01」). Shows the three most recent by default; 查看全部 expands to the full
// fetched set.
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
import type { Accent } from './vendorMeta';
import type { ResolutionEvent, Source } from './types';

const COLLAPSED = 3;

/**
 * 需处理 — read off the server's own grading rather than re-derived here.
 *
 * `severity` is in the contract precisely as 「Feed and Models-page presentation
 * metadata」, pinned to `action_required` on the needs_action and supply_interrupted
 * branches. Ignoring it left those two rows falling through to the same cyan as an
 * ordinary switch: the one kind of event nobody may scroll past looked like traffic.
 *
 * The kind fallback covers only a journal row written before the field existed —
 * where re-grading an outage as cyan would hide it — and never overrides a severity
 * the server did send.
 */
const isActionRequired = (e: ResolutionEvent): boolean =>
  e.severity === 'action_required' ||
  (e.severity == null && (e.kind === 'needs_action' || e.kind === 'supply_interrupted'));

export function eventAccent(e: ResolutionEvent): Accent {
  // Gold is the page's one attention colour (`needsAttention`'s sub-line, the chain's
  // dot). An action-required event earns it rather than a treatment of its own.
  if (isActionRequired(e)) return 'gold';
  if (e.billing_note === 'entered_metered') return 'gold';
  if (e.kind === 'recover' || e.reason === 'recovery') return 'mint';
  if (e.kind === 'cooldown' || e.kind === 'skip') return 'muted';
  return 'cyan';
}

function useEventTime() {
  const { t } = useTranslation();
  return (ts: string): string => {
    const d = new Date(ts);
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const dayMs = 86_400_000;
    const dayDiff = Math.floor((startOfToday - new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()) / dayMs);
    let day: string;
    if (dayDiff === 0) day = t('settings.models.recent.today') as string;
    else if (dayDiff === 1) day = t('settings.models.recent.yesterday') as string;
    else day = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    return `${day} ${hh}:${mm}`;
  };
}

export const RecentSwitchesCard: React.FC<{
  events: ResolutionEvent[];
  /** Live inventory — read only to tell a still-present source from a deleted one. */
  sources: Source[];
}> = ({ events, sources }) => {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const formatTime = useEventTime();
  const zh = i18n.language.startsWith('zh');
  const liveIds = React.useMemo(() => new Set(sources.map((s) => s.id)), [sources]);
  const namesDeletedSource = (e: ResolutionEvent) =>
    [e.from_source, e.to_source].some((id) => typeof id === 'string' && id !== '' && !liveIds.has(id));

  const shown = expanded ? events : events.slice(0, COLLAPSED);
  const canExpand = events.length > COLLAPSED;

  return (
    <section className="rounded-xl border border-border bg-background">
      <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-4 sm:px-5">
        <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.recent.title')}</h2>
        {canExpand && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex min-h-10 items-center text-[13px] font-medium text-mint transition-colors hover:text-mint/80 sm:min-h-0"
          >
            {expanded ? t('settings.models.recent.collapse') : t('settings.models.recent.viewAll')}
          </button>
        )}
      </div>
      {shown.length === 0 ? (
        <div className="px-4 py-8 text-center sm:px-5 text-[13px] text-muted">{t('settings.models.recent.empty')}</div>
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
                    className="ml-1.5 translate-y-[-1px] bg-foreground/[0.03] px-2 py-0 text-[10px] font-medium"
                  >
                    {t('settings.models.recent.deletedSource')}
                  </Badge>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
};
