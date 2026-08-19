// 用量 — the metered-token report over a trailing local-day window
// (`usage-summary.schema.json`).
//
// It is a report and only a report: nothing on this tab feeds resolution,
// admission, or cooldown, and the span it names is the `window_days` the server
// answered with rather than the number the user asked for. The two are the same
// for every option offered here, which is a property `usageProjection.test.ts`
// enforces rather than a coincidence to rely on.
//
// The 額度 half the tab label used to promise is absent on purpose. `cycle_used_pct`
// has exactly one production writer and it writes `None`, so a quota reading would
// be an invention; the tab is named for what it can actually show.
//
// Drawn against `design.pen MS/ConfigPanel → cp-usage-body`. No frame exists for
// this tab on the local surface, so the geometry follows the source table's own
// vocabulary — 12px card radius, 18px gutter, 11px column labels — instead of
// importing a second panel's spacing into the middle of this one. Two deliberate
// departures from that frame: the day series is a column chart because the day
// count is a window parameter and not a fixed five, and the panels stack instead
// of splitting because the table carries nested rows and a trend reads wide.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { SegmentedRadio } from '@/components/ui/segmented';
import { formatCount, formatDayTime, formatPercent } from './format';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import type { UsageByModel, UsageBySource, UsageSummary } from './types';
import {
  USAGE_WINDOW_OPTIONS,
  formatLocalDay,
  modelIdentity,
  sourceIdentity,
  usageCachedInputShare,
  usageDayColumns,
  usageDayIsMetered,
  usageIsEmpty,
  usageReportShortfall,
  usageTotalTokens,
  type UsageDayColumn,
  type UsageIdentity,
  type UsageWindowOption,
} from './usageProjection';

/** A number that reads against a vendor's own console: grouped, never compacted. */
const useCount = () => {
  const { i18n } = useTranslation();
  return (value: number) => formatCount(value, i18n.language);
};

/**
 * A row's own name, or the honest absence of one.
 *
 * A Source that has left the inventory keeps its canonical id, so the reader can
 * still tell which line of the report is which. A model has no displayable
 * identity at all once its label is gone — its ledger key is a digest — so the
 * row says so in words instead of printing the key.
 */
const RowIdentity: React.FC<{ identity: UsageIdentity; goneKey: string }> = ({ identity, goneKey }) => {
  const { t } = useTranslation();
  if (identity.kind === 'label') return <span className="truncate" title={identity.text}>{identity.text}</span>;
  return (
    <span className="flex min-w-0 items-baseline gap-1.5">
      {identity.id !== null && <span className="truncate font-mono" title={identity.id}>{identity.id}</span>}
      <span className="model-hub-usage-gone shrink-0">{t(goneKey)}</span>
    </span>
  );
};

/**
 * One measured cell.
 *
 * The label is carried on the cell rather than only in the header because the
 * header is the first thing that goes when the surface narrows — the row keeps
 * its own labels there, so a stacked cell is still a labelled number instead of
 * an unattributed one.
 */
const Cell: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <span className="model-hub-usage-cell flex items-baseline justify-between gap-2 md:justify-end">
    <span className="model-hub-usage-cell-label md:hidden">{label}</span>
    <span className="min-w-0 truncate">{children}</span>
  </span>
);

const StatCard: React.FC<{ label: string; value: string; note: string }> = ({ label, value, note }) => (
  <div className="model-hub-usage-stat flex flex-col rounded-xl border border-border bg-background">
    <span className="model-hub-usage-stat-label">{label}</span>
    <span className="model-hub-usage-stat-value font-semibold text-foreground">{value}</span>
    <span className="model-hub-usage-stat-note">{note}</span>
  </div>
);

const StatGrid: React.FC<{ summary: UsageSummary }> = ({ summary }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const totals = summary.totals;
  const shortfall = usageReportShortfall(totals);
  const cachedShare = usageCachedInputShare(totals);
  return (
    <div className="grid gap-4 sm:grid-cols-3">
      <StatCard
        label={t('settings.models.usage.tokens.label') as string}
        value={count(usageTotalTokens(totals))}
        note={t('settings.models.usage.tokens.detail', { input: count(totals.input_tokens), output: count(totals.output_tokens) }) as string}
      />
      <StatCard
        label={t('settings.models.usage.requests.label') as string}
        value={count(totals.requests)}
        // A shortfall means reports that never arrived, never capacity left
        // unused, and the copy has to say which of the two it is.
        note={(shortfall > 0
          ? t('settings.models.usage.requests.shortfall', { count: shortfall })
          : t('settings.models.usage.requests.reported')) as string}
      />
      <StatCard
        label={t('settings.models.usage.cached.label') as string}
        value={cachedShare === null ? (t('settings.models.usage.blank') as string) : formatPercent(cachedShare, i18n.language)}
        note={(cachedShare === null
          ? t('settings.models.usage.cached.none')
          : t('settings.models.usage.cached.detail', { cached: count(totals.cached_input_tokens), input: count(totals.input_tokens) })) as string}
      />
    </div>
  );
};

