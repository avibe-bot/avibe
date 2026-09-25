// 订阅额度 — each logged-in subscription's own rate-limit windows
// (`quota-summary.schema.json`).
//
// Like 用量 it is a report and only a report: nothing here feeds resolution,
// admission, or cooldown. Beside the quota it states what the same use would
// cost at list API prices (the optional `value` block), against the plan's
// monthly fee — a valuation to judge whether the plan pays for itself, never a
// charge. A server that predates the block draws the quota alone.
//
// Drawn after the 订阅额度 design preview. Its geometry follows the usage tab's
// vocabulary (12px card radius, 18px gutter) so the two tabs read as one surface.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CircleDollarSign, Clock3, Gauge, LoaderCircle, RefreshCw, Sparkles, TimerReset, TrendingUp } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { formatCount, formatUsd } from './format';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import type { PricedUsage, QuotaSummary, QuotaWindow, SourceQuota, SourceQuotaValue } from './types';
import { VendorGlyph } from './vendorGlyph';
import {
  DAY_MS,
  HOUR_MS,
  MINUTE_MS,
  exhaustedWindows,
  quotaDuration,
  quotaIsLive,
  quotaIsRetained,
  quotaPayback,
  sourceStatus,
  tightestWindow,
  upcomingResets,
  windowIsExhausted,
  windowIsScoped,
  windowLeftPct,
  windowUsedPct,
  windowPace,
  windowResetAt,
  type QuotaPace,
  type QuotaTone,
} from './quotaProjection';

