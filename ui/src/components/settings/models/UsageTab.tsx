import * as React from 'react';
import {
  ArrowDown,
  ArrowDownToLine,
  ChevronDown,
  CircleHelp,
  Cpu,
  Filter,
  LoaderCircle,
  Network,
  Pin,
  PinOff,
  Search,
  X,
  Zap,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { SegmentedRadio } from '@/components/ui/segmented';
import { cn } from '@/lib/utils';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import type { UsageCounters, UsageReport, UsageWindowKey } from './types';
import {
  aggregateCounters,
  filterBucketRows,
  filteredRows,
  formatBucketAxisLabel,
  formatBucketLabel,
  formatBucketRange,
  identityDisplayLabel,
  modelLabel,
  pairKey,
  reportHasPartialHistory,
  reportHasUnknownTokens,
  seriesFor,
  sourceIdentityLabel,
  sourceLabel,
  usageLabelContext,
  usageCachedInputShare,
  usageIsEmpty,
  usageMetricValue,
  usageNonCachedInput,
  usageReportShortfall,
  usageTokensAreKnown,
  type UsageFilter,
  type UsageGroup,
  type UsageIdentity,
  type UsageMetric,
  type UsageSeries,
} from './usageProjection';
import { USAGE_WINDOW_OPTIONS } from './usageProjection';
import { buildUsageCsv } from './usageCsv';
import './modelHubSurface.css';

const SERIES_COLORS = [
  'var(--mint)',
  'var(--violet)',
  'var(--cyan)',
  'var(--gold)',
  'var(--destructive)',
  'var(--primary)',
] as const;

type FilterOption = { key: string; label: string; detail?: string };
type UsageTranslate = (key: string, options?: Record<string, unknown>) => string;

const metricKeys: UsageMetric[] = ['tokens', 'input', 'output', 'cache', 'requests'];
const groupKeys: UsageGroup[] = ['total', 'type', 'model', 'source'];

const useUsageTranslation = () => {
  const translation = useTranslation();
  return {
    ...translation,
    t: translation.t as unknown as UsageTranslate,
  };
};

const useCount = () => {
  const { i18n } = useTranslation();
  return React.useCallback((value: number) => new Intl.NumberFormat(i18n.language).format(value), [i18n.language]);
};

const tokenText = (
  counters: UsageCounters,
  metric: UsageMetric,
  count: (value: number) => string,
  blank: string,
): string => {
  const value = usageMetricValue(counters, metric);
  return value === null ? blank : count(value);
};

const metricLabel = (metric: UsageMetric, t: UsageTranslate): string =>
  t(`settings.models.usage.metric.${metric}`);

function MultiFilter({
  label,
  icon,
  options,
  selected,
  onChange,
}: {
  label: string;
  icon: React.ReactNode;
  options: FilterOption[];
  selected: readonly string[];
  onChange: (next: string[]) => void;
}) {
  const { t } = useUsageTranslation();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const rootRef = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);

  React.useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const visibleOptions = options.filter((option) => (
    option.label.toLocaleLowerCase().includes(query.toLocaleLowerCase())
      || option.detail?.toLocaleLowerCase().includes(query.toLocaleLowerCase())
  ));
  const buttonLabel = selected.length === 0
    ? t('settings.models.usage.filters.all', { label })
    : selected.length === 1
      ? options.find((option) => option.key === selected[0])?.label ?? label
      : t('settings.models.usage.filters.selected', { label, count: selected.length });

  const toggle = (key: string) => {
    onChange(selected.includes(key) ? selected.filter((item) => item !== key) : [...selected, key]);
  };

  return (
    <div className="model-hub-usage-filter" ref={rootRef}>
      <Button
        ref={triggerRef}
        type="button"
        variant="outline"
        size="sm"
        aria-expanded={open}
        aria-haspopup="listbox"
        className={cn('model-hub-usage-filter-trigger', selected.length > 0 && 'is-filtered')}
        onClick={() => { setOpen((value) => !value); setQuery(''); }}
      >
        {icon}
        <span className="truncate">{buttonLabel}</span>
        <ChevronDown aria-hidden className="size-3 shrink-0" />
      </Button>
      {open && (
        <div className="model-hub-usage-filter-menu" role="listbox" aria-label={label} aria-multiselectable="true">
          <div className="model-hub-usage-filter-search">
            <Search aria-hidden className="size-3.5 shrink-0" />
            <input
              autoFocus
              value={query}
              aria-label={t('settings.models.usage.filters.search', { label }) as string}
              placeholder={t('settings.models.usage.filters.search', { label }) as string}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <button
            type="button"
            role="option"
            aria-selected={selected.length === 0}
            className="model-hub-usage-filter-option"
            onClick={() => onChange([])}
          >
            <Checkbox checked={selected.length === 0} presentational />
            {t('settings.models.usage.filters.all', { label })}
          </button>
          <div className="model-hub-usage-filter-divider" />
          {visibleOptions.map((option) => (
            <button
              type="button"
              role="option"
              aria-selected={selected.includes(option.key)}
              className="model-hub-usage-filter-option"
              key={option.key}
              onClick={() => toggle(option.key)}
            >
              <Checkbox checked={selected.includes(option.key)} presentational />
              <span className="min-w-0 truncate">
                <span className="block truncate">{option.label}</span>
                {option.detail && <span className="block truncate text-[10px] text-muted">{option.detail}</span>}
              </span>
            </button>
          ))}
          {visibleOptions.length === 0 && <p className="model-hub-usage-filter-empty">{t('settings.models.usage.filters.noMatch')}</p>}
          <p className="model-hub-usage-filter-foot">{t('settings.models.usage.filters.hint')}</p>
        </div>
      )}
    </div>
  );
}

