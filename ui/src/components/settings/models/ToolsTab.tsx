// 工具 — the hosted tools Vibey may use (`vibey-hosted-tools.md` §8.1, design.pen
// frames pDd61 and kPTBo).
//
// One card per tool, grouped by capability, each with one switch, on by default.
// A card says only what real use learned: the latest run, and the Sources that
// refused, each of which the user can clear so it is tried again from the next
// request. Nothing here checks a Source or decides a route.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ArrowDown, ArrowUp, AtSign, CheckCircle2, ChevronRight, Eraser, Globe, Image as ImageIcon, Info, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ToggleSwitch } from '../SettingsPrimitives';
import { formatMonthDay, formatNameList, formatSmallUsd } from './format';
import { executorOrderAfterMove, pairEvidence, refusalSummary, type PairEvidence, type RefusalSummary } from './hostedTools';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import { ACCENT_ICON, ACCENT_PILL, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type {
  HostedExecutorCandidate,
  HostedSearchToolStatus,
  HostedToolClear,
  HostedToolName,
  HostedToolPriceUnit,
  HostedToolPut,
  HostedToolsStatus,
  HostedToolStatus,
  Source,
} from './types';

type Save = (tool: HostedToolName, body: HostedToolPut) => void;
type Clear = (tool: HostedToolName, body: HostedToolClear) => void;

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generate: ImageIcon,
};

/** The tab's groups, by capability, in catalog order. */
const TOOL_GROUPS: { key: 'search' | 'generate'; tools: readonly HostedToolName[] }[] = [
  { key: 'search', tools: ['web_search', 'x_search'] },
  { key: 'generate', tools: ['image_generate'] },
];

type SourceLookup = { name: (id: string) => string; source: (id: string) => Source | undefined };

const SourceGlyph: React.FC<{ source: Source | undefined }> = ({ source }) => {
  const { Icon, accent } = source ? sourceVisual(source) : { Icon: Globe, accent: 'muted' as const };
  return (
    <span className={cn('flex size-[22px] shrink-0 items-center justify-center rounded-[6px]', ACCENT_TILE[accent])}>
      <Icon className={cn('size-3', ACCENT_ICON[accent])} />
    </span>
  );
};

/** One status line: what real use showed, with a clear when this line owns one. */
const StatusLine: React.FC<{ tone: 'ok' | 'refused'; text: string; clear?: { label: string; onClick: () => void; disabled: boolean } }> = ({ tone, text, clear }) => {
  const { t } = useTranslation();
  const Icon = tone === 'ok' ? CheckCircle2 : AlertCircle;
  return (
    <p className="model-hub-tool-status flex items-start gap-2 border-t border-border">
      <Icon className={cn('mt-0.5 size-[14px] shrink-0', tone === 'ok' ? 'model-hub-ink-mint' : 'model-hub-ink-gold')} aria-hidden />
      <span className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
        <span className="min-w-0 text-foreground">{text}</span>
        {clear && (
          <button type="button" className="model-hub-accent-ink--cyan shrink-0 font-semibold disabled:opacity-50" aria-label={clear.label} disabled={clear.disabled} onClick={clear.onClick}>
            {t('settings.models.tools.clear.label')}
          </button>
        )}
      </span>
    </p>
  );
};

/**
 * The card's status lines, only after something happened. With one Source
 * refused, its line names the models and carries the clear; with two or more,
 * the lines name the Sources and the clears move into Advanced. A call line that
 * says where the tool now runs stands in for the latest-run line.
 */
