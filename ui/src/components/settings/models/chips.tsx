// Small shared chips for the Model Hub rows — all built on the ui/Badge
// primitive (reuse ladder) with size/width overrides. Kept in one place so the
// 来源 and Agent bands stay visually consistent and the fixed-width chip
// columns (frame 01r) align across rows.
import * as React from 'react';
import { Hourglass, TriangleAlert, Unplug, Zap } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useTranslation } from 'react-i18next';

import { ACCENT_DOT, type Accent } from './vendorMeta';
import type { AgentMode, SourceState } from './types';

/** Shared status dot used by source supply and recent-switch rows. */
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
 * Billing type — content-width on phones, fixed-width column on sm+.
 */
export const BillingChip: React.FC<{
  billing: 'monthly' | 'metered';
  className?: string;
}> = ({ billing, className }) => {
  const { t } = useTranslation();
  if (billing === 'monthly') {
    return (
      <Badge variant="secondary" className={cn(BILLING_CHIP, 'model-hub-fill-08', className)}>
        {t('settings.models.billing.monthly')}
      </Badge>
    );
  }
  return (
    <Badge variant="warning" className={cn(BILLING_CHIP, className)}>
      {t('settings.models.billing.metered')}
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
 * Managed / Direct — a fixed 104px column on desktop so the pills line up
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
    <Badge variant="secondary" className={cn(base, 'model-hub-fill-08')}>
      <Unplug className="size-3 sm:hidden" />
      {t('settings.models.mode.direct')}
    </Badge>
  );
};
