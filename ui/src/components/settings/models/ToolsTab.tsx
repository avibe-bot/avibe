// 工具 — the hosted tools Vibey may call (`vibey-hosted-tools.md` §6.1, design.pen
// frames E5IkUQ and XZjhj).
//
// One card per catalog tool: its toggle (off by default), the (Source, model)
// pairs that could run it natively with their evidence and a check for each, and
// the proxy executor that runs it for every other model. The read is a report:
// hop resolution reads config and evidence itself, so nothing on this tab decides
// a route, and every claim about one comes from `bound`, the server's answer.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, AtSign, Ban, Check, Globe, Image as ImageIcon, Info, Loader2, Radar, RotateCw, TriangleAlert, Waypoints } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { ToggleSwitch } from '../SettingsPrimitives';
import { formatMonthDay, formatNameList, formatSmallUsd } from './format';
import { HostedProbeDialog } from './HostedProbeDialog';
import {
  evidenceChip,
  pendingChecks,
  probeableTool,
  probeKey,
  probeTotal,
  sameExecutor,
  verifiedCandidates,
  type HostedEvidenceChip,
  type ProbeTarget,
} from './hostedTools';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import { ACCENT_ICON, ACCENT_PILL, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type {
  HostedToolCandidate,
  HostedToolName,
  HostedToolPriceUnit,
  HostedToolProbeResult,
  HostedToolPut,
  HostedToolsStatus,
  HostedToolStatus,
  Source,
} from './types';
import { useEstimateLabel } from './useEstimateLabel';

type RunProbe = (target: ProbeTarget) => Promise<HostedToolProbeResult | null>;

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generation: ImageIcon,
};

const AUTOMATIC = '';
const executorValue = (executor: { source_id: string; model_id: string } | null): string =>
  executor ? JSON.stringify([executor.source_id, executor.model_id]) : AUTOMATIC;

/** The native-run cell: the call evidence, or 「未检测」 when there is none. */
export const NativeEvidenceChip: React.FC<{ chip: HostedEvidenceChip | null }> = ({ chip }) => {
  const { t } = useTranslation();
  if (chip === null) {
    return <span className="model-hub-pill model-hub-fill-0a border border-border text-muted">{t('settings.models.tools.chip.unchecked')}</span>;
  }
  const date = chip.at ? formatMonthDay(chip.at) : '';
  if (chip.kind === 'engineUpgraded') {
    return <span className="model-hub-pill model-hub-status-gold gap-1 border"><TriangleAlert className="size-3" aria-hidden />{t('settings.models.tools.chip.engineUpgraded')}</span>;
  }
  if (chip.aged) {
    return <span className="model-hub-pill model-hub-status-gold gap-1 border"><RotateCw className="size-3" aria-hidden />{t(chip.kind === 'native' ? 'settings.models.tools.chip.reverify' : 'settings.models.tools.chip.unsupportedAged', { date })}</span>;
  }
  return chip.kind === 'native'
    ? <span className="model-hub-pill model-hub-status-mint gap-1 border"><Check className="size-3" aria-hidden />{t('settings.models.tools.chip.native', { date })}</span>
    : <span className="model-hub-pill model-hub-fill-0a gap-1 border border-border text-muted"><Ban className="size-3" aria-hidden />{t('settings.models.tools.chip.unsupported')}</span>;
};

/**
 * What a call costs on this row: included on a subscription, else the overlay's
 * list price per unit (an estimate; a relay may bill differently). An image has
 * no per-image list price (it is billed per image token), and a price nobody
 * lists is said to be unknown, never shown as zero.
 */
