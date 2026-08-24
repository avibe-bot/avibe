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
import type { UsageByModel, UsageBySource, UsageCounters, UsageSummary } from './types';
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
  usageTokensAreKnown,
  usageTokensAreReported,
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

/** Anything a token figure is derived from: a totals bucket, a row, or a day. */
type TokenCoverage = Pick<UsageCounters, 'requests' | 'token_reports'>;

/**
 * The one way this tab turns a token count into text.
 *
 * A rendered `0` cannot say which of three things it means — a cost reported as
 * nothing, a cost nobody reported, or nothing having run at all — and every panel
 * this tab draws states token figures, so wording any single one of them correctly
 * leaves all the others to be found one review round at a time. The figure asks
 * its own bucket first: for calls that no upstream ever costed there is no
 * measurement to print, and the blank marker the cached share and the metering
 * timestamp already use says exactly that.
 *
 * Coverage travels with the value rather than being checked by the caller, so a
 * new token figure cannot be written without naming the counters it came from.
 */
const useTokenText = () => {
  const { t } = useTranslation();
  const count = useCount();
  return (counters: TokenCoverage, value: number) =>
    usageTokensAreKnown(counters) ? count(value) : (t('settings.models.usage.blank') as string);
};

/**
 * A token figure standing on its own, as a cell or a card value.
 *
 * `data-usage-token` is not styling: it is what lets a test enumerate every token
 * figure on screen and assert they all went through the door, which is a claim
 * about completeness that a per-site test cannot make. A figure interpolated into
 * a translated sentence has no element of its own and uses `useTokenText`
 * directly; its sentence is then the asserted unit.
 */
