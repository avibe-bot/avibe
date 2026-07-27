// One row of the 来源 list (frame 01r): drag handle · priority number · icon +
// name/mono-sub (supply tooltip on hover) · fixed-width usage column · billing
// chip · state chip. Presentation-only; drag + reorder live in SourcesCard.
//
// Mobile (< sm): the frame's single aligned row cannot hold eight columns in
// 390px — it squeezed the name to zero width and pushed the state chip and the
// row menu off-screen entirely (clipped, not scrollable, so unreachable). So the
// row stacks into two tiers per design.pen M01 (m01SrcL1 / m01SrcL2): identity +
// the row menu on top, then billing · state · usage on a single second line,
// inset 26px so it starts under the priority number. The sm+ layout is
// byte-for-byte the desktop frame, so the fixed-width chip columns still align
// down the list.
import * as React from 'react';
import { GripVertical } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { BillingChip, ExperimentalChip, StateChip } from './chips';
import { SupplyTooltip } from './SupplyTooltip';
import { SourceRowMenu } from './SourceRowMenu';
import { ACCENT_ICON, ACCENT_TILE, isCustomEndpoint, sourceVisual } from './vendorMeta';
import { cooldownEtaMinutes, formatSpend } from './format';
import type { Source } from './types';

// The mono sub-line shows the source identity: account_label (subscriptions)
// or masked_credential (api keys) — both server-provided display data (contract
// v1.1, 07-23, from the L4 finding). Falls back to the supply channel / endpoint
// (原生供给 / 中枢托管 / 官方地址 / 自定义地址) when neither is set, e.g. hub-held
// experimental sources before a later adapter rev. Cooldown ETA is appended.
function useSubline(source: Source): string {
  const { t } = useTranslation();

  const fallback =
    source.kind === 'subscription'
      ? source.supply_channel === 'native_cli'
        ? (t('settings.models.source.nativeSupply') as string)
        : (t('settings.models.source.hubHosted') as string)
      : isCustomEndpoint(source)
        ? (t('settings.models.source.customEndpoint') as string)
        : (t('settings.models.source.officialEndpoint') as string);

  const parts = [source.account_label ?? source.masked_credential ?? fallback];
  if (source.state.status === 'cooldown') {
    parts.push(t('settings.models.source.retryIn', { minutes: cooldownEtaMinutes(source.state.retry_at) }) as string);
  }
  return parts.join(' · ');
}

// On phones usage sits at the RIGHT END of the second tier and absorbs the slack
// (design.pen M01 m01Use: fill_container + justify end), which also puts it after
// the chips — hence `order-last`. On sm+ it reverts to the desktop frame's
// leading fixed-width column.
const USAGE_CELL = 'order-last flex-1 sm:order-none sm:w-[150px] sm:flex-none';

const UsageCell: React.FC<{ source: Source }> = ({ source }) => {
  const { t } = useTranslation();
  const pct = source.usage?.cycle_used_pct;
  const spend = source.usage?.month_spend_cents;

  if (source.billing === 'monthly' && typeof pct === 'number') {
    return (
      <div className={cn(USAGE_CELL, 'flex items-center justify-end gap-2')}>
        <div className="h-1.5 w-14 overflow-hidden rounded-full bg-border sm:w-[92px]">
          <div className="h-full rounded-full bg-mint" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
        <span className="text-right font-mono text-[10.5px] text-muted sm:w-9 sm:text-[12px]">{Math.round(pct)}%</span>
      </div>
    );
  }
  if (typeof spend === 'number') {
    return (
      <div className={cn(USAGE_CELL, 'truncate text-right font-mono text-[10.5px] text-muted sm:font-sans sm:text-[12px]')}>
        {t('settings.models.usage.monthSpend', { amount: formatSpend(spend, source.usage?.currency) })}
      </div>
    );
  }
  // No usage data: on mobile the chips just stay left-aligned, so the spacer only
  // exists to hold the desktop column open.
  return <div className="hidden sm:block sm:w-[150px] sm:shrink-0" />;
};

