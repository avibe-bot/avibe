// 工具 — the hosted tools Vibey may use (`vibey-hosted-tools.md` §8.1, design.pen
// frames pDd61 and kPTBo).
//
// One card per tool, grouped by capability, each with one switch, on by default.
// A card says only what real use learned: the latest run, and the Sources that
// refused, each of which the user can clear so it is tried again from the next
// request. Nothing here checks a Source or decides a route.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, AtSign, CheckCircle2, ChevronRight, Eraser, FileText, Globe, Image as ImageIcon, Info, LockKeyhole, Plus, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { ToggleSwitch } from '../SettingsPrimitives';
import { cooldownTime, formatMonthDay, formatNameList, formatSmallUsd } from './format';
import { executorOrderAfterMove, executorOrderAfterReorder, imageModelCopyKey, pairEvidence, refusalSummary, type PairEvidence, type RefusalSummary } from './hostedTools';
import { foldRegionRead, regionFailed, type RegionRead } from './regionRead';
import { KEYLESS_EXA_SOURCE_ID, candidateEnabled, searchServicesActive, serviceVendorForSource, webSearchTool } from './searchServices';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { OrderStepper } from './OrderStepper';
import { OrderOrdinal, SortableOrderList } from './SortableOrderList';
import { OrderPrice, SearchCandidateStatus, SearchServiceOrderPrice, SearchVendorGlyph } from './SearchServiceParts';
import { WebFetchOrder } from './WebFetchOrder';
import { fetchVendorForSource, webFetchTool } from './webFetch';
import { ACCENT_ICON, ACCENT_PILL, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type {
  HostedExecutorCandidate,
  HostedImageModel,
  HostedImagePrice,
  HostedImageToolStatus,
  HostedModelRef,
  HostedSearchToolStatus,
  HostedWebSearchToolStatus,
  HostedToolClear,
  HostedToolName,
  HostedToolPut,
  HostedToolsStatus,
  HostedToolStatus,
  Source,
} from './types';

type Save = (tool: HostedToolName, body: HostedToolPut) => void;
type Clear = (tool: HostedToolName, body: HostedToolClear) => void;

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  web_fetch: FileText,
  x_search: AtSign,
  image_generate: ImageIcon,
};

