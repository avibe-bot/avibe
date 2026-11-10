import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Lightbulb, X } from 'lucide-react';

import { useIsDesktop } from '@/lib/useIsDesktop';
import { Button } from '../ui/button';
import type { Tip } from './homeCopy';

const CONTROL_CLASS = 'grid size-7 shrink-0 place-items-center rounded-md text-muted hover:bg-wash hover:text-foreground';
// A horizontal swipe this far (px) on a phone turns to the next or previous tip.
const SWIPE_PX = 40;

/**
 * The day's tip (ui/design: home/home/b-partial-*, c-ready-*): a title, one sentence, and "Try".
 * ‹ › walk the others; × hides tips until tomorrow. One line on a desktop; on a phone the title
 * over the sentence, "Try" and × beside them, and a swipe for the others. `start` is today's tip
 * in `tips`.
 */
export function TipOfTheDay({
  tips,
  start,
  onTry,
  onHide,
}: {
  tips: Tip[];
  start: number;
  onTry: (tip: Tip) => void;
  onHide: () => void;
}) {
  const { t } = useTranslation();
  const desktop = useIsDesktop();
  const [offset, setOffset] = useState(0);
  const touchX = useRef<number | null>(null);
  if (tips.length === 0) return null;
  const tip = tips[(((start + offset) % tips.length) + tips.length) % tips.length];
  const step = (by: number) => setOffset((current) => current + by);
  const tryIt = tip.tryIt && (
    <Button type="button" variant="text" size="compact" onClick={() => onTry(tip)}>
      {t('workbench.home.tips.try')}
    </Button>
  );
  const hide = (
    <button type="button" onClick={onHide} aria-label={t('workbench.home.tips.hide')} title={t('workbench.home.tips.hide')} className={CONTROL_CLASS}>
      <X className="size-4" />
    </button>
  );
  const mark = (
    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-gold-soft text-gold-ink" aria-hidden="true">
      <Lightbulb className="size-4" />
    </span>
  );
  if (!desktop) {
    return (
      <section
        aria-label={t('workbench.home.tips.label')}
        onTouchStart={(event) => { touchX.current = event.touches[0]?.clientX ?? null; }}
        onTouchEnd={(event) => {
          const from = touchX.current;
          touchX.current = null;
          const to = event.changedTouches[0]?.clientX;
          if (from === null || to === undefined || Math.abs(to - from) < SWIPE_PX) return;
          step(to < from ? 1 : -1);
        }}
        className="flex w-full items-center gap-3 rounded-2xl border border-edge bg-surface-2 py-2.5 pl-2.5 pr-1 shadow-elevation-0"
      >
        {mark}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm font-semibold text-foreground">{tip.title}</span>
          <span className="text-xs text-muted">{tip.why}</span>
        </span>
        <span className="flex shrink-0 items-center gap-0.5">
          {tryIt}
          {hide}
        </span>
      </section>
    );
  }
  return (
    <section
      aria-label={t('workbench.home.tips.label')}
      className="flex h-13 w-full items-center gap-3 rounded-2xl border border-edge bg-surface-2 pl-2.5 pr-1.5 shadow-elevation-0"
    >
      {mark}
      <span className="flex min-w-0 flex-1 items-baseline gap-3 text-sm">
        <span className="shrink-0 font-semibold text-foreground">{tip.title}</span>
        <span className="truncate text-muted">{tip.why}</span>
      </span>
      {tryIt}
      <span className="h-5 w-px bg-border" aria-hidden="true" />
      <span className="flex shrink-0 items-center">
        <button type="button" onClick={() => step(-1)} aria-label={t('workbench.home.tips.previous')} title={t('workbench.home.tips.previous')} className={CONTROL_CLASS}>
          <ChevronLeft className="size-4" />
        </button>
        <button type="button" onClick={() => step(1)} aria-label={t('workbench.home.tips.next')} title={t('workbench.home.tips.next')} className={CONTROL_CLASS}>
          <ChevronRight className="size-4" />
        </button>
        {hide}
      </span>
    </section>
  );
}