const ChartLegend: React.FC<{
  series: UsageSeries[];
  hidden: readonly string[];
  onToggle: (key: string) => void;
  labels: Map<string, string>;
  count: (value: number) => string;
  blank: string;
  ariaLabel: string;
}> = ({ series, hidden, onToggle, labels, count, blank, ariaLabel }) => (
  <div className="model-hub-usage-legend" aria-label={ariaLabel}>
    {series.map((item) => {
      const values = item.values.filter((value): value is number => value !== null);
      const total = values.reduce((sum, value) => sum + value, 0);
      const isHidden = hidden.includes(item.key);
      return (
        <button
          type="button"
          key={item.key}
          aria-pressed={!isHidden}
          className={cn('model-hub-usage-legend-item', isHidden && 'is-hidden')}
          onClick={() => onToggle(item.key)}
        >
          <span className="model-hub-usage-legend-swatch" style={{ background: SERIES_COLORS[item.colorIndex % SERIES_COLORS.length] }} />
          <span>{labels.get(item.key) ?? item.label}</span>
          <strong>{values.length === 0 ? blank : count(total)}</strong>
        </button>
      );
    })}
  </div>
);

function UsageChart({
  report,
  filter,
  metric,
  group,
  pinnedKey,
  scopeKey,
  onPin,
}: {
  report: UsageReport;
  filter: UsageFilter;
  metric: UsageMetric;
  group: UsageGroup;
  pinnedKey: string | null;
  scopeKey: string;
  onPin: (key: string | null) => void;
}) {
  const { t, i18n } = useUsageTranslation();
  const count = useCount();
  const [hovered, setHovered] = React.useState<number | null>(null);
  const [inspected, setInspected] = React.useState<number | null>(null);
  const [dismissed, setDismissed] = React.useState(false);
  const [hidden, setHidden] = React.useState<string[]>([]);
  const [width, setWidth] = React.useState(920);
  const [keyboardActivation, setKeyboardActivation] = React.useState(0);
  const hostRef = React.useRef<HTMLDivElement>(null);
  const pinButtonRef = React.useRef<HTMLButtonElement>(null);
  const bucketButtonRefs = React.useRef(new Map<string, HTMLButtonElement>());
  const invokingBucketKeyRef = React.useRef<string | null>(null);
  const suppressNextBucketFocusRef = React.useRef(false);
  const focusPinAfterOpenRef = React.useRef(false);
  const previousUsageInputRef = React.useRef({ report, scopeKey, group, metric });
  const hoverClearTimer = React.useRef<number | null>(null);
  const pendingHoverTimer = React.useRef<number | null>(null);
  const [escapeDismissed, setEscapeDismissed] = React.useState(false);
  const pinnedIndex = pinnedKey === null ? -1 : report.buckets.findIndex((bucket) => bucket.key === pinnedKey);
  const requestedIndex = dismissed || escapeDismissed
    ? null
    : pinnedIndex >= 0 ? pinnedIndex : inspected ?? hovered;
  const activeIndex = requestedIndex !== null
    && requestedIndex >= 0
    && requestedIndex < report.buckets.length
    ? requestedIndex
    : null;
  const bars = report.window_key === '30d' || report.window_key === '60d';
  const series = React.useMemo(() => seriesFor(report, filter, group, metric, i18n.language), [filter, group, metric, i18n.language, report]);
  const labels = React.useMemo(() => new Map(series.map((item) => [
    item.key,
    item.key === 'total'
      ? metricLabel(metric, t)
      : item.key === 'input'
        ? t('settings.models.usage.series.input')
        : item.key === 'cache'
          ? t('settings.models.usage.series.cache')
          : item.key === 'output'
            ? t('settings.models.usage.series.output')
            : item.key === 'requests'
              ? t('settings.models.usage.series.requests')
              : item.label,
  ])), [metric, series, t]);
  const visibleSeries = series.filter((item) => !hidden.includes(item.key));
  const narrow = width < 560;
  const height = narrow ? 236 : 292;
  const left = narrow ? 40 : 54;
  const right = 16;
  const top = 18;
  const bottom = 38;
  const plotWidth = Math.max(1, width - left - right);
  const plotHeight = Math.max(1, height - top - bottom);
  const lineHitWidth = Math.min(28, Math.max(8, plotWidth / Math.max(1, report.buckets.length - 1) * 0.82));
  const values = report.buckets.map((_, index) => bars
    ? visibleSeries.reduce((sum, item) => sum + (item.values[index] ?? 0), 0)
    : Math.max(0, ...visibleSeries.map((item) => item.values[index] ?? 0)));
  const rawMax = Math.max(1, ...values);
  const scale = 10 ** Math.floor(Math.log10(rawMax));
  const max = Math.ceil(rawMax / scale / 0.5) * scale * 0.5;
  const x = (index: number) => left + (bars
    ? (index + 0.5) / report.buckets.length
    : index / Math.max(1, report.buckets.length - 1)) * plotWidth;
  const y = (value: number) => top + plotHeight * (1 - value / max);
  const bucket = activeIndex === null || activeIndex < 0 ? null : report.buckets[activeIndex];
  const bucketRows = bucket === null ? [] : filterBucketRows(bucket, filter);
  const bucketTotals = aggregateCounters(bucketRows);
  const activeValue = bucket && !bucket.history_complete && bucketRows.length === 0
    ? null
    : usageMetricValue(bucketTotals, metric);
  const currentPartialHour = bucket !== null
    && report.window_key === '24h'
    && activeIndex === report.buckets.length - 1
    && Date.parse(bucket.end_at) - Date.parse(bucket.start_at) < 60 * 60 * 1000;
  const tickIndexes = report.buckets.length <= 7
    ? report.buckets.map((_, index) => index)
    : [...new Set([0, Math.floor((report.buckets.length - 1) / 2), report.buckets.length - 1])];

  const cancelPendingHover = () => {
    if (pendingHoverTimer.current !== null) {
      window.clearTimeout(pendingHoverTimer.current);
      pendingHoverTimer.current = null;
    }
  };
  const cancelHoverClear = () => {
    if (hoverClearTimer.current !== null) {
      window.clearTimeout(hoverClearTimer.current);
      hoverClearTimer.current = null;
    }
  };
  const handleEscape = React.useCallback(() => {
    if (pendingHoverTimer.current !== null) {
      window.clearTimeout(pendingHoverTimer.current);
      pendingHoverTimer.current = null;
    }
    if (hoverClearTimer.current !== null) {
      window.clearTimeout(hoverClearTimer.current);
      hoverClearTimer.current = null;
    }
    const invokingButton = invokingBucketKeyRef.current === null
      ? null
      : bucketButtonRefs.current.get(invokingBucketKeyRef.current) ?? null;
    if (pinnedKey !== null) {
      onPin(null);
    }
    setEscapeDismissed(true);
    setHovered(null);
    setInspected(null);
    setDismissed(true);
    focusPinAfterOpenRef.current = false;
    invokingBucketKeyRef.current = null;
    if (activeIndex !== null && invokingButton !== null
      && hostRef.current?.contains(document.activeElement)) {
      suppressNextBucketFocusRef.current = true;
      invokingButton.focus();
    }
  }, [activeIndex, onPin, pinnedKey]);

  React.useEffect(() => {
    const previousInput = previousUsageInputRef.current;
    const inputChanged = previousInput.report !== report
      || previousInput.scopeKey !== scopeKey
      || previousInput.group !== group
      || previousInput.metric !== metric;
    if (!inputChanged) return;
    const scopeChanged = previousInput.scopeKey !== scopeKey;
    previousUsageInputRef.current = { report, scopeKey, group, metric };
    const invokingKey = invokingBucketKeyRef.current;
    const invokingDetailSurvives = !scopeChanged
      && pinnedKey !== null
      && invokingKey !== null
      && report.buckets.some((currentBucket) => currentBucket.key === invokingKey);
    setHovered(null);
    setInspected(null);
    setDismissed(false);
    setEscapeDismissed(false);
    setHidden([]);
    if (!invokingDetailSurvives) invokingBucketKeyRef.current = null;
    suppressNextBucketFocusRef.current = false;
    focusPinAfterOpenRef.current = false;
  }, [group, metric, pinnedKey, report, scopeKey]);

  React.useEffect(() => {
    if (!focusPinAfterOpenRef.current || activeIndex === null) return;
    focusPinAfterOpenRef.current = false;
    pinButtonRef.current?.focus();
  }, [activeIndex, keyboardActivation]);

  React.useEffect(() => {
    if (!hostRef.current || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      setWidth(Math.max(280, entries[0]?.contentRect.width ?? 920));
    });
    observer.observe(hostRef.current);
    return () => observer.disconnect();
  }, []);

  React.useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (pinnedKey !== null || hostRef.current?.contains(event.target as Node)) return;
      setHovered(null);
      setInspected(null);
      setDismissed(true);
      invokingBucketKeyRef.current = null;
      focusPinAfterOpenRef.current = false;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      handleEscape();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [handleEscape, pinnedKey]);

  React.useEffect(() => () => {
    if (hoverClearTimer.current !== null) window.clearTimeout(hoverClearTimer.current);
    if (pendingHoverTimer.current !== null) window.clearTimeout(pendingHoverTimer.current);
  }, []);

  const scheduleHoverClear = () => {
    if (pinnedKey !== null || inspected !== null) return;
    cancelPendingHover();
    cancelHoverClear();
    hoverClearTimer.current = window.setTimeout(() => {
      hoverClearTimer.current = null;
      setHovered(null);
    }, 100);
  };

  const setHover = (index: number, reopenAfterEscape = false) => {
    if ((escapeDismissed && !reopenAfterEscape) || pinnedKey !== null || inspected !== null) return;
    cancelPendingHover();
    cancelHoverClear();
    const next = Math.max(0, Math.min(report.buckets.length - 1, index));
    if (reopenAfterEscape) setEscapeDismissed(false);
    setDismissed(false);
    if (hovered === null || hovered === next) {
      setHovered(next);
      return;
    }
    pendingHoverTimer.current = window.setTimeout(() => {
      pendingHoverTimer.current = null;
      if (escapeDismissed && !reopenAfterEscape) return;
      setHovered(next);
    }, 100);
  };
  const openDetail = (index: number) => {
    if (pinnedKey !== null) return;
    invokingBucketKeyRef.current = null;
    focusPinAfterOpenRef.current = false;
    setEscapeDismissed(false);
    setDismissed(false);
    setInspected(index);
    setHovered(null);
  };
  const openKeyboardDetail = (index: number) => {
    if (pinnedKey !== null) return;
    invokingBucketKeyRef.current = report.buckets[index]?.key ?? null;
    focusPinAfterOpenRef.current = true;
    setKeyboardActivation((value) => value + 1);
    setEscapeDismissed(false);
    setDismissed(false);
    setInspected(index);
    setHovered(null);
  };
  const handleBucketFocus = (index: number) => {
    if (suppressNextBucketFocusRef.current) {
      suppressNextBucketFocusRef.current = false;
      return;
    }
    setHover(index, true);
  };
  const toggleHidden = (key: string) => {
    setHidden((current) => {
      if (current.includes(key)) return current.filter((item) => item !== key);
      if (current.length >= series.length - 1) return current;
      return [...current, key];
    });
  };
  const svgIndexFromPointer = (event: React.PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = ((event.clientX - bounds.left) / bounds.width) * width;
    const index = bars
      ? Math.floor((position - left) / plotWidth * report.buckets.length)
      : Math.round((position - left) / plotWidth * (report.buckets.length - 1));
    return Math.max(0, Math.min(report.buckets.length - 1, index));
  };
  const pinLabel = pinnedKey !== null
    ? t('settings.models.usage.chart.unpin')
    : t('settings.models.usage.chart.pin');

  return (
    <div className="model-hub-usage-chart-content">
      <ChartLegend
        series={series}
        hidden={hidden}
        onToggle={toggleHidden}
        labels={labels}
        count={count}
        blank={t('settings.models.usage.blank') as string}
        ariaLabel={t('settings.models.usage.chart.legend') as string}
      />
      <div className="model-hub-usage-chart-wrap" ref={hostRef}>
        <svg
          className="model-hub-usage-svg"
          viewBox={`0 0 ${width} ${height}`}
          aria-hidden="true"
          onPointerMove={(event) => {
            if (event.pointerType !== 'touch') setHover(svgIndexFromPointer(event), true);
          }}
          onPointerLeave={scheduleHoverClear}
        >
          <defs>
            <pattern id="usage-unknown-pattern" width="6" height="6" patternUnits="userSpaceOnUse">
              <path d="M-1,1 l2,-2 M0,6 L6,0 M5,7 l2,-2" stroke="var(--model-hub-usage-unknown)" strokeWidth="1" />
            </pattern>
          </defs>
          {[0, 1, 2, 3, 4].map((step) => (
            <g key={step}>
              <line x1={left} x2={width - right} y1={y(max * step / 4)} y2={y(max * step / 4)} className="model-hub-usage-grid" strokeDasharray={step === 0 ? undefined : '3 5'} />
              <text x={left - 10} y={y(max * step / 4) + 4} textAnchor="end" className="model-hub-usage-tick">{count(max * step / 4)}</text>
            </g>
          ))}
          {activeIndex !== null && activeIndex >= 0 && (
            <rect
              x={x(activeIndex) - (bars ? plotWidth / report.buckets.length / 2 : 18)}
              y={top}
              width={bars ? plotWidth / report.buckets.length : 36}
              height={plotHeight}
              className="model-hub-usage-highlight"
              rx="4"
            />
          )}
          {bars
            ? report.buckets.map((currentBucket, index) => {
              let offset = 0;
              return (
                <g key={currentBucket.key}>
                  {visibleSeries.map((item) => {
                    const value = item.values[index];
                    const start = offset;
                    offset += value ?? 0;
                    return (
                      <rect
                        key={item.key}
                        x={x(index) - plotWidth / report.buckets.length * 0.32}
                        y={y(offset)}
                        width={plotWidth / report.buckets.length * 0.64}
                        height={value === null ? 4 : Math.max(0, y(start) - y(offset))}
                        fill={value === null ? 'url(#usage-unknown-pattern)' : SERIES_COLORS[item.colorIndex % SERIES_COLORS.length]}
                        opacity={activeIndex !== null && activeIndex !== index ? 0.48 : 0.88}
                        rx="2"
                      />
                    );
                  })}
                </g>
              );
            })
            : visibleSeries.map((item) => {
              const path = item.values.reduce<string[]>((segments, value, index) => {
                if (value === null) return segments;
                const previous = item.values[index - 1];
                const command = previous === null || previous === undefined ? 'M' : 'L';
                segments.push(`${command}${x(index)},${y(value)}`);
                return segments;
              }, []).join(' ');
              return (
                <g key={item.key}>
                  <path d={path} fill="none" stroke={SERIES_COLORS[item.colorIndex % SERIES_COLORS.length]} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
                  {item.values.map((value, index) => value === null
                    ? <circle key={index} cx={x(index)} cy={y(0)} r="3" className="model-hub-usage-unknown-point" />
                    : <circle key={index} cx={x(index)} cy={y(value)} r={activeIndex === index ? 4.5 : 2.5} fill={SERIES_COLORS[item.colorIndex % SERIES_COLORS.length]} stroke="var(--background)" strokeWidth="1.5" />)}
                </g>
              );
            })}
          {activeIndex !== null && activeIndex >= 0 && (
            <line x1={x(activeIndex)} x2={x(activeIndex)} y1={top} y2={top + plotHeight} className="model-hub-usage-crosshair" />
          )}
          {report.buckets.map((currentBucket, index) => (
            <rect
              key={`hit-${currentBucket.key}`}
              className="model-hub-usage-hit-area"
              x={bars ? x(index) - plotWidth / report.buckets.length / 2 : x(index) - lineHitWidth / 2}
              y={top}
              width={bars ? plotWidth / report.buckets.length : lineHitWidth}
              height={plotHeight}
              onPointerEnter={() => {
                // Removing a dismissed tooltip can expose a stationary pointer.
                // Only a real move, focus, or activation should reopen it.
                setHover(index);
              }}
              onClick={() => openDetail(index)}
            />
          ))}
          {tickIndexes.map((index) => (
            <text
              key={`tick-${report.buckets[index].key}`}
              x={x(index)}
              y={height - 12}
              textAnchor={index === 0 ? 'start' : index === report.buckets.length - 1 ? 'end' : 'middle'}
              className="model-hub-usage-tick"
            >
              {formatBucketAxisLabel(report.buckets[index], i18n.language)}
            </text>
          ))}
        </svg>
        <div
          className="sr-only"
          role="group"
          aria-label={t('settings.models.usage.chart.detail') as string}
        >
          {report.buckets.map((currentBucket, index) => (
            <button
              type="button"
              key={`accessible-${currentBucket.key}`}
              ref={(element) => {
                if (element) bucketButtonRefs.current.set(currentBucket.key, element);
                else bucketButtonRefs.current.delete(currentBucket.key);
              }}
              aria-label={t('settings.models.usage.chart.bucket', {
                bucket: formatBucketRange(currentBucket, i18n.language, true),
              }) as string}
              onPointerEnter={() => {
                setHover(index);
              }}
              onFocus={() => handleBucketFocus(index)}
              onClick={() => openKeyboardDetail(index)}
            >
              {formatBucketRange(currentBucket, i18n.language, true)}
            </button>
          ))}
        </div>
        {bucket && (
          <div
            className={cn('model-hub-usage-tooltip', pinnedKey !== null && 'is-pinned')}
            role="dialog"
            aria-label={t('settings.models.usage.chart.detail') as string}
            data-pinned={pinnedKey !== null ? 'true' : 'false'}
            onPointerEnter={() => {
              cancelPendingHover();
              cancelHoverClear();
            }}
            onPointerLeave={scheduleHoverClear}
            onKeyDownCapture={(event) => {
              if (event.key !== 'Escape') return;
              event.stopPropagation();
              handleEscape();
            }}
          >
            <div className="model-hub-usage-tooltip-head">
              <span>{formatBucketRange(bucket, i18n.language, true)}</span>
              <div className="model-hub-usage-tooltip-actions">
                {pinnedKey !== null && <span>{t('settings.models.usage.chart.pinned')}</span>}
                <button
                  type="button"
                  ref={pinButtonRef}
                  className="model-hub-usage-pin"
                  aria-label={pinLabel as string}
                  aria-pressed={pinnedKey !== null}
                  title={pinLabel as string}
                  onClick={() => onPin(pinnedKey !== null ? null : bucket.key)}
                >
                  {pinnedKey !== null ? <PinOff aria-hidden className="size-3.5" /> : <Pin aria-hidden className="size-3.5" />}
                </button>
              </div>
            </div>
            <div className="model-hub-usage-tooltip-total">
              <strong>{activeValue === null ? t('settings.models.usage.blank') : count(activeValue)}</strong>
              <span>{metricLabel(metric, t)}</span>
            </div>
            <div className="model-hub-usage-tooltip-lines">
              {visibleSeries.map((item) => (
                <div key={item.key}>
                  <span><i style={{ background: SERIES_COLORS[item.colorIndex % SERIES_COLORS.length] }} />{labels.get(item.key) ?? item.label}</span>
                  <b>{item.values[activeIndex as number] === null ? t('settings.models.usage.blank') : count(item.values[activeIndex as number] ?? 0)}</b>
                </div>
              ))}
            </div>
            <div className="model-hub-usage-tooltip-foot">
              <span>
                {bucket.history_complete || bucketRows.length > 0 ? count(bucketTotals.requests) : t('settings.models.usage.blank')}
                {' '}
                {t('settings.models.usage.requests.unit')}
              </span>
              {currentPartialHour && <span>{t('settings.models.usage.chart.currentPartial')}</span>}
              <span>{bucket.history_complete ? t('settings.models.usage.chart.complete') : t('settings.models.usage.chart.partial')}</span>
            </div>
          </div>
        )}
      </div>
      <div className="model-hub-usage-chart-foot">
        <span>{t('settings.models.usage.chart.points', { count: report.buckets.length, granularity: t(`settings.models.usage.granularity.${report.granularity}`) })}</span>
        <span>{hidden.length > 0 ? t('settings.models.usage.chart.legendOnly') : t('settings.models.usage.chart.interaction')}</span>
      </div>
    </div>
  );
}