const StatusLines: React.FC<{ tool: HostedToolStatus; summary: RefusalSummary; lookup: SourceLookup; pending: boolean; onClear: Clear }> = ({ tool, summary, lookup, pending, onClear }) => {
  const { t, i18n } = useTranslation();
  const latest = tool.status.latest_ok;
  const inline = summary.sources === 1;
  const toolInline = t(`settings.models.tools.tool.${tool.name}.inline`) as string;
  const names = (groups: RefusalSummary['call']) => formatNameList(groups.map((group) => lookup.name(group.sourceId)), i18n.language);
  const clearFor = (sourceId: string) => ({
    label: t('settings.models.tools.clear.source', { source: lookup.name(sourceId), toolInline }) as string,
    onClick: () => onClear(tool.name, { source_id: sourceId }),
    disabled: pending,
  });
  const call = summary.call;
  const callLine = call.length === 0
    ? null
    : inline
      ? t('settings.models.tools.status.refused', { source: lookup.name(call[0].sourceId), models: formatNameList(call[0].models, i18n.language) }) as string
      : t('settings.models.tools.status.refusedMany', { sources: names(call) }) as string;
  const replay = summary.replay;
  return (
    <>
      {latest && summary.switchedTo === null && (
        <StatusLine tone="ok" text={t('settings.models.tools.status.ok', { source: lookup.name(latest.source_id), date: formatMonthDay(latest.at) }) as string} />
      )}
      {callLine !== null && (
        <StatusLine
          tone="refused"
          text={summary.switchedTo === null
            ? callLine
            : t('settings.models.tools.status.switched', { line: callLine, source: lookup.name(summary.switchedTo) }) as string}
          clear={inline ? clearFor(call[0].sourceId) : undefined}
        />
      )}
      {replay.length > 0 && (
        <StatusLine
          tone="refused"
          text={inline
            ? t('settings.models.tools.status.replay', { source: lookup.name(replay[0].sourceId) }) as string
            : t('settings.models.tools.status.replayMany', { sources: names(replay) }) as string}
          clear={inline ? clearFor(replay[0].sourceId) : undefined}
        />
      )}
    </>
  );
};

/** What one executor call costs: included on a subscription, else the overlay's list price; unknown is never zero. */
const ExecutorPrice: React.FC<{ candidate: HostedExecutorCandidate }> = ({ candidate }) => {
  const { t } = useTranslation();
  if (candidate.billing === 'monthly') {
    return <span className="flex flex-col"><span className="font-semibold text-foreground">{t('settings.models.tools.cost.included')}</span><span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.includedNote')}</span></span>;
  }
  const price = candidate.price;
  if (!price) return <span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.unknown')}</span>;
  const perUnit = (usdPer1k: number, unit: HostedToolPriceUnit) => t('settings.models.tools.cost.perUnit', {
    usd: formatSmallUsd(usdPer1k / 1000),
    unit: t(`settings.models.tools.cost.unit.${unit}`),
  }) as string;
  return (
    <span className="flex flex-col">
      <span className="font-semibold text-foreground">{perUnit(price.usd_per_1k, price.unit)}</span>
      <span className="model-hub-tool-cost-note">
        {price.extra.length === 0
          ? t('settings.models.tools.cost.listNote')
          : t('settings.models.tools.cost.listNoteExtras', {
              extras: price.extra
                .map((extra) => t('settings.models.tools.cost.extra', { price: perUnit(extra.usd_per_1k, extra.unit) }))
                .join(t('settings.models.tools.cost.extraSeparator')),
            })}
      </span>
    </span>
  );
};

/**
 * The search executors in the order the gateway tries them, for models that
 * cannot run the search natively. Each move saves the whole order; Reset saves
 * `null`, which is automatic. The order shown is always the server's.
 */
