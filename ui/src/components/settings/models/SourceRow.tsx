// One row of the 来源 list (design.pen 「产品改造 V6 01」): icon + name/mono-sub
// (supply tooltip on hover) · fixed-width usage column · billing chip · state
// column · overflow menu. Presentation-only.
//
// V6 removed the drag handle and the priority number: a source is an asset here,
// and ordering is a per-Agent property that lives in the 来源顺序 drawer. The
// state column is still reserved on sm+ even though a healthy source draws no
// chip in V6, so the billing column stays aligned down the list.
//
// Mobile (< sm): the frame's single aligned row cannot hold that many columns in
// 390px — it squeezed the name to zero width and pushed the state chip and the
// row menu off-screen entirely (clipped, not scrollable, so unreachable). So the
// row stacks into two tiers per design.pen M01 (m01SrcL1 / m01SrcL2): identity +
// the row menu on top, then billing · state · usage on a single second line,
// indented to match the frame. The sm+ layout is byte-for-byte the desktop
// frame, so the fixed-width chip columns still align down the list.
import * as React from 'react';
import { Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { BillingChip, ExperimentalChip, StateChip } from './chips';
import { SupplyTooltip } from './SupplyTooltip';
import { SourceRowMenu, type RaisedRepair } from './SourceRowMenu';
import { ACCENT_ICON, ACCENT_TILE, isCustomEndpoint, sourceVisual } from './vendorMeta';
import { cooldownEtaMinutes, formatSpend } from './format';
import { isUnhealthy, needsAttention } from './supply';
import type { Source } from './types';

// The mono sub-line shows the source identity: account_label (subscriptions)
// or masked_credential (api keys) — both server-provided display data (contract
// v1.1, 07-23, from the L4 finding). Falls back to the supply channel / endpoint
// (原生供给 / 中枢托管 / 官方地址 / 自定义地址) when neither is set, e.g. hub-held
// experimental sources before a later adapter rev.
//
// A custom endpoint is named even when an identity exists (V6 01's relay row
// reads `key …9c1 · 自定义地址 · 47 分后重试`): a relay is a materially different
// thing to route through, while 官方地址 / 原生供给 / 中枢托管 beside an identity
// is just noise.
//
// An unhealthy source appends WHY and (when it heals itself) WHEN, which is the
// whole of V6 04's amber sub-line 「me@gmail.com · 本周期额度用完 · 明天 08:00
// 恢复」. `detail_key` is already a full i18n key in the frozen contract, so it
// is translated as-is — the UI never maps upstream text to copy of its own.
function useSubline(source: Source): string {
  const { t } = useTranslation();

  const customEndpoint = source.kind === 'api_key' && isCustomEndpoint(source);
  const channel =
    source.kind === 'subscription'
      ? source.supply_channel === 'native_cli'
        ? (t('settings.models.source.nativeSupply') as string)
        : (t('settings.models.source.hubHosted') as string)
      : customEndpoint
        ? (t('settings.models.source.customEndpoint') as string)
        : (t('settings.models.source.officialEndpoint') as string);

  const identity = source.account_label ?? source.masked_credential ?? null;
  const parts = identity ? [identity] : [channel];
  if (identity && customEndpoint) parts.push(channel);
  if (isUnhealthy(source.state) && source.state.detail_key) parts.push(t(source.state.detail_key) as string);
  if (source.state.status === 'cooldown') {
    parts.push(t('settings.models.source.retryIn', { minutes: cooldownEtaMinutes(source.state.retry_at) }) as string);
  }
  return parts.join(' · ');
}

// On phones usage sits at the RIGHT END of the second tier and absorbs the slack
// (design.pen M01 m01Use: fill_container + justify end), which also puts it after
// the chips — hence `order-last`. On sm+ it reverts to the desktop frame's
// leading fixed-width column.
const USAGE_CELL = 'order-last flex-1 sm:order-none sm:w-[130px] sm:flex-none';

const UsageCell: React.FC<{ source: Source }> = ({ source }) => {
  const { t } = useTranslation();
  const pct = source.usage?.cycle_used_pct;
  const spend = source.usage?.month_spend_cents;

  if (source.billing === 'monthly' && typeof pct === 'number') {
    const clamped = Math.min(100, Math.max(0, pct));
    return (
      <div className={cn(USAGE_CELL, 'flex items-center justify-end gap-2')}>
        <div className="h-[5px] w-14 overflow-hidden rounded-[3px] bg-foreground/[0.07] sm:w-16">
          {/* Gold at a full cycle: the bar itself carries the 「用完了」 the amber
              sub-line spells out, so a glance down the column finds it. */}
          <div
            className={cn('h-full rounded-[3px]', clamped >= 100 ? 'bg-gold' : 'bg-mint')}
            style={{ width: `${clamped}%` }}
          />
        </div>
        <span className="w-8 text-right font-mono text-[10.5px] text-muted sm:text-[11px]">{Math.round(pct)}%</span>
      </div>
    );
  }
  if (typeof spend === 'number') {
    return (
      <div className={cn(USAGE_CELL, 'truncate text-right font-mono text-[10.5px] text-muted sm:text-[11px]')}>
        {t('settings.models.usage.monthSpend', { amount: formatSpend(spend, source.usage?.currency) })}
      </div>
    );
  }
  // No usage data: on mobile the chips just stay left-aligned, so the spacer only
  // exists to hold the desktop column open.
  return <div className="hidden sm:block sm:w-[130px] sm:shrink-0" />;
};

// V6 draws a state chip only when the source is NOT serving normally, so the
// healthy majority of rows stays quiet. The column is still held open on sm+ —
// otherwise the one cooling row would push its own billing chip 86px left of
// every other row's.
const StateCell: React.FC<{ source: Source }> = ({ source }) => (
  // sm:ml-1.5 is the 6px the frame gets from right-aligning an 86px chip inside a
  // 92px column — spent as a margin here so the row keeps one flat flex line.
  <div className={cn('sm:ml-1.5 sm:w-[86px] sm:shrink-0', !isUnhealthy(source.state) && 'hidden sm:block')}>
    <StateChip state={source.state} />
  </div>
);

export const SourceRow: React.FC<{
  source: Source;
  /** Re-fetch after a row action (rename / re-discover / delete). */
  onChanged: () => void;
  /** Open a repair journey the page hosts — see SourceRowMenu's `onRepair`. */
  onRepair?: (source: Source, kind: RaisedRepair) => void;
  /** Open the shared manual-model action with this source preselected. */
  onAddModel: (source: Source) => void;
}> = ({ source, onChanged, onRepair, onAddModel }) => {
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
    <div className="group border-b border-border last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2.5 px-4 py-3 sm:flex-nowrap sm:gap-x-3.5 sm:px-5 sm:py-3">
      {/* Identity — the leading segment on both breakpoints. */}
      <div className="order-1 flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3.5">
        <SupplyTooltip models={source.models} className="flex min-w-0 flex-1 items-center gap-2.5 sm:gap-3.5">
          <span className={cn('flex size-[34px] shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
            <Icon className={cn('size-4', ACCENT_ICON[accent])} />
          </span>
          <span className="flex min-w-0 flex-col gap-0.5 sm:gap-[3px]">
            <span className="flex min-w-0 items-center gap-2">
              <span className="truncate text-[13.5px] font-semibold text-foreground">{source.display_name}</span>
              {isExperimental && <ExperimentalChip />}
            </span>
            {/* Gold when the outage is waiting on a person (V6 04's exhausted
                subscription), muted when it will heal on its own (V6 01's timed-out
                relay) — see `needsAttention`. */}
            <span
              className={cn(
                'truncate font-mono text-[10.5px] sm:text-[11px]',
                needsAttention(source.state) ? 'text-gold' : 'text-muted',
              )}
            >
              {subline}
            </span>
          </span>
        </SupplyTooltip>
      </div>

      {/* Metric / state strip — a full-width second line on phones, indented to
          the V6 M01 frame's second tier; the aligned desktop chip columns on sm+.
          flex-wrap here is the safety net, not the plan: measured at 360/390/430
          in both locales every row stays on one line, including the widest English
          combination ("$3.2 this month" · "Metered $" · "Cooling down"). It stays
          wrap-capable so an unusually long future state label degrades to two
          lines instead of clipping. sm:flex-nowrap keeps the desktop row
          single-line.

          The desktop gaps are the frame's uniform 14px row gap (sm:gap-3.5 here
          and on the row itself), with the 6px each pill is inset by right-aligning
          it in its wider column spent as an sm:ml-1.5 on the pill. */}
      <div className="order-3 flex w-full flex-wrap items-center gap-x-2 gap-y-2 pl-[26px] sm:order-2 sm:w-auto sm:shrink-0 sm:flex-nowrap sm:gap-3.5 sm:pl-0">
        <UsageCell source={source} />
        <BillingChip billing={source.billing} className="sm:ml-1.5" />
        <StateCell source={source} />
      </div>

      {/* Thumb-reachable on the identity line on phones (design.pen M01 m01More,
          36×36), trailing the desktop row on sm+. A flex line rather than a bare
          slot because a stopped row puts its one-tap remedy beside the ⋯ — both
          come from the menu component, which owns the actions. */}
      <div className="order-2 flex shrink-0 items-center gap-1.5 sm:order-3">
        <SourceRowMenu source={source} onChanged={onChanged} onRepair={onRepair} />
      </div>
      </div>

      <div className="flex flex-col gap-2 border-t border-border/70 bg-foreground/[0.012] px-4 py-3 sm:px-5">
        <div className="flex items-center justify-between gap-3">
          <span className="text-[11px] font-semibold text-muted">
            {t('settings.models.sources.modelCount', { count: source.models.length })}
          </span>
          <button
            type="button"
            onClick={() => onAddModel(source)}
            className="inline-flex min-h-8 shrink-0 items-center gap-1 text-[11.5px] font-semibold text-mint transition-colors hover:text-mint/80"
          >
            <Plus className="size-3.5" />
            {t('settings.models.sources.addModel')}
          </button>
        </div>
        {source.models.length === 0 ? (
          <p className="text-[11.5px] text-muted">{t('settings.models.sources.modelsEmpty')}</p>
        ) : (
          <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto pr-1">
            {source.models.map((model) => (
              <span
                key={model.id}
                title={model.display_name || model.id}
                className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1"
              >
                <span className="truncate font-mono text-[10.5px] text-foreground">{model.id}</span>
                {model.provenance === 'manual' && (
                  <Badge variant="secondary" className="shrink-0 px-1 py-0 text-[8.5px]">
                    {t('settings.models.routes.manualBadge')}
                  </Badge>
                )}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