type TableRow = {
  key: string;
  label: string;
  detail: string;
  counters: UsageCounters;
};

function tableRows(
  report: UsageReport,
  filter: UsageFilter,
  group: 'model' | 'source',
  pinnedKey: string | null,
  locale: string,
): TableRow[] {
  const buckets = pinnedKey === null ? report.buckets : report.buckets.filter((bucket) => bucket.key === pinnedKey);
  const rows = buckets.flatMap((bucket) => filterBucketRows(bucket, filter));
  const grouped = new Map<string, TableRow>();
  const labelContext = usageLabelContext(report, filteredRows(report, filter), locale);
  for (const row of rows) {
    const key = group === 'source' ? row.source_id : pairKey(row.source_id, row.model_id);
    const identity = group === 'source'
      ? null
      : {
        key,
        sourceId: row.source_id,
        modelId: row.model_id,
        sourceLabel: sourceLabel(report, row.source_id),
        modelLabel: modelLabel(report, row.source_id, row.model_id),
      } satisfies UsageIdentity;
    const previous = grouped.get(key);
    grouped.set(key, {
      key,
      label: group === 'source'
        ? sourceIdentityLabel(report, row.source_id, labelContext)
        : identityDisplayLabel(identity!, locale, labelContext),
      detail: group === 'source' ? '' : sourceIdentityLabel(report, row.source_id, labelContext),
      counters: aggregateCounters([...(previous ? [previous.counters] : []), row]),
    });
  }
  return [...grouped.values()];
}