const ExecutorOrder: React.FC<{ tool: HostedSearchToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save }> = ({ tool, lookup, pending, onSave }) => {
  const { t } = useTranslation();
  const toolInline = t(`settings.models.tools.tool.${tool.name}.inline`) as string;
  const candidates = tool.executor_candidates;
  const custom = tool.executor_order !== null;
  const [moved, setMoved] = React.useState<{ sourceId: string; position: number; count: number } | null>(null);
  // A move re-renders the list from the answer; focus follows the moved row,
  // to the other arrow once the one pressed is at its end.
  const buttons = React.useRef(new Map<string, HTMLButtonElement>());
  const refocus = React.useRef<{ sourceId: string; direction: -1 | 1 } | null>(null);
  React.useEffect(() => {
    const target = refocus.current;
    if (!target) return;
    const index = candidates.findIndex((candidate) => candidate.source_id === target.sourceId);
    if (index < 0) return;
    refocus.current = null;
    const atEnd = target.direction === -1 ? index === 0 : index === candidates.length - 1;
    const direction = atEnd ? -target.direction : target.direction;
    buttons.current.get(`${target.sourceId}:${direction}`)?.focus();
  }, [candidates]);
  const move = (index: number, direction: -1 | 1) => {
    if (pending) return;
    const shown = candidates.map((candidate) => candidate.source_id);
    const sourceId = shown[index];
    refocus.current = { sourceId, direction };
    setMoved({ sourceId, position: index + direction + 1, count: shown.length });
    onSave(tool.name, { executor_order: executorOrderAfterMove(tool.executor_order, shown, index, direction) });
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h4 className="font-semibold text-foreground">{t('settings.models.tools.order.title')}</h4>
          <p className="model-hub-tool-note">{t(custom ? 'settings.models.tools.order.captionCustom' : 'settings.models.tools.order.caption')}</p>
        </div>
        {custom && (
          <Button variant="outline" size="xs" disabled={pending} onClick={() => onSave(tool.name, { executor_order: null })}>
            <RotateCcw aria-hidden />{t('settings.models.tools.order.reset')}
          </Button>
        )}
      </div>
      {candidates.length === 0
        ? <p className="model-hub-tool-note flex items-start gap-2"><Info className="mt-px size-[13px] shrink-0" aria-hidden />{t('settings.models.tools.order.empty', { toolInline })}</p>
        : (
            <ol className="flex flex-col gap-1.5" aria-label={t('settings.models.tools.order.title') as string}>
              {candidates.map((candidate, index) => {
                const source = lookup.source(candidate.source_id);
                const name = lookup.name(candidate.source_id);
                const arrow = (direction: -1 | 1) => {
                  const blocked = direction === -1 ? index === 0 : index === candidates.length - 1;
                  return (
                    <Button
                      ref={(node) => {
                        const key = `${candidate.source_id}:${direction}`;
                        if (node) buttons.current.set(key, node);
                        else buttons.current.delete(key);
                      }}
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="model-hub-route-action model-hub-order-row-action"
                      aria-label={t(direction === -1 ? 'settings.models.tools.order.moveUp' : 'settings.models.tools.order.moveDown', { source: name }) as string}
                      // While a write is out the arrows stay focusable and do nothing:
                      // the next move must be built on the order that write answers.
                      aria-disabled={pending || undefined}
                      disabled={blocked}
                      onClick={() => move(index, direction)}
                    >
                      {direction === -1 ? <ArrowUp aria-hidden /> : <ArrowDown aria-hidden />}
                    </Button>
                  );
                };
                return (
                  <li key={candidate.source_id} className="model-hub-tool-executor-row grid items-center rounded-lg border border-border bg-background" data-executor={candidate.source_id}>
                    <span className={cn('model-hub-order-ordinal', index === 0 && 'is-first')}>{index + 1}</span>
                    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <SourceGlyph source={source} />
                      <span className="truncate font-semibold text-foreground" title={name}>{name}</span>
                      <span className="truncate font-mono text-muted" title={candidate.model_id}>{candidate.model_id}</span>
                      {source && <span className={cn('model-hub-pill border', ACCENT_PILL[source.kind === 'api_key' ? 'cyan' : sourceVisual(source).accent])}>{t(`settings.models.upstream.kind.${source.kind === 'subscription' ? 'subscription' : 'apiKey'}`)}</span>}
                    </span>
                    <ExecutorPrice candidate={candidate} />
                    <span className="flex items-center gap-1">{arrow(-1)}{arrow(1)}</span>
                  </li>
                );
              })}
            </ol>
          )}
      <span role="status" className="sr-only">{moved && t('settings.models.tools.order.moved', { source: lookup.name(moved.sourceId), position: moved.position, count: moved.count })}</span>
    </div>
  );
};