/** The copy helpers every panel shares, bound to one `now`. */
const useQuotaText = (now: number) => {
  const { t, i18n } = useTranslation();
  const duration = React.useCallback((ms: number) => {
    const d = quotaDuration(ms);
    if (d.unit === 'days') return t('settings.models.quota.duration.days', { days: d.days, hours: d.hours }) as string;
    if (d.unit === 'hours') return t('settings.models.quota.duration.hours', { hours: d.hours, minutes: d.minutes }) as string;
    return t('settings.models.quota.duration.minutes', { minutes: d.minutes }) as string;
  }, [t]);
  const clock = React.useCallback((at: number) => {
    const time = new Intl.DateTimeFormat(i18n.language, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(at);
    const startOfDay = (ms: number) => { const day = new Date(ms); day.setHours(0, 0, 0, 0); return day.getTime(); };
    const dayDiff = Math.round((startOfDay(at) - startOfDay(now)) / DAY_MS);
    if (dayDiff === 0) return t('settings.models.quota.clock.today', { time }) as string;
    if (dayDiff === 1) return t('settings.models.quota.clock.tomorrow', { time }) as string;
    return new Intl.DateTimeFormat(i18n.language, {
      weekday: 'short', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(at);
  }, [i18n.language, now, t]);
  const ago = React.useCallback((at: number) => {
    const ms = Math.max(0, now - at);
    return ms < HOUR_MS
      ? t('settings.models.quota.ago.minutes', { count: Math.max(1, Math.round(ms / MINUTE_MS)) }) as string
      : t('settings.models.quota.ago.long', { duration: duration(ms) }) as string;
  }, [duration, now, t]);
  /** A window's name: the vendor's own model name, framed in this product's words. */
  const windowLabel = React.useCallback((window: QuotaWindow) => {
    if (window.kind === 'session') return t('settings.models.quota.window.session') as string;
    if (window.kind === 'weekly') return t('settings.models.quota.window.weekly') as string;
    if (window.kind === 'model_weekly') return t('settings.models.quota.window.model', { model: window.scope_model ?? window.label }) as string;
    return window.label;
  }, [t]);
  const windowHint = React.useCallback((window: QuotaWindow) => {
    if (window.kind === 'model_weekly') return t('settings.models.quota.hint.model', { model: window.scope_model ?? window.label }) as string;
    if (window.kind === 'session' || window.kind === 'weekly') return t('settings.models.quota.hint.shared') as string;
    return null;
  }, [t]);
  const paceText = React.useCallback((pace: QuotaPace) => {
    switch (pace.kind) {
      case 'paused': return t('settings.models.quota.pace.paused') as string;
      case 'reset': return t('settings.models.quota.pace.reset') as string;
      case 'exhausted': return pace.remainingMs !== null
        ? t('settings.models.quota.pace.exhausted', { duration: duration(pace.remainingMs) }) as string
        : t('settings.models.quota.pace.exhaustedUnknown') as string;
      case 'fast': return t('settings.models.quota.pace.fast', { time: clock(pace.runsOutAt) }) as string;
      case 'slightly_fast': return t('settings.models.quota.pace.slightlyFast') as string;
      default: return t('settings.models.quota.pace.ok') as string;
    }
  }, [clock, duration, t]);
  /** A priced figure: 「≥」 when part of it predates cache-write capture. */
  const dollars = React.useCallback((amount: number) => formatUsd(amount, i18n.language), [i18n.language]);
  const usd = React.useCallback((value: PricedUsage) => (
    `${value.api_cost_lower_bound ? '≥ ' : ''}${dollars(value.api_cost_usd)}`
  ), [dollars]);
  const tokens = React.useCallback((value: number) => formatCount(value, i18n.language), [i18n.language]);
  /** A multiple to one decimal, rounded down so 0.99× never reads as 「1.0 倍」. */
  const multiple = React.useCallback((value: number) => new Intl.NumberFormat(i18n.language, {
    minimumFractionDigits: 1, maximumFractionDigits: 1,
  }).format(Math.floor(value * 10) / 10), [i18n.language]);
  return { duration, clock, ago, windowLabel, windowHint, paceText, dollars, usd, tokens, multiple };
};

type QuotaText = ReturnType<typeof useQuotaText>;

const toneClass = (tone: QuotaTone) => `model-hub-quota-tone--${tone}`;

const WindowRow: React.FC<{ window: QuotaWindow; now: number; retained: boolean; nested?: boolean; text: QuotaText }> = ({
  window, now, retained, nested, text,
}) => {
  const { t } = useTranslation();
  const reading = windowPace(window, now, retained);
  const label = text.windowLabel(window);
  const hint = text.windowHint(window);
  const resetAt = windowResetAt(window);
  const untilReset = resetAt !== null ? resetAt - now : null;
  const alarm = reading.tone === 'warn' || reading.tone === 'danger';
  return (
    <div className={cn('model-hub-quota-row', nested && 'model-hub-quota-row--nested')} data-quota-window={window.id}>
      <div className="flex items-end justify-between gap-2.5">
        <div className="model-hub-quota-row-title min-w-0">
          <strong className="font-medium text-foreground">{label}</strong>
          {hint && <small>{hint}</small>}
        </div>
        <div className={cn('model-hub-quota-left flex shrink-0 items-baseline gap-1', toneClass(reading.tone))}>
          <b>{windowLeftPct(window, now)}%</b>
          <span>{t('settings.models.quota.left')}</span>
        </div>
      </div>
      <div
        className="model-hub-quota-bar"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={windowUsedPct(window, now)}
        aria-label={t('settings.models.quota.meter', { label, pct: windowUsedPct(window, now) }) as string}
      >
        <i className={toneClass(reading.tone)} style={{ width: `${windowUsedPct(window, now)}%` }} />
        {untilReset !== null && untilReset > 0 && reading.elapsedPct !== null && (
          <span className="model-hub-quota-time-mark" style={{ left: `${reading.elapsedPct}%` }} />
        )}
      </div>
      <div className="model-hub-quota-row-foot">
        <span className={cn('model-hub-quota-pace flex items-center gap-[5px]', toneClass(reading.tone))}>
          {alarm ? <AlertTriangle className="size-[11px]" aria-hidden /> : <span className="model-hub-quota-dot" aria-hidden />}
          {text.paceText(reading.pace)}
        </span>
        {untilReset !== null && (
          <span className="model-hub-quota-reset flex items-center gap-[5px]">
            <TimerReset className="size-[11px]" aria-hidden />
            {untilReset > 0
              ? <>{t('settings.models.quota.reset.in', { duration: text.duration(untilReset) })}<em>{text.clock(resetAt as number)}</em></>
              : t('settings.models.quota.reset.done')}
          </span>
        )}
      </div>
    </div>
  );
};

/** The no-reading line: the known states have their own copy, a named failure its own reason. */
const unreadKey = (source: SourceQuota) => {
  if (source.state === 'auth_expired') return 'settings.models.quota.unread.auth_expired' as const;
  if (source.state === 'unsupported') return 'settings.models.quota.unread.unsupported' as const;
  if (source.error_key === 'models.quota.error.rate_limited') return 'models.quota.error.rate_limited' as const;
  if (source.error_key === 'models.quota.error.malformed') return 'models.quota.error.malformed' as const;
  return 'settings.models.quota.unread.error' as const;
};

/** Whether every priced token of a span belongs to a model with no price. */
const unpriced = (value: PricedUsage) => value.api_cost_usd === 0 && value.excluded_tokens > 0;

/** The account's API-price value: its week, its period against the fee, and the renewal. */
const ValueStrip: React.FC<{ value: SourceQuotaValue; now: number; text: QuotaText }> = ({ value, now, text }) => {
  const { t } = useTranslation();
  const payback = value.fee_usd !== null ? quotaPayback(value.period.api_cost_usd, value.fee_usd) : null;
  const renewsOn = value.period.renews_on !== null ? Date.parse(`${value.period.renews_on}T00:00:00`) : NaN;
  const excluded = Math.max(value.week.excluded_tokens, value.period.excluded_tokens);
  return (
    <>
      <div className="model-hub-quota-value" data-quota-value>
        <div>
          <span>{t('settings.models.quota.value.week')}</span>
          <b>{unpriced(value.week) ? t('settings.models.quota.value.noPrice') : text.usd(value.week)}</b>
        </div>
        <div>
          <span>{t(`settings.models.quota.value.period.${value.period.basis}`)}</span>
          <b>
            {unpriced(value.period) ? t('settings.models.quota.value.noPrice') : text.usd(value.period)}
            {payback !== null && (
            <em className={cn(payback.kind !== 'short' && 'is-good')} data-quota-payback={payback.kind}>
              {payback.kind === 'paid'
                  ? t('settings.models.quota.value.paid', { multiple: text.multiple(payback.multiple) })
                  : payback.kind === 'even'
                    ? t('settings.models.quota.value.even')
                    : t('settings.models.quota.value.short', { amount: text.dollars(payback.shortfallUsd) })}
            </em>
            )}
          </b>
        </div>
        {!Number.isNaN(renewsOn) && (
          <div>
            <span>{t('settings.models.quota.value.renews')}</span>
            <b>
              {t('settings.models.quota.value.renewsIn', { count: Math.max(0, Math.ceil((renewsOn - now) / DAY_MS)) })}
              <em>{value.period.renews_on}</em>
            </b>
          </div>
        )}
      </div>
      {excluded > 0 && (
        <p className="model-hub-quota-value-note">{t('settings.models.quota.value.excluded', { tokens: text.tokens(excluded) })}</p>
      )}
    </>
  );
};

const AccountCard: React.FC<{
  source: SourceQuota;
  now: number;
  text: QuotaText;
  /** The read is still running: a gap is loading, not a failure. */
  pending?: boolean;
  onRequestReauth?: (sourceId: string) => void;
}> = ({ source, now, text, pending = false, onRequestReauth }) => {
  const { t } = useTranslation();
  const retained = quotaIsRetained(source);
  const status = sourceStatus(source, now);
  const shared = source.windows.filter((window) => !windowIsScoped(window));
  const scoped = source.windows.filter(windowIsScoped);
  const statusTone: QuotaTone = status.kind === 'ok' ? 'ok' : status.kind === 'exhausted' ? 'danger' : 'stale';
  const statusText = status.kind === 'exhausted'
    ? t('settings.models.quota.status.exhausted', { label: text.windowLabel(status.window) })
    : t(`settings.models.quota.status.${status.kind}`);
  const expired = source.state === 'auth_expired';
  const fetchedAt = source.fetched_at !== null ? Date.parse(source.fetched_at) : NaN;
  return (
    <article
      className={cn('model-hub-quota-card min-w-0 rounded-xl border border-border bg-background', retained && 'model-hub-quota-card--retained')}
      aria-label={source.display_name}
      data-quota-source={source.source_id}
    >
      <header className="flex items-center gap-2.5">
        <VendorGlyph vendor={source.vendor} className="size-7 shrink-0" />
        <div className="min-w-0 flex-1">
          <strong className="model-hub-quota-account flex min-w-0 items-center gap-2 font-semibold text-foreground">
            <span className="truncate">{source.display_name}</span>
            {source.plan && <span className="model-hub-quota-plan shrink-0">{source.plan}</span>}
          </strong>
          {source.account_label && <small className="model-hub-quota-account-label block truncate">{source.account_label}</small>}
        </div>
        <span className={cn('model-hub-quota-status flex shrink-0 items-center gap-1.5', toneClass(statusTone))}>
          <span className="model-hub-quota-dot" aria-hidden />{statusText}
        </span>
      </header>
      {retained && !Number.isNaN(fetchedAt) && (
        <div className="model-hub-quota-stale flex items-center gap-2" role="status">
          <AlertTriangle className="size-[13px] shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">
            {t(expired ? 'settings.models.quota.stale.authExpired' : 'settings.models.quota.stale.retained', { ago: text.ago(fetchedAt) })}
          </span>
          {expired && onRequestReauth && (
            <Button variant="outline" size="sm" className="shrink-0" onClick={() => onRequestReauth(source.source_id)}>
              {t('settings.models.quota.stale.reauth')}
            </Button>
          )}
        </div>
      )}
      {source.windows.length === 0 && pending && source.state === 'error'
        ? (
            <div className="model-hub-quota-unread flex items-center gap-2" role="status" data-quota-pending>
              <LoaderCircle className="size-[13px] shrink-0 animate-spin" aria-hidden />
              <span className="min-w-0 flex-1">{t('settings.models.quota.pending')}</span>
            </div>
          )
        : source.windows.length === 0 || (!retained && !quotaIsLive(source))
        ? (
            <div className="model-hub-quota-unread flex items-center gap-2">
              <span className="min-w-0 flex-1">
                {t(unreadKey(source))}
              </span>
              {expired && onRequestReauth && (
                <Button variant="outline" size="sm" className="shrink-0" onClick={() => onRequestReauth(source.source_id)}>
                  {t('settings.models.quota.stale.reauth')}
                </Button>
              )}
            </div>
          )
        : (
            <div className="model-hub-quota-rows flex flex-col">
              {shared.map((window) => <WindowRow key={window.id} window={window} now={now} retained={retained} text={text} />)}
              {scoped.length > 0 && (
                <div className="model-hub-quota-scoped flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                  <Sparkles className="size-[11px]" aria-hidden />
                  {t('settings.models.quota.scoped.title')}
                  <span>{t('settings.models.quota.scoped.note')}</span>
                </div>
              )}
              {scoped.map((window) => <WindowRow key={window.id} window={window} now={now} retained={retained} nested text={text} />)}
            </div>
          )}
      {source.value && <ValueStrip value={source.value} now={now} text={text} />}
    </article>
  );
};

const StatCard: React.FC<{ label: string; icon: React.ReactNode; value: React.ReactNode; note: React.ReactNode; primary?: boolean }> = ({
  label, icon, value, note, primary,
}) => (
  <div className={cn('model-hub-usage-stat flex flex-col rounded-xl border border-border bg-background', primary && 'model-hub-quota-stat--primary')}>
    <span className="model-hub-usage-stat-label flex items-center justify-between gap-2">{label}{icon}</span>
    <span className="model-hub-usage-stat-value font-semibold text-foreground">{value}</span>
    <span className="model-hub-usage-stat-note">{note}</span>
  </div>
);

/** A live clock for pace and countdowns, re-read every half minute. */
const useNow = (override?: number) => {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (override !== undefined) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [override]);
  return override ?? now;
};

export const QuotaTab: React.FC<{
  quota: RegionRead<QuotaSummary>;
  refreshing?: boolean;
  onRefresh?: () => void | Promise<void>;
  onRetry?: () => void | Promise<void>;
  /** Asks to sign the expired grant in again; the page confirms before the journey starts. */
  onRequestReauth?: (sourceId: string) => void;
  /** Fixes the clock the projection reads; the tab ticks its own when absent. */
  now?: number;
}> = ({ quota: quotaRead, refreshing = false, onRefresh, onRetry, onRequestReauth, now: fixedNow }) => {
  const { t } = useTranslation();
  const now = useNow(fixedNow);
  const text = useQuotaText(now);
  const summary = foldRegionRead<QuotaSummary, QuotaSummary | null>(quotaRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    // The last reading stays on screen under the failure strip.
    degraded: (staleData) => staleData,
  });
  const sources = summary?.sources ?? [];
  const tightest = tightestWindow(sources, now);
  const exhausted = exhaustedWindows(sources, now);
  const upcoming = upcomingResets(sources, now);
  const pending = new Set(summary?.pending ?? []);
  const value = summary?.value;
  const periodPayback = value?.period ? quotaPayback(value.period.api_cost_usd, value.period.fee_usd) : null;
  const unfeed = sources.filter((source) => source.value && source.value.fee_usd === null).length;
  // 本期 is each account's billing cycle only where its renewal day is known.
  const rollingPeriod = sources.some((source) => source.value?.fee_usd != null && source.value.period.basis === 'rolling_30d');

  return (
    <section className="model-hub-usage" aria-label={t('settings.models.quota.title') as string}>
      <div className="model-hub-usage-bar flex flex-col gap-3 rounded-xl border border-border bg-surface sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="model-hub-usage-title font-semibold text-foreground">{t('settings.models.quota.title')}</h2>
          <p className="model-hub-usage-note flex flex-wrap gap-x-2">
            {summary !== null && <span>{t('settings.models.quota.header', { count: sources.length })}</span>}
            {summary !== null && <span aria-hidden>·</span>}
            <span>{t('settings.models.quota.autoRefresh')}</span>
          </p>
        </div>
        <Button variant="outline" size="sm" className="shrink-0 self-start sm:self-auto" disabled={refreshing} onClick={() => void onRefresh?.()}>
          <RefreshCw className={cn('size-[13px]', refreshing && 'animate-spin')} aria-hidden />
          {refreshing ? t('settings.models.quota.refreshing') : t('settings.models.quota.refresh')}
        </Button>
      </div>
      {regionFailed(quotaRead) && (
        <div className="model-hub-usage-failure flex items-center justify-between gap-3 rounded-xl border border-border bg-background text-destructive-ink">
          <span>{t('settings.models.toast.refreshFailed')}</span>
          <button type="button" onClick={() => void onRetry?.()} className="model-hub-action-mint shrink-0 font-semibold">
            {t('settings.models.upstream.retry')}
          </button>
        </div>
      )}
      {summary === null
        ? quotaRead.kind === 'loading' && <p className="model-hub-usage-pending text-muted">{t('common.loading')}</p>
        : sources.length === 0
          ? <p className="model-hub-usage-empty rounded-xl border border-border bg-background text-center text-muted">{t('settings.models.quota.empty')}</p>
          : <>
              <div className={cn('model-hub-quota-stats grid gap-4', value && 'model-hub-quota-stats--valued')}>
                <StatCard
                  primary
                  label={t('settings.models.quota.stat.tightest')}
                  icon={<Gauge className="size-[15px]" aria-hidden />}
                  value={tightest ? <>{windowLeftPct(tightest.window, now)}%<small className="model-hub-quota-stat-unit">{t('settings.models.quota.left')}</small></> : '—'}
                  note={tightest
                    ? (() => {
                        const resetAt = windowResetAt(tightest.window);
                        return [
                          tightest.source.display_name,
                          text.windowLabel(tightest.window),
                          resetAt !== null && resetAt > now ? t('settings.models.quota.stat.resetsIn', { duration: text.duration(resetAt - now) }) : null,
                        ].filter(Boolean).join(' · ');
                      })()
                    // Like 「every account is usable」 below, 「every limit is used up」 is a
                    // claim about all accounts: make it only when each has a current reading.
                    : t(exhausted.length && sources.every(quotaIsLive) ? 'settings.models.quota.stat.tightestAllSpent' : 'settings.models.quota.stat.tightestUnread')}
                />
                <StatCard
                  label={t('settings.models.quota.stat.exhausted')}
                  icon={<Clock3 className="size-[15px]" aria-hidden />}
                  value={<>{exhausted.length}<small className="model-hub-quota-stat-unit">{t('settings.models.quota.stat.exhaustedUnit')}</small></>}
                  note={exhausted.length
                    ? (() => {
                        const resetAt = windowResetAt(exhausted[0].window);
                        return resetAt !== null
                          ? t('settings.models.quota.stat.exhaustedNote', { name: exhausted[0].source.display_name, time: text.clock(resetAt) })
                          : exhausted[0].source.display_name;
                      })()
                    // 「every account is usable」 is a claim about accounts; make it only
                    // when every account has a current reading to back it.
                    : t(sources.every(quotaIsLive) ? 'settings.models.quota.stat.exhaustedNone' : 'settings.models.quota.stat.exhaustedNoneObserved')}
                />
                {value && (
                  <StatCard
                    label={t('settings.models.quota.stat.weekValue')}
                    icon={<CircleDollarSign className="size-[15px]" aria-hidden />}
                    value={unpriced(value.week)
                      ? t('settings.models.quota.value.noPrice')
                      : <>{text.usd(value.week)}<small className="model-hub-quota-stat-unit">USD</small></>}
                    note={[
                      t('settings.models.quota.stat.weekValueNote'),
                      value.week.excluded_tokens > 0 && !unpriced(value.week)
                        ? t('settings.models.quota.value.excluded', { tokens: text.tokens(value.week.excluded_tokens) })
                        : null,
                    ].filter(Boolean).join(' · ')}
                  />
                )}
                {value && (
                  <StatCard
                    label={t('settings.models.quota.stat.payback')}
                    icon={<TrendingUp className="size-[15px]" aria-hidden />}
                    value={value.period && periodPayback
                      ? <>{t('settings.models.quota.stat.paybackMultiple', { multiple: text.multiple(periodPayback.multiple) })}<small className="model-hub-quota-stat-unit">{t('settings.models.quota.stat.paybackUnit')}</small></>
                      : '—'}
                    note={value.period && periodPayback
                      ? [
                          periodPayback.kind === 'short'
                            ? t('settings.models.quota.stat.paybackShort', { amount: text.dollars(periodPayback.shortfallUsd) })
                            : t('settings.models.quota.stat.paybackPaid', { amount: text.dollars(periodPayback.surplusUsd) }),
                          rollingPeriod ? t('settings.models.quota.stat.paybackRolling') : null,
                          unfeed > 0 ? t('settings.models.quota.stat.paybackPartial', { count: unfeed }) : null,
                        ].filter(Boolean).join(' · ')
                      : t('settings.models.quota.stat.paybackUnknown')}
                  />
                )}
              </div>
              {upcoming.length > 0 && (
                <div className="model-hub-quota-timeline flex flex-wrap items-center gap-2 rounded-xl border border-border bg-surface" aria-label={t('settings.models.quota.upcoming') as string}>
                  <span className="model-hub-quota-timeline-title flex items-center gap-1.5"><TimerReset className="size-[13px]" aria-hidden />{t('settings.models.quota.upcoming')}</span>
                  {upcoming.map(({ source, window }) => (
                    <span key={`${source.source_id}:${window.id}`} className={cn('model-hub-quota-chip flex items-baseline gap-1.5', windowIsExhausted(window, now) && toneClass('danger'))}>
                      <b>{text.duration((windowResetAt(window) as number) - now)}</b>
                      {source.display_name} · {text.windowLabel(window)}
                    </span>
                  ))}
                </div>
              )}
              <div className="model-hub-quota-grid grid gap-4">
                {sources.map((source) => (
                  <AccountCard
                    key={source.source_id}
                    source={source}
                    now={now}
                    text={text}
                    pending={pending.has(source.source_id)}
                    onRequestReauth={onRequestReauth}
                  />
                ))}
              </div>
              <p className="model-hub-quota-foot flex items-start gap-2">
                <Clock3 className="mt-px size-[13px] shrink-0" aria-hidden />
                <span>
                  {t('settings.models.quota.footnote')}
                  {value && <>{' '}{t('settings.models.quota.footnoteValue')}</>}
                  {value && <>{' '}{value.price_table_date
                    ? t('settings.models.quota.priceTableDate', { date: value.price_table_date })
                    : t('settings.models.quota.priceTableUnknown')}</>}
                </span>
              </p>
            </>}
    </section>
  );
};