/** The tab's groups, by capability, in catalog order. */
const TOOL_GROUPS: { key: 'search' | 'generate'; tools: readonly HostedToolName[] }[] = [
  { key: 'search', tools: ['web_search', 'web_fetch', 'x_search'] },
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

/**
 * What one executor call costs, as one line in the row's price column: included on a subscription, else the
 * overlay's list price per thousand calls; unknown is never zero. The list price's date, the executor model's tokens
 * billed besides it, and any extra units are its tooltip.
 */
const ExecutorPrice: React.FC<{ candidate: HostedExecutorCandidate }> = ({ candidate }) => {
  const { t } = useTranslation();
  if (candidate.kind === 'service') return <SearchServiceOrderPrice price={candidate.price} />;
  if (candidate.billing === 'monthly') {
    return <OrderPrice text={t('settings.models.tools.cost.includedShort')} title={t('settings.models.tools.cost.includedNote')} />;
  }
  const price = candidate.price;
  if (!price) return <OrderPrice text={t('settings.models.tools.cost.unknown')} muted />;
  const extras = price.extra.map((extra) => t('settings.models.tools.cost.extra', {
    price: t('settings.models.tools.cost.perUnit', { usd: formatSmallUsd(extra.usd_per_1k / 1000), unit: t(`settings.models.tools.cost.unit.${extra.unit}`) }),
  }));
  const title = [t('settings.models.tools.cost.executorTitle', { date: price.as_of, model: candidate.model_id }), ...extras]
    .join(t('settings.models.tools.cost.extraSeparator'));
  // Per thousand of the unit billed: a search or call reads as a search, an X search post as a post.
  return <OrderPrice text={t(`settings.models.tools.cost.per1k.${price.unit}`, { usd: formatSmallUsd(price.usd_per_1k) })} title={title} href={price.url} />;
};

/** What the search order's ⓘ explains: how the automatic order is built, and where the built-in service sends a query. */
const SearchOrderHint: React.FC<{ tool: HostedWebSearchToolStatus; custom: boolean }> = ({ tool, custom }) => {
  const { t } = useTranslation();
  const keyless = tool.service_vendors?.some((vendor) => vendor.id === 'exa-mcp') === true;
  return (
    <span className="flex flex-col gap-2">
      <span>{t(keyless ? 'settings.models.searchServices.orderAutoKeyless' : 'settings.models.searchServices.orderAuto')}</span>
      {custom && <span>{t('settings.models.searchServices.orderCustom')}</span>}
      {keyless && <span>{t('settings.models.searchServices.keylessQueryDestination')}</span>}
    </span>
  );
};

/**
 * The search executors in the order the gateway tries them, for models that
 * cannot run the search natively. Each move saves the whole order; Reset saves
 * `null`, which is automatic. The order shown is always the server's.
 */
const ExecutorOrder: React.FC<{ tool: HostedSearchToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save; onOpenService?: (sourceId: string, opener: HTMLButtonElement) => void; onRetryService?: (sourceId: string) => void }> = ({ tool, lookup, pending, onSave, onOpenService, onRetryService }) => {
  const { t, i18n } = useTranslation();
  const toolInline = t(`settings.models.tools.tool.${tool.name}.inline`) as string;
  const activeWeb = tool.name === 'web_search' && searchServicesActive(tool) ? tool : null;
  const candidates = tool.executor_candidates;
  const movable = candidates.filter(candidateEnabled);
  const custom = tool.executor_order !== null;
  const candidateName = (candidate: HostedExecutorCandidate): string => {
    if (candidate.kind === 'service' && candidate.source_id === KEYLESS_EXA_SOURCE_ID) return t('settings.models.searchServices.vendorName.exa-mcp') as string;
    const vendor = candidate.kind === 'service'
      ? activeWeb?.service_vendors?.find((item) => item.source_ids.includes(candidate.source_id))
      : undefined;
    return lookup.source(candidate.source_id)?.display_name
      ?? (vendor ? t(`settings.models.searchServices.vendorName.${vendor.id}`, { defaultValue: vendor.name }) as string : lookup.name(candidate.source_id));
  };
  // A move is announced, and focus follows the moved row, only once the server's
  // order shows it. One that did not land (refused, or lost and reconciled to
  // another order) is dropped once its write has been out and settled, without
  // a word.
  const [requested, setRequested] = React.useState<{
    sourceId: string;
    direction: -1 | 1;
    expected: string;
    position: number;
    count: number;
    /** Its write has been seen out, so the tool no longer pending means it settled. */
    sent: boolean;
  } | null>(null);
  const shownOrder = candidates.map((candidate) => candidate.source_id).join('\n');
  const landed = requested !== null && shownOrder === requested.expected;
  if (requested !== null && !landed) {
    if (pending && !requested.sent) setRequested({ ...requested, sent: true });
    else if (!pending && requested.sent) setRequested(null);
  }
  const list = React.useRef<HTMLOListElement>(null);
  const buttons = React.useRef(new Map<string, HTMLButtonElement>());
  const focusedFor = React.useRef<typeof requested>(null);
  React.useEffect(() => {
    if (!landed || requested === null || focusedFor.current === requested) return;
    focusedFor.current = requested;
    // Only where focus still is the list: a user who has moved on is not pulled back.
    const active = document.activeElement;
    if (active !== null && active !== document.body && !list.current?.contains(active)) return;
    const index = movable.findIndex((candidate) => candidate.source_id === requested.sourceId);
    const atEnd = requested.direction === -1 ? index === 0 : index === movable.length - 1;
    const direction = atEnd ? -requested.direction : requested.direction;
    buttons.current.get(`${requested.sourceId}:${direction}`)?.focus();
  }, [movable, landed, requested]);
  const move = (index: number, direction: -1 | 1) => {
    if (pending) return;
    const shown = movable.map((candidate) => candidate.source_id);
    setRequested({
      sourceId: shown[index],
      direction,
      expected: [...executorOrderAfterMove(null, shown, index, direction), ...candidates.filter((candidate) => !candidateEnabled(candidate)).map((candidate) => candidate.source_id)].join('\n'),
      position: index + direction + 1,
      count: shown.length,
      sent: false,
    });
    onSave(tool.name, { executor_order: executorOrderAfterMove(tool.executor_order, shown, index, direction) });
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex items-center gap-1.5">
            <h4 className="font-semibold text-foreground">{t(activeWeb ? 'settings.models.searchServices.order' : 'settings.models.tools.order.title')}</h4>
            {activeWeb && <ModelHubInfoHint label={t('settings.models.searchServices.orderAbout') as string} content={<SearchOrderHint tool={activeWeb} custom={custom} />} />}
          </span>
          <p className="model-hub-tool-note">{activeWeb
            ? t(activeWeb.prefer_native ? 'settings.models.searchServices.orderNative' : 'settings.models.searchServices.orderAll')
            : t(custom ? 'settings.models.tools.order.captionCustom' : 'settings.models.tools.order.caption')}</p>
        </div>
        {custom && (
          <Button variant="outline" size="xs" disabled={pending} onClick={() => { setRequested(null); onSave(tool.name, { executor_order: null }); }}>
            <RotateCcw aria-hidden />{t('settings.models.tools.order.reset')}
          </Button>
        )}
      </div>
      {candidates.length === 0
        ? <p className="model-hub-tool-note flex items-start gap-2"><Info className="mt-px size-[13px] shrink-0" aria-hidden />{t('settings.models.tools.order.empty', { toolInline })}</p>
        : (
            <SortableOrderList
              rows={candidates}
              idOf={(candidate) => candidate.source_id}
              nameOf={candidateName}
              isOn={candidateEnabled}
              pending={pending}
              label={t(activeWeb ? 'settings.models.searchServices.order' : 'settings.models.tools.order.title') as string}
              listRef={list}
              rowClassName={(candidate) => cn('model-hub-tool-executor-row grid items-center rounded-lg border border-border bg-background', !candidateEnabled(candidate) && 'is-off')}
              rowAttrs={(candidate) => ({ 'data-executor': candidate.source_id })}
              onReorder={(shown, from, to) => { setRequested(null); onSave(tool.name, { executor_order: executorOrderAfterReorder(tool.executor_order, shown, from, to) }); }}
            >
              {(candidate, index, handle) => {
                const source = lookup.source(candidate.source_id);
                const vendor = candidate.kind === 'service'
                  ? activeWeb?.service_vendors?.find((item) => item.source_ids.includes(candidate.source_id))
                  : undefined;
                const keyless = candidate.kind === 'service' && candidate.source_id === KEYLESS_EXA_SOURCE_ID;
                const name = candidateName(candidate);
                const cooldown = candidate.kind === 'service' && candidate.cooldown_until !== null
                  ? cooldownTime(candidate.cooldown_until, i18n.language)
                  : null;
                const switchedOff = !candidateEnabled(candidate);
                // A web-search model executor carries its switch on a server that has one (contract v23).
                const modelSwitch = candidate.kind === 'model' ? candidate.settings : undefined;
                const stepper = (
                  <OrderStepper
                    label={name}
                    canUp={!switchedOff && index > 0}
                    canDown={!switchedOff && index < movable.length - 1}
                    pending={pending}
                    onMove={(direction) => move(index, direction)}
                    buttonRef={(direction, node) => {
                      const key = `${candidate.source_id}:${direction}`;
                      if (node) buttons.current.set(key, node);
                      else buttons.current.delete(key);
                    }}
                  />
                );
                return (
                  <>
                    <span className="flex items-center gap-1.5">{stepper}<OrderOrdinal index={index} off={switchedOff} handle={handle} name={name} /></span>
                    {activeWeb ? <div className="flex min-w-0 items-center gap-3">
                      {source ? <SearchVendorGlyph vendor={source.vendor} /> : candidate.kind === 'service' ? <SearchVendorGlyph vendor={vendor?.id ?? candidate.model_id} /> : <SourceGlyph source={source} />}
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                          {candidate.kind === 'service' ? <button type="button" data-search-source={candidate.source_id} className="truncate text-left font-semibold text-foreground hover:underline" onClick={(event) => onOpenService?.(candidate.source_id, event.currentTarget)}>{name}</button> : <span className="truncate font-semibold text-foreground">{name}</span>}
                          <span className="model-hub-order-kind">{t(candidate.kind === 'service' ? 'settings.models.searchServices.kind' : 'settings.models.searchServices.modelKind')}</span>
                          <SearchCandidateStatus candidate={candidate} tool={activeWeb} source={source} inRow />
                        </div>
                        <span className="model-hub-tool-note truncate">{candidate.kind === 'service'
                          ? <>{cooldown && keyless && <>{t('settings.models.searchServices.keylessCooldownUntil', { time: cooldown })} · </>}{t(keyless ? 'settings.models.searchServices.keylessResults' : source?.protocol === 'search' ? 'settings.models.searchServices.resultCount' : 'settings.models.searchServices.sharedKeyResults', { count: candidate.settings.results, source: name })}</>
                          : <span className="font-mono">{candidate.model_id}</span>}</span>
                        {candidate.kind === 'service' && candidate.cooldown_until !== null && <Button variant="outline" size="xs" className="self-start" disabled={pending} onClick={() => onRetryService?.(candidate.source_id)}>{t('settings.models.searchServices.retry')}</Button>}
                      </div>
                    </div> : <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <SourceGlyph source={source} />
                      <span className="truncate font-semibold text-foreground" title={name}>{name}</span>
                      <span className="truncate font-mono text-muted" title={candidate.model_id}>{candidate.model_id}</span>
                      {source && <span className={cn('model-hub-pill border', ACCENT_PILL[source.kind === 'api_key' ? 'cyan' : sourceVisual(source).accent])}>{t(`settings.models.upstream.kind.${source.kind === 'subscription' ? 'subscription' : 'apiKey'}`)}</span>}
                    </span>}
                    <ExecutorPrice candidate={candidate} />
                    <span className="model-hub-order-switch">{(candidate.kind === 'service' || modelSwitch) && activeWeb && (
                      <ToggleSwitch
                        enabled={!switchedOff}
                        disabled={pending}
                        label={t('settings.models.searchServices.use') as string}
                        onClick={() => onSave('web_search', candidate.kind === 'service'
                          ? { services: { [candidate.source_id]: { enabled: switchedOff } } }
                          : { executors: { [candidate.source_id]: { enabled: switchedOff } } })}
                      />
                    )}</span>
                  </>
                );
              }}
            </SortableOrderList>
          )}
      {/* The arrows' announcement; a drag is announced by the drag context's own live region. */}
      <span role="status" data-order-moved className="sr-only">{landed && t('settings.models.tools.order.moved', { source: lookup.name(requested.sourceId), position: requested.position, count: requested.count })}</span>
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
  const services = tool.name === 'web_fetch' || (tool.name === 'web_search' && searchServicesActive(tool));
  const search = tool.name === 'image_generate' || tool.name === 'web_fetch' || services ? null : tool;
  const statusList = services || summary.sources >= 2;
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

/** One image's price: the vendor's list price, an estimate from token pricing (≈), or none known. */
/**
 * One image's price: included on a subscription Source, else the vendor's list
 * price, an estimate from token pricing (≈), or none known; unknown is never zero.
 */
const ImagePrice: React.FC<{ price: HostedImagePrice | null; billing: HostedImageModel['billing'] }> = ({ price, billing }) => {
  const { t } = useTranslation();
  if (billing === 'monthly') {
    return <span className="flex flex-col"><span className="font-semibold text-foreground">{t('settings.models.tools.cost.included')}</span><span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.includedNote')}</span></span>;
  }
  if (!price) return <span className="model-hub-tool-cost-note">{t('settings.models.tools.image.noUnitPrice')}</span>;
  const usd = formatSmallUsd(price.usd);
  return (
    <span className="flex flex-col">
      <span className="font-semibold text-foreground">{t(price.basis === 'list' ? 'settings.models.tools.image.perImage' : 'settings.models.tools.image.perImageEstimated', { usd })}</span>
      <span className="model-hub-tool-cost-note">{t('settings.models.tools.cost.listNote')}</span>
    </span>
  );
};

const pairValue = (pair: HostedModelRef) => JSON.stringify([pair.source_id, pair.model_id]);
const samePair = (left: HostedModelRef | null, right: HostedModelRef | null) =>
  left !== null && right !== null && left.source_id === right.source_id && left.model_id === right.model_id;
const AUTOMATIC = '';

/**
 * The image models `image_generate` offers now, with the default Vibey names when
 * it does not pick one. The saved default is shown as saved, even when it is no
 * longer offered, and can always be reset; the default in use is the server's
 * `effective_default`. Automatic is named only while it is what is in use: with a
 * model saved, the read does not say what automatic would pick, so the option
 * says how it picks instead.
 */
const ImageModels: React.FC<{ tool: HostedImageToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save }> = ({ tool, lookup, pending, onSave }) => {
  const { t, i18n } = useTranslation();
  const pairLabel = (pair: HostedModelRef) => t('settings.models.tools.image.pair', { model: pair.model_id, source: lookup.name(pair.source_id) }) as string;
  const saved = tool.default_model;
  const inUse = tool.effective_default;
  const vendors = tool.image_offered_by.map((vendor) => t(`settings.models.tools.image.vendor.${vendor}`, { defaultValue: vendor }) as string);
  // With a current refusal, the list is empty because of it, and the refusal lines with their clears say so; a key
  // is the remedy only when nothing was refused.
  const refused = tool.status.refused.length > 0;
  return (
    <div className="model-hub-tool-image flex flex-col gap-3 border-t border-border">
      {tool.models.length === 0
        ? (
            <p className="flex items-start gap-2 text-muted">
              <Info className="mt-0.5 size-[14px] shrink-0" aria-hidden />
              <span>{refused
                ? t('settings.models.tools.image.emptyRefused')
                : vendors.length === 0
                  ? t('settings.models.tools.image.emptyNoVendor')
                  : vendors.length === 1
                    ? t('settings.models.tools.image.emptyOne', { vendor: vendors[0] })
                    : t('settings.models.tools.image.emptyMany', { vendors: formatNameList(vendors, i18n.language) })}</span>
            </p>
          )
        : (
            <div className="flex flex-col gap-1.5">
              <h4 className="font-semibold text-foreground">{t('settings.models.tools.image.title')}</h4>
              <ul className="flex flex-col gap-1.5">
                {tool.models.map((model) => {
                  const source = lookup.source(model.source_id);
                  const name = lookup.name(model.source_id);
                  return (
                    <li key={pairValue(model)} className="model-hub-tool-image-row grid items-center rounded-lg border border-border bg-background" data-image-model={`${model.source_id}:${model.model_id}`}>
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="truncate font-mono font-semibold text-foreground" title={model.model_id}>{model.model_id}</span>
                          {samePair(model, inUse) && <span className="model-hub-pill model-hub-status-mint border">{t('settings.models.tools.image.defaultBadge')}</span>}
                        </span>
                        {/* A shipped model has its own copy in each locale; one without (a user's own) shows the overlay's line. */}
                        <span className="model-hub-tool-note">{t(`settings.models.tools.image.description.${imageModelCopyKey(model.model_id)}`, { defaultValue: model.description })}</span>
                      </span>
                      <span className="flex min-w-0 items-center gap-2">
                        <SourceGlyph source={source} />
                        <span className="truncate font-semibold text-foreground" title={name}>{name}</span>
                        {source && <span className={cn('model-hub-pill border', ACCENT_PILL[source.kind === 'api_key' ? 'cyan' : sourceVisual(source).accent])}>{t(`settings.models.upstream.kind.${source.kind === 'subscription' ? 'subscription' : 'apiKey'}`)}</span>}
                      </span>
                      <ImagePrice price={model.price} billing={model.billing} />
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
      {(tool.models.length > 0 || saved !== null) && (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
          <span className="shrink-0 font-semibold text-foreground">{t('settings.models.tools.image.defaultLabel')}</span>
          <Select
            wrapperClassName="sm:w-[300px] shrink-0"
            className="h-8 text-[12px]"
            aria-label={t('settings.models.tools.image.defaultPick') as string}
            disabled={pending}
            value={saved ? pairValue(saved) : AUTOMATIC}
            onChange={(event) => {
              const picked = tool.models.find((model) => pairValue(model) === event.target.value);
              onSave(tool.name, { default_model: picked ? { source_id: picked.source_id, model_id: picked.model_id } : null });
            }}
          >
            <option value={AUTOMATIC}>{saved
              ? t('settings.models.tools.image.automaticRule')
              : inUse
                ? t('settings.models.tools.image.automaticNamed', { pair: pairLabel(inUse) })
                : t('settings.models.tools.image.automatic')}</option>
            {saved && !saved.available && <option value={pairValue(saved)}>{t('settings.models.tools.image.unavailable', { pair: pairLabel(saved) })}</option>}
            {tool.models.map((model) => <option key={pairValue(model)} value={pairValue(model)}>{pairLabel(model)}</option>)}
          </Select>
          <span className="model-hub-tool-note">
            {saved && !saved.available
              ? inUse
                ? t('settings.models.tools.image.unavailableNote', { pair: pairLabel(inUse) })
                : t('settings.models.tools.image.unavailableNoteNone')
              : t('settings.models.tools.image.defaultNote')}
          </span>
        </div>
      )}
    </div>
  );
};

type ServiceActions = {
  onAddService?: (opener: HTMLButtonElement) => void;
  onOpenService?: (sourceId: string, opener: HTMLButtonElement) => void;
  onRetryService?: (sourceId: string) => void;
  onRetryFetch?: (sourceId: string) => void;
};

const WebSearchServices: React.FC<{ tool: HostedWebSearchToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save } & ServiceActions> = ({ tool, lookup, pending, onSave, onAddService, onOpenService, onRetryService }) => {
  const { t } = useTranslation();
  const noServices = !tool.service_vendors?.some((vendor) => !vendor.facts.built_in && vendor.source_ids.length > 0);
  const hasKeyless = tool.service_vendors?.some((vendor) => vendor.id === 'exa-mcp') === true;
  return <div className="flex flex-col gap-4 border-t border-border p-4">
    <div className="flex items-center justify-between gap-4 rounded-lg border border-border bg-background p-3">
      <div className="flex min-w-0 flex-col gap-1"><h4 className="font-semibold text-foreground">{t('settings.models.searchServices.preferNative')}</h4><p className="model-hub-tool-note">{t(tool.prefer_native ? 'settings.models.searchServices.nativeOn' : 'settings.models.searchServices.nativeOff')}</p></div>
      <ToggleSwitch enabled={tool.prefer_native} disabled={pending} label={t('settings.models.searchServices.preferNative')} onClick={() => onSave('web_search', { prefer_native: !tool.prefer_native })} />
    </div>
    <ExecutorOrder tool={tool} lookup={lookup} pending={pending} onSave={onSave} onOpenService={onOpenService} onRetryService={onRetryService} />
    <p className="model-hub-tool-note">{t('settings.models.searchServices.priceFootnote')}</p>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Button variant={noServices ? 'default' : 'outline'} size="sm" disabled={pending} onClick={(event) => onAddService?.(event.currentTarget)}><Plus aria-hidden />{t('settings.models.searchServices.add')}</Button>
      <p className="model-hub-tool-note flex items-center gap-1.5" title={t(hasKeyless ? 'settings.models.searchServices.keylessCustody' : 'settings.models.searchServices.custody') as string}><LockKeyhole className="size-3.5 shrink-0" aria-hidden />{t('settings.models.searchServices.custodyShort')}</p>
    </div>
  </div>;
};

const ToolCard: React.FC<{ tool: HostedToolStatus; lookup: SourceLookup; pending: boolean; onSave: Save; onClear: Clear } & ServiceActions> = ({ tool, lookup, pending, onSave, onClear, ...serviceActions }) => {
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
      {tool.name === 'web_search' && searchServicesActive(tool) && <WebSearchServices tool={tool} lookup={lookup} pending={pending} onSave={onSave} {...serviceActions} />}
      {tool.name === 'web_fetch' && <WebFetchOrder tool={tool} sources={tool.executor_candidates.flatMap((row) => { const source = lookup.source(row.source_id); return source ? [source] : []; })} pending={pending} onSave={(body) => onSave('web_fetch', body)} onAdd={serviceActions.onAddService} onOpen={serviceActions.onOpenService} onRetry={serviceActions.onRetryFetch} />}
      {tool.name === 'image_generate' && <ImageModels tool={tool} lookup={lookup} pending={pending} onSave={onSave} />}
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
} & ServiceActions> = ({ read, sources, pending, onSave, onClear, onRetry, ...serviceActions }) => {
  const { t } = useTranslation();
  const status = foldRegionRead<HostedToolsStatus, HostedToolsStatus | null>(read, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: (staleData) => staleData,
  });
  const lookup = React.useMemo<SourceLookup>(() => {
    const byId = new Map(sources.map((source) => [source.id, source]));
    return {
      name: (id) => {
        const source = byId.get(id);
        if (source) return source.display_name;
        // Built-ins have catalog membership, not a persisted Source. Resolve
        // them here so status, fallback, evidence and action labels agree.
        const fetchVendor = fetchVendorForSource(webFetchTool(status), id);
        if (fetchVendor?.facts.built_in) return t(`settings.models.webFetch.vendorName.${fetchVendor.id}`, { defaultValue: fetchVendor.name }) as string;
        const vendor = serviceVendorForSource(webSearchTool(status), id);
        return vendor?.facts.built_in
          ? t(`settings.models.searchServices.vendorName.${vendor.id}`, { defaultValue: vendor.name }) as string
          : id;
      },
      source: (id) => byId.get(id),
    };
  }, [sources, status, t]);
  return (
    <section className="model-hub-tools flex flex-col" aria-label={t('settings.models.tools.title') as string}>
      <div className="flex min-w-0 flex-col gap-1">
        <h2 className="model-hub-tools-title font-bold text-foreground">{t('settings.models.tools.title')}</h2>
        <p className="model-hub-tools-subtitle">{t(searchServicesActive(webSearchTool(status)) ? 'settings.models.searchServices.subtitle' : 'settings.models.tools.subtitle')}</p>
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
                    <ToolCard key={tool.name} tool={tool} lookup={lookup} pending={pending.has(tool.name)} onSave={onSave} onClear={onClear} {...serviceActions} />
                  ))}
                </section>
              );
            })}
            <p className="model-hub-tools-foot">{t('settings.models.tools.footnote')}</p>
          </>}
    </section>
  );
};