/** One (Source, model)'s record on the tool, as the Advanced status list states it. */
const PairRow: React.FC<{ tool: HostedToolName; pair: PairEvidence; lookup: SourceLookup; pending: boolean; onClear: Clear }> = ({ tool, pair, lookup, pending, onClear }) => {
  const { t } = useTranslation();
  const name = lookup.name(pair.sourceId);
  const refusal = pair.kind === 'refused' ? pair.refusal : null;
  const date = formatMonthDay(pair.kind === 'refused' ? pair.refusal.at : pair.success.at);
  return (
    <li className="model-hub-tool-pair-row grid items-center rounded-lg border border-border bg-background" data-pair={`${pair.sourceId}:${pair.modelId}`}>
      <span className="flex min-w-0 items-center gap-2">
        <SourceGlyph source={lookup.source(pair.sourceId)} />
        <span className="truncate font-semibold text-foreground" title={name}>{name}</span>
        <span className="truncate font-mono text-muted" title={pair.modelId}>{pair.modelId}</span>
      </span>
      {refusal
        ? (
            <span className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
              <span className="model-hub-pill model-hub-status-gold gap-1 border"><AlertCircle className="size-3" aria-hidden />{t(refusal.aspect === 'call' ? 'settings.models.tools.pair.refused' : 'settings.models.tools.pair.replay', { date })}</span>
              {refusal.reason && <span className="truncate font-mono text-muted" title={refusal.reason}>{refusal.reason}</span>}
            </span>
          )
        : <span className="model-hub-pill model-hub-status-mint gap-1 border"><CheckCircle2 className="size-3" aria-hidden />{t('settings.models.tools.pair.ok', { date })}</span>}
      {refusal
        ? (
            <button
              type="button"
              className="model-hub-accent-ink--cyan justify-self-end font-semibold disabled:opacity-50"
              aria-label={t('settings.models.tools.clear.model', { source: name, model: pair.modelId, toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string}
              disabled={pending}
              onClick={() => onClear(tool, { source_id: pair.sourceId, model_id: pair.modelId })}
            >
              {t('settings.models.tools.clear.label')}
            </button>
          )
        : <span />}
    </li>
  );
};

/** Every (Source, model) with a record, with the clears, once two or more Sources refused. */
const SourceStatusList: React.FC<{ tool: HostedToolStatus; lookup: SourceLookup; pending: boolean; onClear: Clear }> = ({ tool, lookup, pending, onClear }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h4 className="font-semibold text-foreground">{t('settings.models.tools.sources.title')}</h4>
          <p className="model-hub-tool-note">{t('settings.models.tools.sources.caption')}</p>
        </div>
        <Button
          variant="outline"
          size="xs"
          disabled={pending}
          aria-label={t('settings.models.tools.clear.allLabel', { toolInline: t(`settings.models.tools.tool.${tool.name}.inline`) }) as string}
          onClick={() => onClear(tool.name, {})}
        >
          <Eraser aria-hidden />{t('settings.models.tools.clear.all')}
        </Button>
      </div>
      <ul className="flex flex-col gap-1.5">
        {pairEvidence(tool.status).map((pair) => (
          <PairRow key={`${pair.sourceId}:${pair.modelId}`} tool={tool.name} pair={pair} lookup={lookup} pending={pending} onClear={onClear} />
        ))}
      </ul>
    </div>
  );
};

/**
 * The collapsed Advanced section: a search tool's executor order, and every
 * tool's clears once two or more Sources refused it. A card with neither has
 * no Advanced section.
 */
const Advanced: React.FC<{ tool: HostedToolStatus; summary: RefusalSummary; lookup: SourceLookup; pending: boolean; onSave: Save; onClear: Clear }> = ({ tool, summary, lookup, pending, onSave, onClear }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const bodyId = React.useId();
  const search = tool.name === 'image_generate' ? null : tool;
  const statusList = summary.sources >= 2;
  if (!search && !statusList) return null;
  const order = search
    ? t(search.executor_order === null ? 'settings.models.tools.order.summaryAuto' : 'settings.models.tools.order.summaryCustom') as string
    : null;
  const label = order === null
    ? t('settings.models.tools.sources.title') as string
    : statusList
      ? t('settings.models.tools.advanced.summaryBoth', { order, sources: t('settings.models.tools.sources.title') }) as string
      : order;
  return (
    <div className="model-hub-tool-advanced flex flex-col border-t border-border">
      <button type="button" className="model-hub-tool-advanced-toggle flex items-center gap-2 text-left" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((previous) => !previous)}>
        <ChevronRight className={cn('size-[14px] shrink-0 text-muted transition-transform', open && 'rotate-90')} aria-hidden />
        <span className="font-semibold text-foreground">{t('settings.models.tools.advanced.label')}</span>
        {!open && <span className="truncate text-muted">{label}</span>}
      </button>
      {open && (
        <div id={bodyId} className="model-hub-tool-advanced-body flex flex-col gap-4">
          {search && <ExecutorOrder tool={search} lookup={lookup} pending={pending} onSave={onSave} />}
          {statusList && <SourceStatusList tool={tool} lookup={lookup} pending={pending} onClear={onClear} />}
        </div>
      )}
    </div>
  );
};