export const SourceRow: React.FC<{
  source: Source;
  priority: number;
  onDragHandlePointerDown: (e: React.PointerEvent) => void;
  /** False at the ends of the list — the matching menu action is disabled. */
  canMoveUp: boolean;
  canMoveDown: boolean;
  /** Keyboard/screen-reader path for reordering; drag stays the primary one. */
  onMove: (delta: -1 | 1) => void;
  /** Re-fetch after a row action (rename / re-discover / delete). */
  onChanged: () => void;
}> = ({ source, priority, onDragHandlePointerDown, canMoveUp, canMoveDown, onMove, onChanged }) => {
  const { t } = useTranslation();
  const { Icon, accent } = sourceVisual(source);
  const subline = useSubline(source);
  const isExperimental = source.kind === 'subscription' && source.supply_channel === 'hub';

  return (
    // One wrapping flex line, not two stacked tiers: the row menu has to sit at
    // the end of the FIRST line on phones and at the end of the (single) row on
    // sm+, which no amount of CSS can do if it lives inside one of two sibling
    // containers. So identity / menu / metrics are siblings here and `order`
    // rearranges them per breakpoint — one menu instance, one set of dialogs.
    <div className="group flex flex-wrap items-center gap-x-2.5 gap-y-2.5 border-b border-border px-4 py-3 last:border-b-0 sm:flex-nowrap sm:gap-x-3 sm:px-5 sm:py-3.5">
      {/* Identity — the leading segment on both breakpoints. */}
      <div className="order-1 flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3">
        <button
          type="button"
          aria-label={t('settings.models.source.reorder') as string}
          onPointerDown={onDragHandlePointerDown}
          // 40px touch target on phones (the 24px desktop handle is unhittable
          // with a thumb); the glyph size is unchanged.
          className="flex size-10 shrink-0 cursor-grab touch-none items-center justify-center rounded text-muted/50 transition-colors hover:text-muted active:cursor-grabbing sm:size-6"
        >
          <GripVertical className="size-4" />
        </button>

        <span className="grid size-[22px] shrink-0 place-items-center rounded-[7px] border border-border bg-surface font-mono text-[11px] font-bold text-muted sm:size-7 sm:rounded-md sm:text-[13px] sm:font-normal">
          {priority}
        </span>

        <SupplyTooltip models={source.models} className="flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3">
          <span className={cn('flex size-[34px] shrink-0 items-center justify-center rounded-[10px] sm:size-11', ACCENT_TILE[accent])}>
            <Icon className={cn('size-[18px] sm:size-[22px]', ACCENT_ICON[accent])} />
          </span>
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate text-[13.5px] font-semibold text-foreground sm:text-[15px]">{source.display_name}</span>
              {isExperimental && <ExperimentalChip />}
            </span>
            <span className="truncate font-mono text-[10.5px] text-muted sm:text-[12px]">{subline}</span>
          </span>
        </SupplyTooltip>
      </div>

      {/* Metric / state strip — a full-width second line on phones, inset 26px
          (handle glyph + gap) so it starts under the priority number; the aligned
          desktop chip columns on sm+.
          flex-wrap here is the safety net, not the plan: measured at 360/390/430
          in both locales every row stays on one line, including the widest English
          combination ("$3.2 this month" · "Metered $" · "Cooling down"). It stays
          wrap-capable so an unusually long future state label degrades to two
          lines instead of clipping. sm:flex-nowrap keeps the desktop row
          single-line. */}
      <div className="order-3 flex w-full flex-wrap items-center gap-x-2 gap-y-2 pl-[26px] sm:order-2 sm:w-auto sm:shrink-0 sm:flex-nowrap sm:gap-4 sm:pl-0">
        <UsageCell source={source} />
        <BillingChip billing={source.billing} currency={source.usage?.currency} />
        <StateChip state={source.state} />
      </div>

      {/* Thumb-reachable on the identity line on phones (design.pen M01 m01More,
          36×36 on the handle/priority axis); trailing the desktop row on sm+.
          sm:ml-1 restores the desktop frame's 16px gap before the menu — it used
          to come from the metrics strip's own gap-4, and the row's gap-3 alone
          pulled every chip column 4px right of the reviewed desktop layout
          (caught by diffing the rendered geometry against master). */}
      <div className="order-2 shrink-0 sm:order-3 sm:ml-1">
        <SourceRowMenu
          source={source}
          priority={priority}
          canMoveUp={canMoveUp}
          canMoveDown={canMoveDown}
          onMove={onMove}
          onChanged={onChanged}
        />
      </div>
    </div>
  );
};