const ModelRow: React.FC<{ model: UsageByModel }> = ({ model }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const share = usageCachedInputShare(model);
  return (
    <div className="model-hub-usage-row model-hub-usage-row--model grid border-t border-border md:items-center">
      <span className="model-hub-usage-model flex min-w-0 items-baseline">
        <RowIdentity identity={modelIdentity(model)} goneKey="settings.models.usage.bySource.goneModel" />
      </span>
      <Cell label={t('settings.models.usage.bySource.col.tokens') as string}>{count(usageTotalTokens(model))}</Cell>
      <Cell label={t('settings.models.usage.bySource.col.requests') as string}>{count(model.requests)}</Cell>
      <Cell label={t('settings.models.usage.bySource.col.cached') as string}>
        {share === null ? (t('settings.models.usage.blank') as string) : formatPercent(share, i18n.language)}
      </Cell>
      <span className="hidden md:block" />
    </div>
  );
};

const SourceRows: React.FC<{ source: UsageBySource }> = ({ source }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const share = usageCachedInputShare(source);
  return (
    <div className="border-b border-border last:border-b-0">
      <div className="model-hub-usage-row grid md:items-center">
        <span className="model-hub-usage-source flex min-w-0 items-baseline font-semibold text-foreground">
          <RowIdentity identity={sourceIdentity(source)} goneKey="settings.models.usage.bySource.goneSource" />
        </span>
        <Cell label={t('settings.models.usage.bySource.col.tokens') as string}>{count(usageTotalTokens(source))}</Cell>
        <Cell label={t('settings.models.usage.bySource.col.requests') as string}>{count(source.requests)}</Cell>
        <Cell label={t('settings.models.usage.bySource.col.cached') as string}>
          {share === null ? (t('settings.models.usage.blank') as string) : formatPercent(share, i18n.language)}
        </Cell>
        <Cell label={t('settings.models.usage.bySource.col.lastMetered') as string}>
          {source.last_metered_at === null ? (t('settings.models.usage.blank') as string) : formatDayTime(source.last_metered_at, i18n.language)}
        </Cell>
      </div>
      {source.models.map((model) => <ModelRow key={model.model_id} model={model} />)}
    </div>
  );
};

const BySourcePanel: React.FC<{ summary: UsageSummary }> = ({ summary }) => {
  const { t } = useTranslation();
  return (
    <section className="model-hub-usage-card overflow-hidden rounded-xl border border-border bg-background">
      <h3 className="model-hub-usage-card-head model-hub-usage-section-title border-b border-border font-semibold text-foreground">
        {t('settings.models.usage.bySource.title')}
      </h3>
      <div className="model-hub-usage-head hidden border-b border-border font-semibold md:grid">
        <span className="truncate">{t('settings.models.usage.bySource.col.source')}</span>
        <span className="flex justify-end truncate">{t('settings.models.usage.bySource.col.tokens')}</span>
        <span className="flex justify-end truncate">{t('settings.models.usage.bySource.col.requests')}</span>
        <span className="flex justify-end truncate">{t('settings.models.usage.bySource.col.cached')}</span>
        <span className="flex justify-end truncate">{t('settings.models.usage.bySource.col.lastMetered')}</span>
      </div>
      {summary.sources.map((source) => <SourceRows key={source.source_id} source={source} />)}
    </section>
  );
};

/**
 * The trend, over every day of the window rather than every day reported.
 *
 * `usageDayColumns` is what densifies it; the track behind each column is what
 * makes the zero-fill legible. A bar of zero height in an empty row is
 * indistinguishable from a missing bar, and the difference — an idle day versus a
 * day the report does not cover — is the whole reason the series is drawn.
 *
 * Which day ran is `usageDayIsMetered`'s answer and never a token total: an
 * upstream can serve a call and report nothing about it, so a window of those has
 * bars to draw and a peak it cannot name.
 */