const CostCell: React.FC<{ tool: HostedToolName; candidate: HostedToolCandidate }> = ({ tool, candidate }) => {
  const { t } = useTranslation();
  if (candidate.billing === 'monthly') {
    return <span className="flex min-w-0 flex-col"><span className="font-semibold text-foreground">{t('settings.models.tools.cost.included')}</span><span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.includedNote')}</span></span>;
  }
  if (tool === 'image_generation') {
    return <span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.imageTokens')}</span>;
  }
  const price = candidate.price;
  if (!price) return <span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.unknown')}</span>;
  const perUnit = (usdPer1k: number, unit: HostedToolPriceUnit) => t('settings.models.tools.cost.perUnit', {
    usd: formatSmallUsd(usdPer1k / 1000),
    unit: t(`settings.models.tools.cost.unit.${unit}`),
  }) as string;
  return (
    <span className="flex min-w-0 flex-col">
      <span className="font-semibold text-foreground">{t('settings.models.tools.cost.list', { price: perUnit(price.usd_per_1k, price.unit) })}</span>
      <span className="model-hub-tool-cost-note">
        {[t('settings.models.tools.cost.listNote'), ...price.extra.map((extra) => `+ ${perUnit(extra.usd_per_1k, extra.unit)}`)].join(' · ')}
      </span>
    </span>
  );
};

/** A row's check: run it now, with its cost on the button (§6.1: the probe's estimate shows first). */
const ProbeCell: React.FC<{
  target: ProbeTarget;
  running: boolean;
  last: HostedToolProbeResult | null | undefined;
  onProbe: RunProbe;
  disabled: boolean;
}> = ({ target, running, last, onProbe, disabled }) => {
  const { t } = useTranslation();
  const estimate = useEstimateLabel()(target.candidate.probe_estimate);
  const checked = target.candidate.call.basis !== null;
  const unanswered = last !== undefined && (last === null || last.call.outcome === 'inconclusive' || last.call.outcome === 'transient');
  const label = running
    ? t('settings.models.tools.probe.running')
    : unanswered
      ? t('settings.models.tools.probe.retry')
      : checked
        ? t('settings.models.tools.probe.recheck')
        : estimate
          ? t('settings.models.tools.probe.checkCost', { cost: estimate })
          : t('settings.models.tools.probe.check');
  return (
    <span className="flex min-w-0 flex-col items-start gap-1 md:items-end">
      <Button
        variant="outline"
        size="xs"
        disabled={disabled || running}
        aria-busy={running}
        onClick={() => void onProbe(target)}
        title={estimate ?? undefined}
      >
        {running ? <Loader2 className="animate-spin" aria-hidden /> : checked || unanswered ? <RotateCw aria-hidden /> : <Radar aria-hidden />}
        {label}
      </Button>
      {unanswered && (
        <span className="model-hub-tool-cost-note flex items-center gap-1 model-hub-ink-gold" role="status">
          <AlertCircle className="size-3 shrink-0" aria-hidden />
          {last?.call.outcome === 'inconclusive'
            ? t('settings.models.tools.probe.outcome.inconclusive')
            : last?.call.error
              ? t('settings.models.tools.probe.outcome.transientReason', { reason: t(last.call.error, { defaultValue: last.call.error }) })
              : t('settings.models.tools.probe.outcome.transient')}
        </span>
      )}
    </span>
  );
};

const CandidateRow: React.FC<{
  tool: HostedToolName;
  candidate: HostedToolCandidate;
  source: Source | undefined;
  executor: boolean;
  probe: { running: boolean; last: HostedToolProbeResult | null | undefined; run: RunProbe; disabled: boolean } | null;
}> = ({ tool, candidate, source, executor, probe }) => {
  const { t } = useTranslation();
  const { Icon, accent } = source ? sourceVisual(source) : { Icon: Globe, accent: 'muted' as const };
  const name = source?.display_name ?? candidate.source_id;
  return (
    <div className="model-hub-tool-row grid items-center rounded-lg border border-border bg-background" data-candidate={`${candidate.source_id}:${candidate.model_id}`}>
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className={cn('flex size-[26px] shrink-0 items-center justify-center rounded-[7px]', ACCENT_TILE[accent])}><Icon className={cn('size-[13px]', ACCENT_ICON[accent])} /></span>
        <span className="truncate font-semibold text-foreground" title={name}>{name}</span>
        <span className="truncate font-mono text-muted" title={candidate.model_id}>{candidate.model_id}</span>
        {source && <span className={cn('model-hub-pill border', ACCENT_PILL[source.kind === 'api_key' ? 'cyan' : accent])}>{t(`settings.models.upstream.kind.${source.kind === 'subscription' ? 'subscription' : 'apiKey'}`)}</span>}
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-1.5">
        <NativeEvidenceChip chip={evidenceChip(candidate.call)} />
        {executor && <span className="model-hub-pill gap-1 model-hub-accent-pill--cyan border"><Waypoints className="size-3" aria-hidden />{t('settings.models.tools.chip.executor')}</span>}
      </span>
      <CostCell tool={tool} candidate={candidate} />
      {probe
        ? <ProbeCell target={{ tool, candidate }} running={probe.running} last={probe.last} onProbe={probe.run} disabled={probe.disabled} />
        : <span />}
    </div>
  );
};