const ToolCard: React.FC<{ tool: HostedToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save; onClear: Clear }> = ({ tool, lookup, pending, onSave, onClear }) => {
  const { t } = useTranslation();
  const Icon = TOOL_ICON[tool.name];
  const name = t(`settings.models.tools.tool.${tool.name}.name`) as string;
  const summary = refusalSummary(tool.status);
  return (
    <section className="model-hub-tool-card overflow-hidden rounded-xl border border-border bg-surface" aria-label={name} data-tool={tool.name}>
      <header className="model-hub-tool-card-head flex items-center gap-3">
        <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE.cyan)}><Icon className={cn('size-[17px]', ACCENT_ICON.cyan)} /></span>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <h3 className="model-hub-tool-name font-bold text-foreground">{name}</h3>
            <code className="model-hub-pill model-hub-fill-0a border border-border font-mono font-normal text-muted">{tool.name}</code>
          </span>
          <span className="model-hub-tool-description">{t(`settings.models.tools.tool.${tool.name}.description`)}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2.5">
          <span className={cn('model-hub-tool-state font-semibold', tool.enabled ? 'model-hub-ink-mint' : 'text-muted')}>{t(tool.enabled ? 'settings.models.tools.state.on' : 'settings.models.tools.state.off')}</span>
          <ToggleSwitch
            enabled={tool.enabled}
            disabled={pending}
            label={t(tool.enabled ? 'settings.models.tools.toggle.turnOff' : 'settings.models.tools.toggle.turnOn', { name }) as string}
            onClick={() => onSave(tool.name, { enabled: !tool.enabled })}
          />
        </span>
      </header>
      <StatusLines tool={tool} summary={summary} lookup={lookup} pending={pending} onClear={onClear} />
      <Advanced tool={tool} summary={summary} lookup={lookup} pending={pending} onSave={onSave} onClear={onClear} />
    </section>
  );
};

export const ToolsTab: React.FC<{
  read: RegionRead<HostedToolsStatus>;
  /** The Sources read, for names and glyphs; the status carries only ids. */
  sources: readonly Source[];
  pending: ReadonlySet<HostedToolName>;
  onSave: Save;
  onClear: Clear;
  onRetry: () => void;
}> = ({ read, sources, pending, onSave, onClear, onRetry }) => {
  const { t } = useTranslation();
  const status = foldRegionRead<HostedToolsStatus, HostedToolsStatus | null>(read, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: (staleData) => staleData,
  });
  const lookup = React.useMemo<SourceLookup>(() => {
    const byId = new Map(sources.map((source) => [source.id, source]));
    return { name: (id) => byId.get(id)?.display_name ?? id, source: (id) => byId.get(id) };
  }, [sources]);
  return (
    <section className="model-hub-tools flex flex-col" aria-label={t('settings.models.tools.title') as string}>
      <div className="flex min-w-0 flex-col gap-1">
        <h2 className="model-hub-tools-title font-bold text-foreground">{t('settings.models.tools.title')}</h2>
        <p className="model-hub-tools-subtitle">{t('settings.models.tools.subtitle')}</p>
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
            {TOOL_GROUPS.map((group) => {
              const tools = group.tools.flatMap((name) => status.tools.filter((tool) => tool.name === name));
              if (tools.length === 0) return null;
              return (
                <section key={group.key} className="model-hub-tool-group flex flex-col" aria-label={t(`settings.models.tools.group.${group.key}`) as string}>
                  <h3 className="model-hub-tool-group-title font-semibold">{t(`settings.models.tools.group.${group.key}`)}</h3>
                  {tools.map((tool) => (
                    <ToolCard key={tool.name} tool={tool} lookup={lookup} pending={pending.has(tool.name)} onSave={onSave} onClear={onClear} />
                  ))}
                </section>
              );
            })}
            <p className="model-hub-tools-foot">{t('settings.models.tools.footnote')}</p>
          </>}
    </section>
  );
};