const StatCard: React.FC<{ label: string; value: React.ReactNode; note: React.ReactNode }> = ({ label, value, note }) => (
  <div className="model-hub-usage-stat-card">
    <span className="model-hub-usage-stat-label">{label}</span>
    <strong className="model-hub-usage-stat-value">{value}</strong>
    <span className="model-hub-usage-stat-note">{note}</span>
  </div>
);

export const UsageTab: React.FC<{
  usage: RegionRead<UsageReport>;
  windowKey: UsageWindowKey;
  onWindowChange: (window: UsageWindowKey) => void;
  onRetry?: () => void | Promise<void>;
}> = ({ usage: usageRead, windowKey, onWindowChange, onRetry }) => {
  const { t, i18n } = useUsageTranslation();
  const count = useCount();
  const report = foldRegionRead<UsageReport, UsageReport | null>(usageRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: (staleData) => staleData,
  });
  const [sourceIds, setSourceIds] = React.useState<string[]>([]);
  const [modelKeys, setModelKeys] = React.useState<string[]>([]);
  const [metric, setMetric] = React.useState<UsageMetric>('tokens');
  const [group, setGroup] = React.useState<UsageGroup>('type');
  const [tableGroup, setTableGroup] = React.useState<'model' | 'source'>('model');
  const [sortAscending, setSortAscending] = React.useState(false);
  const [pinnedKey, setPinnedKey] = React.useState<string | null>(null);
  const scopeKey = JSON.stringify([windowKey, sourceIds, modelKeys, metric, group]);

  React.useEffect(() => {
    setSourceIds([]);
    setModelKeys([]);
    setPinnedKey(null);
  }, [windowKey]);

  React.useEffect(() => {
    setPinnedKey(null);
  }, [scopeKey]);

  React.useEffect(() => {
    if (report !== null && pinnedKey !== null && !report.buckets.some((bucket) => bucket.key === pinnedKey)) {
      setPinnedKey(null);
    }
  }, [report, pinnedKey]);

  if (report === null) {
    return (
      <div className="model-hub-usage-analytics">
        <UsageHeading report={null} windowKey={windowKey} onWindowChange={onWindowChange} />
        {regionFailed(usageRead) ? (
          <FailureState onRetry={onRetry} />
        ) : (
          <div className="model-hub-usage-loading" role="status">
            <LoaderCircle className="size-4 animate-spin" />
            {t('common.loading')}
          </div>
        )}
      </div>
    );
  }

  const allRows = filteredRows(report, { sourceIds: [], modelKeys: [] });
  const labelContext = usageLabelContext(report, allRows, i18n.language);
  const sourceOptions: FilterOption[] = report.sources.map((source) => ({
    key: source.source_id,
    label: sourceIdentityLabel(report, source.source_id, labelContext),
  }));
  const modelOptions = [...new Map(
    allRows.map((row) => {
      const key = pairKey(row.source_id, row.model_id);
      const identity = {
        key,
        sourceId: row.source_id,
        modelId: row.model_id,
        sourceLabel: sourceLabel(report, row.source_id),
        modelLabel: modelLabel(report, row.source_id, row.model_id),
      } satisfies UsageIdentity;
      return [key, {
        key,
        label: identityDisplayLabel(identity, i18n.language, labelContext),
        detail: identity.modelLabel ? undefined : t('settings.models.usage.unknownModel'),
      }];
    }),
  ).values()];
  const filter: UsageFilter = { sourceIds, modelKeys };
  const scopedRows = filteredRows(report, filter);
  const totals = aggregateCounters(scopedRows);
  const partialHistory = reportHasPartialHistory(report, filter);
  const reportEmpty = usageIsEmpty(report);
  const filteredEmpty = !reportEmpty && scopedRows.length === 0 && !partialHistory;
  const historyOnlyUnknown = partialHistory && scopedRows.length === 0;
  const pinnedBucket = pinnedKey === null
    ? null
    : report.buckets.find((bucket) => bucket.key === pinnedKey) ?? null;
  const activePinnedKey = pinnedBucket?.key ?? null;
  const rows = tableRows(report, filter, tableGroup, activePinnedKey, i18n.language);
  const sortValue = (row: TableRow): number => usageMetricValue(row.counters, metric) ?? -1;
  const sortedRows = [...rows].sort((left, right) => (sortValue(right) - sortValue(left)) * (sortAscending ? -1 : 1));
  const tableTotal = aggregateCounters(
    (activePinnedKey === null ? report.buckets : report.buckets.filter((bucket) => bucket.key === activePinnedKey))
      .flatMap((bucket) => filterBucketRows(bucket, filter)),
  );
  const metricText = (counters: UsageCounters, selectedMetric = metric) =>
    tokenText(counters, selectedMetric, count, t('settings.models.usage.blank') as string);
  const partialHistoryNote = String(t('settings.models.usage.partialHistory'));
  const unknownTokensNote = String(t('settings.models.usage.unknownTokens'));
  const allReportedNote = String(t('settings.models.usage.stats.allReported'));

  const exportCsv = () => {
    const csv = buildUsageCsv(report, filter, activePinnedKey, {
      bucketKey: t('settings.models.usage.csv.bucketKey') as string,
      startAt: t('settings.models.usage.csv.startAt') as string,
      endAt: t('settings.models.usage.csv.endAt') as string,
      historyComplete: t('settings.models.usage.csv.historyComplete') as string,
      sourceId: t('settings.models.usage.csv.sourceId') as string,
      modelId: t('settings.models.usage.csv.modelId') as string,
      ledgerKey: t('settings.models.usage.csv.ledgerKey') as string,
      sourceLabel: t('settings.models.usage.csv.sourceLabel') as string,
      modelLabel: t('settings.models.usage.csv.modelLabel') as string,
      requests: t('settings.models.usage.table.requests') as string,
      tokenReports: t('settings.models.usage.csv.tokenReports') as string,
      inputTokens: t('settings.models.usage.csv.inputTokens') as string,
      nonCachedInputTokens: t('settings.models.usage.csv.nonCachedInputTokens') as string,
      cachedInputTokens: t('settings.models.usage.csv.cachedInputTokens') as string,
      outputTokens: t('settings.models.usage.csv.outputTokens') as string,
      totalTokens: t('settings.models.usage.csv.totalTokens') as string,
    }, t('settings.models.usage.unknownModel') as string);
    const url = URL.createObjectURL(new Blob([`\ufeff${csv}`], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `model-usage-${report.window_key}.csv`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  return (
    <div className="model-hub-usage-analytics">
      <UsageHeading report={report} windowKey={windowKey} onWindowChange={onWindowChange} />
      {regionFailed(usageRead) && <FailureState onRetry={onRetry} />}
      <div className="model-hub-usage-filter-bar">
        <Filter aria-hidden className="size-3.5 text-muted" />
        <MultiFilter
          label={t('settings.models.usage.filters.source') as string}
          icon={<Network aria-hidden className="size-3.5" />}
          options={sourceOptions}
          selected={sourceIds}
          onChange={setSourceIds}
        />
        <MultiFilter
          label={t('settings.models.usage.filters.model') as string}
          icon={<Cpu aria-hidden className="size-3.5" />}
          options={modelOptions}
          selected={modelKeys}
          onChange={setModelKeys}
        />
        <div className="model-hub-usage-select">
          <Zap aria-hidden className="size-3.5" />
          <select
            aria-label={t('settings.models.usage.metric.label') as string}
            value={metric}
            onChange={(event) => {
              const next = event.target.value as UsageMetric;
              setMetric(next);
              if (next === 'requests' && group === 'type') setGroup('total');
            }}
          >
            {metricKeys.map((key) => <option value={key} key={key}>{metricLabel(key, t)}</option>)}
          </select>
          <ChevronDown aria-hidden className="size-3" />
        </div>
        {(sourceIds.length > 0 || modelKeys.length > 0 || metric !== 'tokens') && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="model-hub-usage-reset"
            onClick={() => { setSourceIds([]); setModelKeys([]); setMetric('tokens'); setGroup('type'); }}
          >
            <X aria-hidden className="size-3" />
            {t('settings.models.usage.filters.reset')}
          </Button>
        )}
        <span className="model-hub-usage-filter-context">
          {sourceIds.length > 0 ? t('settings.models.usage.filters.sourceCount', { count: sourceIds.length }) : t('settings.models.usage.filters.allSources')}
          <span>·</span>
          {modelKeys.length > 0 ? t('settings.models.usage.filters.modelCount', { count: modelKeys.length }) : t('settings.models.usage.filters.allModels')}
        </span>
      </div>

      {reportEmpty ? (
        <EmptyState kind="report" />
      ) : filteredEmpty ? (
        <EmptyState kind="filter" onReset={() => { setSourceIds([]); setModelKeys([]); }} />
      ) : (
        <>
          <div className="model-hub-usage-stat-grid">
            <StatCard
              label={t('settings.models.usage.stats.tokens')}
              value={historyOnlyUnknown ? t('settings.models.usage.blank') : metricText(totals, 'tokens')}
              note={!historyOnlyUnknown && usageTokensAreKnown(totals)
                ? t('settings.models.usage.stats.tokenSplit', { input: count(totals.input_tokens), output: count(totals.output_tokens) })
                : historyOnlyUnknown ? partialHistoryNote : unknownTokensNote}
            />
            <StatCard
              label={t('settings.models.usage.stats.requests')}
              value={historyOnlyUnknown ? t('settings.models.usage.blank') : count(totals.requests)}
              note={!historyOnlyUnknown && usageReportShortfall(totals) > 0
                ? t('settings.models.usage.stats.shortfall', { count: usageReportShortfall(totals) })
                : historyOnlyUnknown ? partialHistoryNote : allReportedNote}
            />
            <StatCard
              label={t('settings.models.usage.stats.cached')}
              value={historyOnlyUnknown || usageCachedInputShare(totals) === null ? t('settings.models.usage.blank') : `${((usageCachedInputShare(totals) ?? 0) * 100).toFixed(1)}%`}
              note={historyOnlyUnknown || usageCachedInputShare(totals) === null
                ? t('settings.models.usage.stats.cacheUnknown')
                : t('settings.models.usage.stats.cacheSplit', { cached: count(totals.cached_input_tokens), input: count(totals.input_tokens) })}
            />
          </div>
          {(partialHistory || reportHasUnknownTokens(report, filter)) && (
            <div className="model-hub-usage-notice" role="status">
              <CircleHelp aria-hidden className="size-3.5 shrink-0" />
              <span>
                {partialHistory && t('settings.models.usage.partialHistory')}
                {partialHistory && reportHasUnknownTokens(report, filter) && ' '}
                {reportHasUnknownTokens(report, filter) && t('settings.models.usage.unknownTokens')}
              </span>
            </div>
          )}
          <section className="model-hub-usage-card" aria-labelledby="model-hub-usage-chart-title">
            <div className="model-hub-usage-card-header">
              <div>
                <h3 id="model-hub-usage-chart-title">{t('settings.models.usage.chart.title')} <span>{t(`settings.models.usage.granularity.${report.granularity}`)}</span></h3>
                <p>{t('settings.models.usage.chart.subtitle')}</p>
              </div>
              <div className="model-hub-usage-group-control">
                <span>{t('settings.models.usage.group.label')}</span>
                <div role="group" aria-label={t('settings.models.usage.group.label') as string}>
                  {groupKeys.map((key) => (
                    <button
                      type="button"
                      key={key}
                      aria-pressed={group === key}
                      disabled={key === 'type' && metric === 'requests'}
                      className={cn(group === key && 'is-selected')}
                      onClick={() => setGroup(key)}
                    >
                      {t(`settings.models.usage.group.${key}`)}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <UsageChart
              report={report}
              filter={filter}
              metric={metric}
              group={group}
              pinnedKey={activePinnedKey}
              scopeKey={scopeKey}
              onPin={setPinnedKey}
            />
          </section>
          <section className="model-hub-usage-card model-hub-usage-details" aria-labelledby="model-hub-usage-details-title">
            <div className="model-hub-usage-card-header model-hub-usage-details-header">
              <div>
                <h3 id="model-hub-usage-details-title">{t('settings.models.usage.table.title')} <span>{sortedRows.length}</span></h3>
                <p>{activePinnedKey === null
                  ? t('settings.models.usage.table.range')
                  : t('settings.models.usage.table.bucket', { bucket: formatBucketLabel(pinnedBucket!, i18n.language) })}</p>
              </div>
              <div className="model-hub-usage-details-actions">
                {activePinnedKey !== null && <Button type="button" variant="ghost" size="sm" onClick={() => setPinnedKey(null)}><X aria-hidden className="size-3" />{t('settings.models.usage.table.allBuckets')}</Button>}
                <div className="model-hub-usage-table-group" role="group" aria-label={t('settings.models.usage.table.group') as string}>
                  <button type="button" className={tableGroup === 'model' ? 'is-selected' : ''} aria-pressed={tableGroup === 'model'} onClick={() => setTableGroup('model')}>{t('settings.models.usage.table.byModel')}</button>
                  <button type="button" className={tableGroup === 'source' ? 'is-selected' : ''} aria-pressed={tableGroup === 'source'} onClick={() => setTableGroup('source')}>{t('settings.models.usage.table.bySource')}</button>
                </div>
                <Button type="button" variant="outline" size="sm" onClick={exportCsv}><ArrowDownToLine aria-hidden className="size-3.5" />{t('settings.models.usage.table.export')}</Button>
              </div>
            </div>
            <div className="model-hub-usage-table-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t('settings.models.usage.table.identity')}</th>
                    <th scope="col">{t('settings.models.usage.table.requests')}</th>
                    <th scope="col">{t('settings.models.usage.table.input')}</th>
                    <th scope="col">{t('settings.models.usage.table.nonCache')}</th>
                    <th scope="col">{t('settings.models.usage.table.cache')}</th>
                    <th scope="col">{t('settings.models.usage.table.output')}</th>
                    <th scope="col">
                      <button type="button" onClick={() => setSortAscending((value) => !value)}>
                        {metricLabel(metric, t)} <ArrowDown aria-hidden className={cn('size-3', sortAscending && 'rotate-180')} />
                      </button>
                    </th>
                    <th scope="col">{t('settings.models.usage.table.share')}</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((row) => {
                    const value = usageMetricValue(row.counters, metric);
                    const totalValue = usageMetricValue(tableTotal, metric);
                    const share = value === null || totalValue === null || totalValue === 0 ? null : value / totalValue;
                    return (
                      <tr key={row.key}>
                        <th scope="row">
                          <span className="model-hub-usage-row-name">{row.label}</span>
                          {row.detail && <span className="model-hub-usage-row-detail">{row.detail}</span>}
                        </th>
                        <td>{count(row.counters.requests)}</td>
                        <td>{tokenText(row.counters, 'input', count, t('settings.models.usage.blank') as string)}</td>
                        <td>{usageTokensAreKnown(row.counters) ? count(usageNonCachedInput(row.counters)) : t('settings.models.usage.blank')}</td>
                        <td>{tokenText(row.counters, 'cache', count, t('settings.models.usage.blank') as string)}</td>
                        <td>{tokenText(row.counters, 'output', count, t('settings.models.usage.blank') as string)}</td>
                        <td className="model-hub-usage-table-total">{value === null ? t('settings.models.usage.blank') : count(value)}</td>
                        <td>{share === null ? t('settings.models.usage.blank') : `${(share * 100).toFixed(1)}%`}</td>
                      </tr>
                    );
                  })}
                  {sortedRows.length === 0 && <tr><td colSpan={8} className="model-hub-usage-table-empty">{t('settings.models.usage.table.empty')}</td></tr>}
                </tbody>
                {sortedRows.length > 0 && (
                  <tfoot>
                    <tr>
                      <th scope="row">{t('settings.models.usage.table.total')}</th>
                      <td>{count(tableTotal.requests)}</td>
                      <td>{tokenText(tableTotal, 'input', count, t('settings.models.usage.blank') as string)}</td>
                      <td>{usageTokensAreKnown(tableTotal) ? count(usageNonCachedInput(tableTotal)) : t('settings.models.usage.blank')}</td>
                      <td>{tokenText(tableTotal, 'cache', count, t('settings.models.usage.blank') as string)}</td>
                      <td>{tokenText(tableTotal, 'output', count, t('settings.models.usage.blank') as string)}</td>
                      <td>{metricText(tableTotal)}</td>
                      <td>{(() => {
                        const totalValue = usageMetricValue(tableTotal, metric);
                        return totalValue === null || totalValue === 0
                          ? t('settings.models.usage.blank')
                          : '100%';
                      })()}</td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
            <div className="model-hub-usage-table-foot">
              <CircleHelp aria-hidden className="size-3.5 shrink-0" />
              {t('settings.models.usage.table.accounting')}
            </div>
          </section>
        </>
      )}
    </div>
  );
};

function UsageHeading({
  report,
  windowKey,
  onWindowChange,
}: {
  report: UsageReport | null;
  windowKey: UsageWindowKey;
  onWindowChange: (window: UsageWindowKey) => void;
}) {
  const { t, i18n } = useUsageTranslation();
  return (
    <div className="model-hub-usage-heading">
      <div>
        <h2>{t('settings.models.usage.title')}</h2>
        <p>
          {report
            ? formatBucketRange({ start_at: report.from_at, end_at: report.to_at, key: report.from_at, history_complete: true, rows: [] }, i18n.language, true)
            : t('settings.models.usage.detail')}
        </p>
      </div>
      <SegmentedRadio
        value={windowKey}
        onChange={onWindowChange}
        options={USAGE_WINDOW_OPTIONS.map((key) => ({ id: key, label: t(`settings.models.usage.window.${key}`) as string }))}
        ariaLabel={t('settings.models.usage.window.label') as string}
        className="model-hub-usage-window"
      />
    </div>
  );
}

function FailureState({ onRetry }: { onRetry?: () => void | Promise<void> }) {
  const { t } = useUsageTranslation();
  return (
    <div className="model-hub-usage-failure" role="alert">
      <span>{t('settings.models.usage.failure')}</span>
      <button type="button" onClick={() => void onRetry?.()}>{t('settings.models.usage.retry')}</button>
    </div>
  );
}

function EmptyState({ kind, onReset }: { kind: 'report' | 'filter'; onReset?: () => void }) {
  const { t } = useUsageTranslation();
  return (
    <div className="model-hub-usage-empty-state">
      <Search aria-hidden className="size-7" />
      <h3>{t(`settings.models.usage.empty.${kind}.title`)}</h3>
      <p>{t(`settings.models.usage.empty.${kind}.body`)}</p>
      {onReset && <Button type="button" variant="outline" onClick={onReset}>{t('settings.models.usage.filters.reset')}</Button>}
    </div>
  );
}