const TokenFigure: React.FC<{ counters: TokenCoverage; value: number }> = ({ counters, value }) => {
  const tokenText = useTokenText();
  return <span data-usage-token="">{tokenText(counters, value)}</span>;
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
 * The by-source table's columns, in the order the rows state them.
 *
 * One list, read by the header row and by every cell's own label, so the two
 * cannot come to name a column differently.
 */
const SOURCE_COLUMNS = ['source', 'tokens', 'requests', 'cached', 'lastMetered'] as const;

type SourceColumn = (typeof SOURCE_COLUMNS)[number];

const columnLabel = (column: SourceColumn) => `settings.models.usage.bySource.col.${column}`;

/**
 * One measured cell, told which column it answers.
 *
 * A cell's column is the position it holds in its row, which is what makes the
 * header row above it an answer and not decoration — so no row may end early: the
 * model row holds its empty column open below rather than shifting the cells after
 * it. The label repeats the header on the cell itself because the header is the
 * first thing that goes when the surface narrows, and a stacked cell has to remain
 * a labelled number rather than an unattributed one. Between them the two cover
 * both widths, so a per-cell `aria-colindex` would restate what position already
 * says; the index earns its keep only for a row that skips a column in the middle,
 * and holding the place with an empty cell is the simpler way to not have one.
 */
const Cell: React.FC<{ column: SourceColumn; children: React.ReactNode }> = ({ column, children }) => {
  const { t } = useTranslation();
  return (
    <span role="cell" className="model-hub-usage-cell flex items-baseline justify-between gap-2 md:justify-end">
      <span className="model-hub-usage-cell-label md:hidden">{t(columnLabel(column))}</span>
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
};

const StatCard: React.FC<{ label: string; value: React.ReactNode; note: string }> = ({ label, value, note }) => (
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
        value={<TokenFigure counters={totals} value={usageTotalTokens(totals)} />}
        // The input/output split is a reading of the same unreported total, so it
        // cannot survive on its own once the total is blank: it says what nobody
        // reported instead.
        note={(usageTokensAreKnown(totals)
          ? t('settings.models.usage.tokens.detail', { input: count(totals.input_tokens), output: count(totals.output_tokens) })
          : t('settings.models.usage.tokens.none')) as string}
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
        // 「No input tokens in this window」 is the same defect one card over: with
        // nothing reported there were no input tokens WE KNOW OF, and an absence of
        // reports is not an absence of usage. Coverage is asked before the share, so
        // the card states the missing reports rather than an empty input.
        note={(!usageTokensAreKnown(totals)
          ? t('settings.models.usage.tokens.none')
          : cachedShare === null
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
    <div role="row" className="model-hub-usage-row model-hub-usage-row--model grid border-t border-border md:items-center">
      <span role="rowheader" className="model-hub-usage-model flex min-w-0 items-baseline">
        <RowIdentity identity={modelIdentity(model)} goneKey="settings.models.usage.bySource.goneModel" />
      </span>
      <Cell column="tokens"><TokenFigure counters={model} value={usageTotalTokens(model)} /></Cell>
      <Cell column="requests">{count(model.requests)}</Cell>
      <Cell column="cached">
        {share === null ? (t('settings.models.usage.blank') as string) : formatPercent(share, i18n.language)}
      </Cell>
      {/* A model has no metering timestamp of its own; the column stays empty
          rather than repeating the Source's, and holds its place so every cell
          before it still sits under the header it answers. */}
      <span role="cell" className="hidden md:block" />
    </div>
  );
};

const SourceRows: React.FC<{ source: UsageBySource }> = ({ source }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const share = usageCachedInputShare(source);
  return (
    // A Source and the models under it are one group of rows, which is also what
    // the border draws: the group is the unit a reader scans, not each line.
    <div role="rowgroup" className="border-b border-border last:border-b-0">
      <div role="row" className="model-hub-usage-row grid md:items-center">
        <span role="rowheader" className="model-hub-usage-source flex min-w-0 items-baseline font-semibold text-foreground">
          <RowIdentity identity={sourceIdentity(source)} goneKey="settings.models.usage.bySource.goneSource" />
        </span>
        <Cell column="tokens"><TokenFigure counters={source} value={usageTotalTokens(source)} /></Cell>
        <Cell column="requests">{count(source.requests)}</Cell>
        <Cell column="cached">
          {share === null ? (t('settings.models.usage.blank') as string) : formatPercent(share, i18n.language)}
        </Cell>
        <Cell column="lastMetered">
          {source.last_metered_at === null ? (t('settings.models.usage.blank') as string) : formatDayTime(source.last_metered_at, i18n.language)}
        </Cell>
      </div>
      {source.models.map((model) => <ModelRow key={model.model_id} model={model} />)}
    </div>
  );
};

const BySourcePanel: React.FC<{ summary: UsageSummary }> = ({ summary }) => {
  const { t } = useTranslation();
  const titleId = React.useId();
  return (
    <section className="model-hub-usage-card overflow-hidden rounded-xl border border-border bg-background">
      <h3 id={titleId} className="model-hub-usage-card-head model-hub-usage-section-title border-b border-border font-semibold text-foreground">
        {t('settings.models.usage.bySource.title')}
      </h3>
      {/* A table by role rather than by tag. The layout is a CSS grid that stacks
          into labelled lines on a narrow surface, which a `<table>` can only do
          through a `display` override — and overriding `display` is exactly what
          strips a table of the semantics it was chosen for. Explicit roles keep
          the grid and the structure at every width. */}
      <div role="table" aria-labelledby={titleId}>
        <div role="row" className="model-hub-usage-head hidden border-b border-border font-semibold md:grid">
          {SOURCE_COLUMNS.map((column, index) => (
            <span key={column} role="columnheader" className={index === 0 ? 'truncate' : 'flex justify-end truncate'}>
              {t(columnLabel(column))}
            </span>
          ))}
        </div>
        {summary.sources.map((source) => <SourceRows key={source.source_id} source={source} />)}
      </div>
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
 * bars to draw and a peak it cannot name. Whether a day's cost is known at all is
 * `usageTokensAreReported`'s separate answer — asked of the very same zero.
 */
const ByDayPanel: React.FC<{ summary: UsageSummary }> = ({ summary }) => {
  const { t, i18n } = useTranslation();
  const count = useCount();
  const tokenText = useTokenText();
  const columns = React.useMemo(() => usageDayColumns(summary), [summary]);
  // Only a day whose tokens were reported can be the busiest one. A day with no
  // report has no measured cost to compare, so naming it the peak would put a
  // superlative on a number the report never carried.
  const peak = columns.reduce<UsageDayColumn | null>(
    (best, column) => (usageTokensAreReported(column) && column.tokens > (best?.tokens ?? 0) ? column : best),
    null,
  );
  const metered = columns.some(usageDayIsMetered);
  const reported = columns.some(usageTokensAreReported);
  const day = (value: string) => formatLocalDay(value, i18n.language);
  // One day's figures as a hover readout. The table below states the same three
  // values in cells, and MH-USAGE-023 derives its expectation from this sentence
  // so the two readings of a day cannot answer differently.
  const readout = (column: UsageDayColumn) =>
    t('settings.models.usage.byDay.column', {
      day: day(column.day),
      tokens: tokenText(column, column.tokens),
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
        {/* Every day's figures again, as a table. A pointer tooltip is the one
            readout a keyboard or screen-reader user cannot open, and `role="img"`
            above hides the columns from assistive tech by design — so the series
            needs a second reading, in cells rather than one sentence per day,
            which is the same per-column association the Source table carries.
            Sibling of the image, never a child: inside it, it would be hidden
            along with everything else. */}
        {/* Apply the visually-hidden box to a generic wrapper. A table keeps its
            intrinsic column width even when the table itself is only one pixel
            wide; containing that layout inside the clipped wrapper keeps its
            semantics without letting it widen the document. */}
        <div className="model-hub-usage-a11y-table sr-only">
          <table>
            <caption>{t('settings.models.usage.byDay.table', { from: day(summary.from_day), to: day(summary.to_day) })}</caption>
            <thead>
              <tr>
                <th scope="col">{t('settings.models.usage.byDay.col.day')}</th>
                <th scope="col">{t('settings.models.usage.tokens.label')}</th>
                <th scope="col">{t('settings.models.usage.requests.label')}</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((column) => (
                <tr key={column.day}>
                  <th scope="row">{day(column.day)}</th>
                  <td><TokenFigure counters={column} value={column.tokens} /></td>
                  <td>{count(column.requests)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="model-hub-usage-axis">
          <span className="model-hub-usage-axis-label">{day(summary.from_day)}</span>
          <span className="model-hub-usage-axis-label model-hub-usage-peak">
            {peak !== null
              ? t('settings.models.usage.byDay.peak', { tokens: tokenText(peak, peak.tokens), day: day(peak.day) })
              // No peak is three different windows, and the tokens cannot tell them
              // apart: one nobody used, one whose upstreams never reported what the
              // calls cost, and one that was measured and really did cost nothing.
              // Coverage is asked before activity, because a window with any report
              // in it can state what its reports said — the calls that came back
              // without one are the requests card's shortfall to name, not this
              // sentence's.
              : reported
                ? t('settings.models.usage.byDay.zero')
                : metered
                  ? t('settings.models.usage.byDay.unreported')
                  : t('settings.models.usage.byDay.quiet')}
          </span>
          <span className="model-hub-usage-axis-label">{day(summary.to_day)}</span>
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