const ToolCard: React.FC<{
  tool: HostedToolStatus;
  sources: ReadonlyMap<string, Source>;
  hubSources: readonly Source[];
  pending: boolean;
  probing: ReadonlySet<string>;
  lastProbes: ReadonlyMap<string, HostedToolProbeResult | null>;
  onChange: (tool: HostedToolName, body: HostedToolPut) => void;
  onProbe: RunProbe;
}> = ({ tool, sources, hubSources, pending, probing, lastProbes, onChange, onProbe }) => {
  const { t, i18n } = useTranslation();
  const Icon = TOOL_ICON[tool.name];
  const name = t(`settings.models.tools.tool.${tool.name}.name`) as string;
  const toolInline = t(`settings.models.tools.tool.${tool.name}.inline`) as string;
  const verified = verifiedCandidates(tool);
  // What a turn would bind now: the server's own answer, so the card claims no
  // routing it cannot see.
  const bound = tool.bound;
  const sourceName = (id: string) => sources.get(id)?.display_name ?? id;
  // Sources with no (Source, model) pair that could run this tool natively.
  const offering = new Set(tool.candidates.map((candidate) => candidate.source_id));
  const withoutNative = hubSources.filter((source) => !offering.has(source.id)).map((source) => source.display_name);
  // The saved executor as the server would accept it back: a pair still among the
  // candidates. One that left them (its Source or model went away, or the overlay
  // stopped offering the tool there) is refused on write, and resolution already
  // skips it, so it reads and writes as Automatic.
  const savedExecutor = tool.executor && tool.candidates.some((candidate) => sameExecutor(candidate, tool.executor))
    ? tool.executor
    : null;
  // A stored choice that is no longer verified still shows, so the picker never
  // silently reads as a different executor than the one saved.
  const options = savedExecutor && !verified.some((candidate) => sameExecutor(candidate, savedExecutor))
    ? [...verified, { ...savedExecutor, stale: true as const }]
    : verified;
  // With nothing verified there is nothing to pick, but a saved choice still reads as itself.
  const pickable = verified.length > 0;
  // Turning a tool on needs a verified pair to run it: on with none, every call
  // would fail. Turning it off is always possible.
  const locked = !tool.enabled && !pickable;
  return (
    <section className="model-hub-tool-card overflow-hidden rounded-xl border border-border bg-surface" aria-label={name} data-tool={tool.name}>
      <header className="model-hub-tool-card-head flex items-center gap-3 border-b border-border">
        <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE.cyan)}><Icon className={cn('size-[17px]', ACCENT_ICON.cyan)} /></span>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <h3 className="model-hub-tool-name font-bold text-foreground">{name}</h3>
            <code className="model-hub-pill model-hub-fill-0a border border-border font-mono font-normal text-muted">{tool.name}</code>
          </span>
          <span className="model-hub-tool-description">{t(`settings.models.tools.tool.${tool.name}.description`)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2.5">
          <span className={cn('model-hub-tool-state font-semibold', tool.enabled ? 'model-hub-ink-mint' : 'text-muted')}>{t(tool.enabled ? 'settings.models.tools.state.on' : locked ? 'settings.models.tools.state.needsCheck' : 'settings.models.tools.state.off')}</span>
          <ToggleSwitch
            enabled={tool.enabled}
            disabled={pending || locked}
            label={t(tool.enabled ? 'settings.models.tools.toggle.turnOff' : 'settings.models.tools.toggle.turnOn', { name }) as string}
            onClick={() => onChange(tool.name, { enabled: !tool.enabled, executor: savedExecutor })}
          />
        </span>
      </header>
      <div className="model-hub-tool-table flex flex-col gap-2">
        {tool.candidates.length === 0
          ? <p className="model-hub-tool-note flex items-start gap-2"><Info className="mt-px size-[13px] shrink-0" aria-hidden />{t('settings.models.tools.empty', { toolInline })}</p>
          : <>
              <div className="model-hub-tool-table-head hidden font-semibold md:grid" aria-hidden>
                <span>{t('settings.models.tools.col.source')}</span>
                <span>{t('settings.models.tools.col.native')}</span>
                <span>{t('settings.models.tools.col.cost')}</span>
                <span />
              </div>
              {tool.candidates.map((candidate) => {
                const key = probeKey({ tool: tool.name, candidate });
                return (
                  <CandidateRow
                    key={key}
                    tool={tool.name}
                    candidate={candidate}
                    source={sources.get(candidate.source_id)}
                    executor={tool.enabled && sameExecutor(candidate, bound)}
                    probe={probeableTool(tool.name)
                      ? { running: probing.has(key), last: lastProbes.get(key), run: onProbe, disabled: probing.size > 0 && !probing.has(key) }
                      : null}
                  />
                );
              })}
              {withoutNative.length > 0 && <p className="model-hub-tool-note flex items-start gap-2"><Info className="mt-px size-[13px] shrink-0" aria-hidden />{t('settings.models.tools.noNative', { sources: formatNameList(withoutNative, i18n.language), toolInline })}</p>}
            </>}
      </div>
      <div className="model-hub-tool-executor flex flex-col gap-2 border-t border-border sm:flex-row sm:items-center sm:gap-3">
        <span className="shrink-0 font-semibold text-foreground">{t('settings.models.tools.executor.label')}</span>
        <Select
          wrapperClassName="sm:w-[330px] shrink-0"
          className="h-8 text-[12px]"
          aria-label={t('settings.models.tools.executor.pick', { name }) as string}
          disabled={pending || !pickable}
          value={executorValue(savedExecutor)}
          onChange={(event) => {
            const picked = options.find((option) => executorValue(option) === event.target.value);
            onChange(tool.name, {
              enabled: tool.enabled,
              executor: picked ? { source_id: picked.source_id, model_id: picked.model_id } : null,
            });
          }}
        >
          {!pickable && savedExecutor === null
            ? <option value={AUTOMATIC}>{t('settings.models.tools.executor.unavailable')}</option>
            : <>
                {pickable && (
                  <option value={AUTOMATIC}>
                    {/* Automatic names what it resolves to now, when the saved choice is Automatic. */}
                    {savedExecutor === null && bound
                      ? t('settings.models.tools.executor.automaticBound', { source: sourceName(bound.source_id), model: bound.model_id })
                      : t('settings.models.tools.executor.automatic')}
                  </option>
                )}
                {options.map((option) => (
                  <option key={executorValue(option)} value={executorValue(option)}>
                    {'stale' in option
                      ? t('settings.models.tools.executor.notVerified', { source: sourceName(option.source_id), model: option.model_id })
                      : t('settings.models.tools.executor.option', { source: sourceName(option.source_id), model: option.model_id })}
                  </option>
                ))}
              </>}
        </Select>
        <span className="model-hub-tool-note">
          {/* A routing claim holds only while the tool is on: resolution leaves an
              off tool out entirely, so its executor is just a saved preference. */}
          {!tool.enabled
            ? t('settings.models.tools.executor.note.off')
            : bound
              ? t('settings.models.tools.executor.note.bound', { source: sourceName(bound.source_id) })
              : t('settings.models.tools.executor.note.unbound')}
        </span>
      </div>
    </section>
  );
};