const ByDayPanel: React.FC<{ summary: UsageSummary }> = ({ summary }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const columns = React.useMemo(() => usageDayColumns(summary), [summary]);
  const peak = columns.reduce<UsageDayColumn | null>((best, column) => (column.tokens > (best?.tokens ?? 0) ? column : best), null);
  const metered = columns.some(usageDayIsMetered);
  const day = (value: string) => formatLocalDay(value, i18n.language);
  // One day's figures, in the one wording both the pointer tooltip and the list
  // below read from — a second phrasing would be a second answer to drift from.
  const readout = (column: UsageDayColumn) =>
    t('settings.models.usage.byDay.column', {
      day: day(column.day),
      tokens: count(column.tokens),
      requests: count(column.requests),
    }) as string;
  return (
    <section className="model-hub-usage-card overflow-hidden rounded-xl border border-border bg-background">
      <h3 className="model-hub-usage-card-head model-hub-usage-section-title border-b border-border font-semibold text-foreground">
        {t('settings.models.usage.byDay.title')}
      </h3>
      <div className="model-hub-usage-plot flex flex-col">
        <div
          role="img"
          aria-label={t('settings.models.usage.byDay.chart', { from: day(summary.from_day), to: day(summary.to_day) }) as string}
          className="model-hub-usage-chart flex items-end"
        >
          {columns.map((column) => (
            <div
              key={column.day}
              className="model-hub-usage-track flex flex-1 items-end"
              title={readout(column)}
            >
              {/* A metered day is floored to a visible sliver so the quietest one
                  still reads as activity — including one whose tokens never came
                  back, which is why the floor asks about calls and not about
                  tokens. Only a day that carried nothing keeps zero height and
                  shows the track alone. */}
              <div className="model-hub-usage-column w-full" style={{ height: usageDayIsMetered(column) ? `max(2px, ${column.ratio * 100}%)` : 0 }} />
            </div>
          ))}
        </div>
        {/* Every day's figures, in text. A pointer tooltip is the one readout a
            keyboard or screen-reader user cannot open, and `role="img"` above
            hides the columns from assistive tech by design — so the series is
            also a list, and the axis stops being the only reachable reading of
            it. Sibling rather than child: inside the image it would be hidden
            with everything else. */}
        <ul className="sr-only">
          {columns.map((column) => (
            <li key={column.day}>{readout(column)}</li>
          ))}
        </ul>
        <div className="model-hub-usage-axis flex items-baseline justify-between gap-3">
          <span className="truncate">{day(summary.from_day)}</span>
          <span className="model-hub-usage-peak truncate">
            {peak !== null
              ? t('settings.models.usage.byDay.peak', { tokens: count(peak.tokens), day: day(peak.day) })
              // No peak is two different windows: one nobody used, and one whose
              // upstreams never reported what they cost. Only the first is quiet.
              : metered
                ? t('settings.models.usage.byDay.unreported')
                : t('settings.models.usage.byDay.quiet')}
          </span>
          <span className="truncate">{day(summary.to_day)}</span>
        </div>
      </div>
    </section>
  );
};

export const UsageTab: React.FC<{
  usage: RegionRead<UsageSummary>;
  /** The window the user asked for. The report answers with what it served. */
  windowDays: UsageWindowOption;
  onWindowChange: (days: UsageWindowOption) => void;
  onRetry?: () => void | Promise<void>;
}> = ({ usage: usageRead, windowDays, onWindowChange, onRetry }) => {
  const { t, i18n } = useTranslation();
  const summary = foldRegionRead<UsageSummary, UsageSummary | null>(usageRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    // A stale report is still the last true one; it stays on screen under the
    // failure strip rather than being replaced by an empty state that would read
    // as "nothing was ever metered".
    degraded: (staleData) => staleData,
  });
  const options = React.useMemo(
    () => USAGE_WINDOW_OPTIONS.map((option) => ({ id: String(option), label: t('settings.models.usage.window.option', { days: option }) as string })),
    [t],
  );
  const pickWindow = (id: string) => {
    const next = USAGE_WINDOW_OPTIONS.find((option) => String(option) === id);
    if (next !== undefined) onWindowChange(next);
  };
  const day = (value: string) => formatLocalDay(value, i18n.language);

  return (
    <div className="model-hub-usage">
      <div className="model-hub-usage-bar flex flex-col gap-3 rounded-xl border border-border bg-surface sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="model-hub-usage-title font-semibold text-foreground">{t('settings.models.usage.title')}</h2>
          <p className="model-hub-usage-note truncate">
            {summary === null
              ? t('settings.models.usage.detail')
              : t('settings.models.usage.range', { from: day(summary.from_day), to: day(summary.to_day), days: summary.window_days })}
          </p>
        </div>
        <SegmentedRadio
          value={String(windowDays)}
          onChange={pickWindow}
          options={options}
          ariaLabel={t('settings.models.usage.window.label') as string}
          className="shrink-0 sm:w-auto"
        />
      </div>
      {regionFailed(usageRead) && (
        <div className="model-hub-usage-failure flex items-center justify-between gap-3 rounded-xl border border-border bg-background text-destructive-ink">
          <span>{t('settings.models.toast.refreshFailed')}</span>
          <button type="button" onClick={() => void onRetry?.()} className="model-hub-action-mint shrink-0 font-semibold">
            {t('settings.models.upstream.retry')}
          </button>
        </div>
      )}
      {summary === null
        ? usageRead.kind === 'loading' && <p className="model-hub-usage-pending text-muted">{t('common.loading')}</p>
        : usageIsEmpty(summary)
          ? <p className="model-hub-usage-empty rounded-xl border border-border bg-background text-center text-muted">{t('settings.models.usage.empty')}</p>
          : <>
              <StatGrid summary={summary} />
              <BySourcePanel summary={summary} />
              <ByDayPanel summary={summary} />
            </>}
    </div>
  );
};
