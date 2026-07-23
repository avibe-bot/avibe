// One row of the 来源 list (frame 01r): drag handle · priority number · icon +
// name/mono-sub (supply tooltip on hover) · fixed-width usage column · billing
// chip · state chip. Presentation-only; drag + reorder live in SourcesCard.
import * as React from 'react';
import { GripVertical } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { BillingChip, ExperimentalChip, StateChip } from './chips';
import { SupplyTooltip } from './SupplyTooltip';
import { ACCENT_ICON, ACCENT_TILE, VENDOR_OPTIONS, sourceVisual } from './vendorMeta';
import { cooldownEtaMinutes, formatSpend } from './format';
import type { Source } from './types';

// An api_key points at a custom endpoint when its base URL is set and differs
// from the vendor's official one — covers both vendor='custom' (official = null)
// and an official vendor whose prefilled Base URL was edited to a relay.
function isCustomEndpoint(source: Source): boolean {
  if (!source.base_url) return false;
  const official = VENDOR_OPTIONS.find((v) => v.value === source.vendor)?.base_url ?? null;
  return source.base_url !== official;
}

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
      <div className="flex w-[150px] shrink-0 items-center justify-end gap-2">
        <div className="h-1.5 w-[92px] overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full bg-mint" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
        </div>
        <span className="w-9 text-right font-mono text-[12px] text-muted">{Math.round(pct)}%</span>
      </div>
    );
  }
  if (typeof spend === 'number') {
    return (
      <div className="w-[150px] shrink-0 text-right text-[12px] text-muted">
        {t('settings.models.usage.monthSpend', { amount: formatSpend(spend, source.usage?.currency) })}
      </div>
    );
  }
  return <div className="w-[150px] shrink-0" />;
};

export const SourceRow: React.FC<{
  source: Source;
  priority: number;
  onDragHandlePointerDown: (e: React.PointerEvent) => void;
}> = ({ source, priority, onDragHandlePointerDown }) => {
  const { t } = useTranslation();
  const { Icon, accent } = sourceVisual(source);
  const subline = useSubline(source);
  const isExperimental = source.kind === 'subscription' && source.supply_channel === 'hub';

  return (
    <div className="flex items-center gap-3 border-b border-border px-5 py-3.5 last:border-b-0">
      <button
        type="button"
        aria-label={t('settings.models.source.reorder') as string}
        onPointerDown={onDragHandlePointerDown}
        className="flex size-6 shrink-0 cursor-grab touch-none items-center justify-center rounded text-muted/50 transition-colors hover:text-muted active:cursor-grabbing"
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
          <span className="flex items-center gap-2">
            <span className="truncate text-[15px] font-semibold text-foreground">{source.display_name}</span>
            {isExperimental && <ExperimentalChip />}
          </span>
          <span className="truncate font-mono text-[12px] text-muted">{subline}</span>
        </span>
      </SupplyTooltip>

      <div className="flex shrink-0 items-center gap-4">
        <UsageCell source={source} />
        <BillingChip billing={source.billing} />
        <StateChip state={source.state} />
      </div>
    </div>
  );
};