export const ToolsTab: React.FC<{
  read: RegionRead<HostedToolsStatus>;
  /** The Sources read, for names and glyphs; candidates carry only ids. */
  sources: readonly Source[];
  pending: ReadonlySet<HostedToolName>;
  /** Probe keys (`probeKey`) running now. */
  probing: ReadonlySet<string>;
  onChange: (tool: HostedToolName, body: HostedToolPut) => void;
  onProbe: RunProbe;
  onRetry: () => void;
}> = ({ read, sources, pending, probing, onChange, onProbe, onRetry }) => {
  const { t } = useTranslation();
  const estimateLabel = useEstimateLabel();
  // A row's last answer that did not become evidence (inconclusive, transient, a
  // failed request) shows where it was asked, until that row is checked again.
  const [lastProbes, setLastProbes] = React.useState<ReadonlyMap<string, HostedToolProbeResult | null>>(() => new Map());
  const [checking, setChecking] = React.useState<readonly ProbeTarget[] | null>(null);
  const runProbe = React.useCallback<RunProbe>(async (target) => {
    const result = await onProbe(target);
    setLastProbes((previous) => new Map(previous).set(probeKey(target), result));
    return result;
  }, [onProbe]);
  const status = foldRegionRead<HostedToolsStatus, HostedToolsStatus | null>(read, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: (staleData) => staleData,
  });
  const byId = React.useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);
  const hubSources = React.useMemo(() => sources.filter((source) => source.supply_channel === 'hub'), [sources]);
  const reverify = status?.reverify;
  const toCheck = status ? pendingChecks(status) : [];
  // 「全部已检测」 holds only where there is something a check could run on.
  const checkable = status?.tools.some((tool) => probeableTool(tool.name) && tool.candidates.length > 0) ?? false;
  const toCheckTotal = probeTotal(toCheck);
  const toCheckCost = toCheckTotal === null
    ? t('settings.models.tools.header.totalUnknown')
    : toCheckTotal.basis === 'included'
      ? t('settings.models.tools.header.totalIncluded')
      : t('settings.models.tools.header.total', { cost: estimateLabel(toCheckTotal) });
  return (
    <section className="model-hub-tools flex flex-col" aria-label={t('settings.models.tools.title') as string}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="model-hub-tools-title font-bold text-foreground">{t('settings.models.tools.title')}</h2>
          <p className="model-hub-tools-subtitle">{t('settings.models.tools.subtitle')}</p>
        </div>
        <div className="flex shrink-0 flex-col items-start gap-1.5 sm:items-end">
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {checkable && (
              <span className="model-hub-tools-reverify">
                {toCheck.length > 0
                  ? `${t('settings.models.tools.header.pending', { count: toCheck.length })} · ${toCheckCost}`
                  : t('settings.models.tools.header.none')}
              </span>
            )}
            <Button
              size="sm"
              variant={toCheck.length > 0 ? 'default' : 'outline'}
              disabled={!status || toCheck.length === 0 || probing.size > 0}
              onClick={() => setChecking(toCheck)}
            >
              <Radar aria-hidden />{t('settings.models.tools.header.action')}
            </Button>
          </span>
          {reverify && (reverify.engine_upgraded > 0 || reverify.aged > 0) && (
            <p className="model-hub-tools-reverify flex flex-wrap items-center gap-x-2" role="status">
              {reverify.engine_upgraded > 0 && <span className="model-hub-ink-gold font-semibold">{t('settings.models.tools.reverify.engineUpgraded', { count: reverify.engine_upgraded })}</span>}
              {reverify.engine_upgraded > 0 && reverify.aged > 0 && <span aria-hidden>·</span>}
              {reverify.aged > 0 && <span>{t('settings.models.tools.reverify.aged', { count: reverify.aged })}</span>}
            </p>
          )}
        </div>
      </div>
      {regionFailed(read) && (
        <div className="model-hub-usage-failure flex items-center justify-between gap-3 rounded-xl border border-border bg-background text-destructive-ink">
          <span>{t('settings.models.toast.refreshFailed')}</span>
          <button type="button" onClick={onRetry} className="model-hub-action-mint shrink-0 font-semibold">{t('settings.models.upstream.retry')}</button>
        </div>
      )}
      {status === null
        ? read.kind === 'loading' && <p className="model-hub-usage-pending text-muted">{t('common.loading')}</p>
        : <>
            {status.tools.map((tool) => (
              <ToolCard
                key={tool.name}
                tool={tool}
                sources={byId}
                hubSources={hubSources}
                pending={pending.has(tool.name)}
                probing={probing}
                lastProbes={lastProbes}
                onChange={onChange}
                onProbe={runProbe}
              />
            ))}
            <p className="model-hub-tools-foot">{t('settings.models.tools.footnote')}</p>
          </>}
      {checking && <HostedProbeDialog targets={checking} sources={byId} onRun={runProbe} onClose={() => setChecking(null)} />}
    </section>
  );
};
