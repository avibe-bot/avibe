// Small shared chips for the Model Hub rows — all built on the ui/Badge
// primitive (reuse ladder) with size/width overrides. Kept in one place so the
// 来源 and Agent bands stay visually consistent and the fixed-width chip
// columns (frame 01r) align across rows.
import * as React from 'react';
import { FlaskConical, Hourglass, TriangleAlert, Unplug, Zap } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useTranslation } from 'react-i18next';

import { currencySymbol } from './format';
import { ACCENT_DOT, type Accent } from './vendorMeta';
import type { AgentMode, SourceState } from './types';

/** Status dot used in the ● 当前 chip and the 最近切换 list. */
export const Dot: React.FC<{ accent: Accent; className?: string }> = ({ accent, className }) => (
  <span className={cn('inline-block size-1.5 shrink-0 rounded-full', ACCENT_DOT[accent], className)} aria-hidden />
);

/**
 * Source-row chip metrics. On phones the row's second tier has to fit billing +
 * state + usage on ONE line (design.pen M01 m01SrcL2), so the chips shrink to
 * content-width pills — the desktop frame's fixed-width columns are what made
 * that line overflow. From sm+ the fixed widths come back so the chip columns
 * still align down the list, which is the whole point of them on a wide screen.
 */
const CHIP_MOBILE = 'rounded-full py-1';
const BILLING_CHIP = cn(CHIP_MOBILE, 'font-medium sm:w-[58px] sm:justify-center');

/**
 * 包月 / 按量 $ — content-width on phones, fixed-width column on sm+.
 *
 * The metered symbol tracks the source's REPORTED currency (USD when absent).
 * A static `$` contradicted the usage cell for a source that genuinely reports
 * CNY/EUR — it would read `按量 $` beside `¥12.4`. The symbol still comes from
 * format.ts's single map, never hand-written here; an unmappable code drops the
 * symbol rather than printing a wrong one (the amount cell carries the truth).
 */
export const BillingChip: React.FC<{
  billing: 'monthly' | 'metered';
  currency?: string | null;
  className?: string;
}> = ({ billing, currency, className }) => {
  const { t } = useTranslation();
  if (billing === 'monthly') {
    return (
      <Badge variant="secondary" className={cn(BILLING_CHIP, 'bg-foreground/[0.03]', className)}>
        {t('settings.models.billing.monthly')}
      </Badge>
    );
  }
  const symbol = currencySymbol(currency);
  return (
    <Badge variant="warning" className={cn(BILLING_CHIP, className)}>
      {symbol
        ? t('settings.models.billing.meteredWithSymbol', { symbol })
        : t('settings.models.billing.metered')}
    </Badge>
  );
};

/**
 * 暂不可用 / 需要处理 / 不可用 — nothing at all while the source is healthy.
 *
 * A source that is serving normally (active / standby) draws NO chip in V6:
 * health surfaces as an exception, not as a per-row label (design.pen 「产品改造
 * V6 01」 — only the cooling relay carries one). Callers that reserve an aligned
 * column ask `isUnhealthy(state)` (supply.ts) to tell an empty cell from a
 * missing one.
 */
export const StateChip: React.FC<{ state: SourceState }> = ({ state }) => {
  const { t } = useTranslation();
  const base = cn(CHIP_MOBILE, 'sm:w-[86px] sm:justify-center');
  switch (state.status) {
    case 'cooldown':
      return (
        <Badge variant="warning" className={base}>
          <Hourglass className="size-3" />
          {t('settings.models.state.cooldown')}
        </Badge>
      );
    case 'needs_action':
      return (
        <Badge variant="warning" className={base}>
          <TriangleAlert className="size-3" />
          {t('settings.models.state.needs_action')}
        </Badge>
      );
    case 'error':
      return (
        <Badge variant="destructive" className={base}>
          {t('settings.models.state.error')}
        </Badge>
      );
    case 'active':
    case 'standby':
    default:
      return null;
  }
};

/**
 * ● 当前 — the source this Agent is resolved onto right now (§4.3), which is a
 * per-Agent fact: the same source is 当前 for one backend and merely enabled for
 * another. Sits in the state chip's column, and the two are mutually exclusive by
 * construction — a source that isn't serving normally can't be the current one.
 */
export const CurrentChip: React.FC = () => {
  const { t } = useTranslation();
  return (
    <Badge variant="success" className={cn(CHIP_MOBILE, 'text-[10px] sm:text-[11px]')}>
      <Dot accent="mint" />
      {t('settings.models.current')}
    </Badge>
  );
};

/**
 * 中枢 Hub / 直连 Direct — a fixed 104px column on desktop so the pills line up
 * down the Agent list (V6 01), content-width beside the name on a phone (M01).
 *
 * Direct carries the `unplug` glyph on mobile only: the desktop frame drops it
 * because the pill sits in a labelled column, while the mobile row has it inline
 * next to the Agent name where a bare word reads as part of the title.
 */
export const ModeChip: React.FC<{ mode: AgentMode }> = ({ mode }) => {
  const { t } = useTranslation();
  const base = 'h-[26px] rounded-full px-2.5 text-[11px] sm:w-[104px] sm:justify-center';
  return mode === 'hub' ? (
    <Badge variant="success" className={cn(base, 'font-bold')}>
      <Zap className="size-3 sm:size-[11px]" />
      {t('settings.models.mode.hub')}
    </Badge>
  ) : (
    <Badge variant="secondary" className={cn(base, 'bg-foreground/[0.03]')}>
      <Unplug className="size-3 sm:hidden" />
      {t('settings.models.mode.direct')}
    </Badge>
  );
};

/** 实验 — marks a consent-gated hub-held subscription source. */
export const ExperimentalChip: React.FC = () => {
  const { t } = useTranslation();
  return (
    <Badge variant="warning" className="rounded-md px-2 py-0.5 text-[10px] font-medium">
      <FlaskConical className="size-3" />
      {t('settings.models.experimental')}
    </Badge>
  );
};

