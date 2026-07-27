// 最近切换 — the human-readable resolution-event feed (frame 01r). Shows the
// three most recent by default; 查看全部 expands to the full fetched set. Text
// uses the locale-matched human_zh / human_en the adapter already produced.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { Dot } from './chips';
import type { Accent } from './vendorMeta';
import type { ResolutionEvent } from './types';

const COLLAPSED = 3;

function eventAccent(e: ResolutionEvent): Accent {
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

export const RecentSwitchesCard: React.FC<{ events: ResolutionEvent[] }> = ({ events }) => {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const formatTime = useEventTime();
  const zh = i18n.language.startsWith('zh');

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
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
};
