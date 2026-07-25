// One row of the 来源 list (frame 01r): drag handle · priority number · icon +
// name/mono-sub (supply tooltip on hover) · fixed-width usage column · billing
// chip · state chip. Presentation-only; drag + reorder live in SourcesCard.
//
// Mobile (< sm): the frame's single aligned row cannot hold eight columns in
// 390px — it squeezed the name to zero width and pushed the state chip and the
// row menu off-screen entirely (clipped, not scrollable, so unreachable). So the
// row stacks into two tiers: identity on top (handle · priority · icon · name),
// the metric/state strip below with the menu pushed to the right edge. The sm+
// layout is byte-for-byte the desktop frame, so the fixed-width chip columns
// still align down the list.
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

const UsageCell: React.FC<{ source: Source }> = ({ source }) => {
  const { t } = useTranslation();
  const pct = source.usage?.cycle_used_pct;
  const spend = source.usage?.month_spend_cents;

  if (source.billing === 'monthly' && typeof pct === 'number') {
    return (
      <div className="flex shrink-0 items-center gap-2 sm:w-[150px] sm:justify-end">
        <div className="h-1.5 w-[92px] overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full bg-mint" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
        <span className="w-9 text-right font-mono text-[12px] text-muted">{Math.round(pct)}%</span>
      </div>
    );
  }
  if (typeof spend === 'number') {
    return (
      <div className="shrink-0 text-[12px] text-muted sm:w-[150px] sm:text-right">
        {t('settings.models.usage.monthSpend', { amount: formatSpend(spend, source.usage?.currency) })}
      </div>
    );
  }
  // No usage data: on mobile the spacer would eat the strip's width, so it only
  // holds the desktop column open.
  return <div className="hidden sm:block sm:w-[150px] sm:shrink-0" />;
};

export const SourceRow: React.FC<{
  source: Source;
  priority: number;
  onDragHandlePointerDown: (e: React.PointerEvent) => void;
  /** Re-fetch after a row action (rename / re-discover / delete). */
  onChanged: () => void;
}> = ({ source, priority, onDragHandlePointerDown, onChanged }) => {
  const { t } = useTranslation();
  const { Icon, accent } = sourceVisual(source);
  const subline = useSubline(source);
  const isExperimental = source.kind === 'subscription' && source.supply_channel === 'hub';

  return (
    <div className="group flex flex-col gap-2 border-b border-border px-4 py-3 last:border-b-0 sm:flex-row sm:items-center sm:gap-3 sm:px-5 sm:py-3.5">
      {/* Identity tier — the whole row on sm+. */}
      <div className="flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3">
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

        <span className="grid size-7 shrink-0 place-items-center rounded-md border border-border bg-surface font-mono text-[13px] text-muted">
          {priority}
        </span>

        <SupplyTooltip models={source.models} className="flex min-w-0 flex-1 items-center gap-3">
          <span className={cn('flex size-11 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
            <Icon size={22} className={ACCENT_ICON[accent]} />
          </span>
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate text-[15px] font-semibold text-foreground">{source.display_name}</span>
              {isExperimental && <ExperimentalChip />}
            </span>
            <span className="truncate font-mono text-[12px] text-muted">{subline}</span>
          </span>
        </SupplyTooltip>
      </div>

      {/* Metric / state strip — its own line on phones, with the row menu pinned
          to the right edge; the aligned desktop chip columns on sm+. The 88px
          inset is the identity tier's handle + priority + gaps, so the strip
          starts under the source's icon tile and reads as part of that row
          rather than floating between two of them. */}
      <div className="flex items-center gap-3 pl-[88px] sm:shrink-0 sm:gap-4 sm:pl-0">
        <UsageCell source={source} />
        <BillingChip billing={source.billing} />
        <StateChip state={source.state} />
        <div className="ml-auto sm:ml-0">
          <SourceRowMenu source={source} onChanged={onChanged} />
        </div>
      </div>
    </div>
  );
};
