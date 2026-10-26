import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowDown, ArrowUp, LockKeyhole, Plus, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ToggleSwitch } from '../SettingsPrimitives';
import { SearchVendorGlyph } from './SearchServiceParts';
import { executorOrderAfterMove } from './hostedTools';
import { cooldownTime, formatMonthDay } from './format';
import { sourceStatePresentation } from './sourceStatePresentation';
import { fetchVendorForSource } from './webFetch';
import type { HostedFetchExecutorCandidate, HostedToolPut, HostedWebFetchToolStatus, Source } from './types';

const FetchPrice: React.FC<{ candidate: HostedFetchExecutorCandidate }> = ({ candidate }) => {
  const { t } = useTranslation();
  return <span className="flex flex-col gap-0.5">
    <span className="font-semibold text-foreground">{candidate.price.usd_per_1k === 0 ? t('settings.models.searchServices.free') : t('settings.models.webFetch.price', { usd: candidate.price.usd_per_1k })}</span>
    <span className="model-hub-tool-cost-note">{t(candidate.kind === 'local' ? 'settings.models.webFetch.localPrice' : candidate.price.usd_per_1k === 0 ? 'settings.models.webFetch.builtInPrice' : candidate.model_id === 'tavily/extract' ? 'settings.models.webFetch.tavilyPrice' : 'settings.models.tools.cost.listNote')}</span>
  </span>;
};

const FetchStatus: React.FC<{ candidate: HostedFetchExecutorCandidate; tool: HostedWebFetchToolStatus }> = ({ candidate, tool }) => {
  const { t, i18n } = useTranslation();
  const vendor = fetchVendorForSource(tool, candidate.source_id);
  const success = tool.status.verified.find((row) => row.source_id === candidate.source_id && row.model_id === candidate.model_id);
  const bad = ['needs_action', 'error', 'cooldown'].includes(candidate.state.status);
  const state = sourceStatePresentation(candidate.state, 'card', i18n.language);
  const text = !candidate.settings.enabled ? t('settings.models.webFetch.unused')
    : candidate.cooldown_until ? t('settings.models.searchServices.cooldown', { time: cooldownTime(candidate.cooldown_until, i18n.language) })
    : bad && state.key ? t(state.key, state.values)
    : success ? t('settings.models.tools.pair.ok', { date: formatMonthDay(success.at) })
    : vendor?.facts.built_in && candidate.kind !== 'local' ? t('settings.models.searchServices.keylessReady') : null;
  return text ? <span className={`model-hub-pill border ${!candidate.settings.enabled ? 'model-hub-accent-pill--neutral' : candidate.cooldown_until || bad ? 'model-hub-status-gold' : 'model-hub-status-mint'}`}>{text}</span> : null;
};

/** Reused in configured Source detail and in the built-in detail: no key fields. */
export const FetchServiceSettings: React.FC<{
  sourceId: string; tool: HostedWebFetchToolStatus; pending: boolean;
  onSave: (body: HostedToolPut) => void; onRetry: (sourceId: string) => void;
}> = ({ sourceId, tool, pending, onSave, onRetry }) => {
  const { t } = useTranslation();
  const candidate = tool.executor_candidates.find((row) => row.source_id === sourceId);
  const vendor = fetchVendorForSource(tool, sourceId);
  if (!candidate || !vendor) return null;
  return <section className="flex flex-col gap-3 border-b border-border px-5 py-4" aria-label={t('settings.models.tools.tool.web_fetch.name')}>
    <div className="flex items-center justify-between gap-3"><span className="font-semibold">{t('settings.models.webFetch.use')}</span><ToggleSwitch enabled={candidate.settings.enabled} disabled={pending} label={t('settings.models.webFetch.use')} onClick={() => onSave({ services: { [sourceId]: { enabled: !candidate.settings.enabled } } })} /></div>
    <div className="flex flex-wrap items-center gap-2"><FetchStatus candidate={candidate} tool={tool} />{candidate.kind !== 'local' && candidate.cooldown_until && <Button variant="outline" size="xs" disabled={pending} onClick={() => onRetry(sourceId)}>{t('settings.models.searchServices.retry')}</Button>}</div>
    {candidate.kind !== 'local' && <p className="model-hub-tool-note break-all">{t('settings.models.webFetch.endpoint')}: {t(`settings.models.webFetch.endpoints.${vendor.id}`, { defaultValue: candidate.model_id })}</p>}
    <FetchPrice candidate={candidate} />
    {candidate.kind !== 'local' && candidate.price.usd_per_1k !== 0 && <a href={candidate.price.url} target="_blank" rel="noopener noreferrer" className="model-hub-tool-note underline">{t('settings.models.searchServices.priceDate', { date: candidate.price.as_of })}</a>}
    {vendor.id === 'local' && <><p className="model-hub-tool-note">{t('settings.models.webFetch.localDetail')}</p><p className="model-hub-tool-note">{t('settings.models.webFetch.limits')}</p></>}
    {vendor.id === 'exa-mcp' && <p className="model-hub-tool-note">{t('settings.models.webFetch.exaDetail')}</p>}
    {vendor.id === 'jina-reader' && <><p className="model-hub-tool-note">{t('settings.models.webFetch.jinaDetail')}</p><a href="https://jina.ai/legal/" target="_blank" rel="noopener noreferrer" className="model-hub-tool-note underline">{t('settings.models.webFetch.terms')}</a></>}
  </section>;
};

export const WebFetchOrder: React.FC<{
  tool: HostedWebFetchToolStatus; sources: readonly Source[]; pending: boolean;
  onSave: (body: HostedToolPut) => void;
  onAdd?: (opener: HTMLButtonElement) => void;
  onOpen?: (sourceId: string, opener: HTMLButtonElement) => void;
  onRetry?: (sourceId: string) => void;
}> = ({ tool, sources, pending, onSave, onAdd, onOpen, onRetry }) => {
  const { t } = useTranslation();
  const [detail, setDetail] = React.useState<string | null>(null);
  const list = React.useRef<HTMLOListElement>(null);
  const moved = React.useRef<{ id: string; order: string; direction: number } | null>(null);
  const rows = tool.executor_candidates;
  const enabled = rows.filter((row) => row.settings.enabled);
  const order = rows.map((row) => row.source_id).join('\n');
  const name = (id: string) => {
    const vendor = fetchVendorForSource(tool, id);
    return sources.find((source) => source.id === id)?.display_name ?? t(`settings.models.webFetch.vendorName.${vendor?.id}`, { defaultValue: vendor?.name ?? id }) as string;
  };
  React.useEffect(() => {
    const request = moved.current;
    if (!request || order !== request.order) return;
    if (list.current?.contains(document.activeElement)) {
      const row = [...list.current.querySelectorAll<HTMLElement>('[data-executor]')].find((node) => node.dataset.executor === request.id);
      (row?.querySelector<HTMLButtonElement>(`[data-direction="${request.direction}"]:not(:disabled)`) ?? row?.querySelector<HTMLButtonElement>('[data-direction]:not(:disabled)'))?.focus();
    }
    moved.current = null;
  }, [order]);
  const move = (index: number, direction: -1 | 1) => {
    if (pending) return;
    const visible = enabled.map((row) => row.source_id);
    const next = executorOrderAfterMove(tool.executor_order, visible, index, direction);
    moved.current = { id: visible[index], direction, order: [...executorOrderAfterMove(null, visible, index, direction), ...rows.filter((r) => !r.settings.enabled).map((r) => r.source_id)].join('\n') };
    onSave({ executor_order: next });
  };
  return <div className="flex flex-col gap-3 border-t border-border p-4">
    <div className="flex items-center justify-between gap-3"><h4 className="font-semibold">{t('settings.models.webFetch.order')}</h4><Button variant="outline" size="xs" disabled={pending} onClick={() => onSave({ executor_order: null })}><RotateCcw aria-hidden />{t('settings.models.tools.order.reset')}</Button></div>
    <p className="model-hub-tool-note">{t('settings.models.webFetch.caption')}</p>
    <ol ref={list} className="flex flex-col gap-1.5" aria-label={t('settings.models.webFetch.order')}>
      {rows.map((row, index) => {
        const vendor = fetchVendorForSource(tool, row.source_id);
        const label = name(row.source_id);
        return <li key={row.source_id} data-executor={row.source_id} className="model-hub-tool-executor-row grid items-center rounded-lg border border-border bg-background">
          <span className="model-hub-order-ordinal">{row.settings.enabled ? index + 1 : '—'}</span>
          <div className="flex min-w-0 items-center gap-3"><SearchVendorGlyph vendor={vendor?.id ?? 'local'} /><div className="flex min-w-0 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2"><button type="button" data-search-source={row.source_id} className="truncate text-left font-semibold hover:underline" onClick={(event) => vendor?.facts.built_in && vendor.id !== 'exa-mcp' ? setDetail(row.source_id) : onOpen?.(row.source_id, event.currentTarget)}>{label}</button><span className="model-hub-pill model-hub-accent-pill--cyan border">{t(row.kind === 'local' ? 'settings.models.webFetch.local' : 'settings.models.webFetch.kind')}</span><FetchStatus candidate={row} tool={tool} /></div>
            <p className="model-hub-tool-note">{t(`settings.models.webFetch.row.${vendor?.id}`, { defaultValue: row.model_id })}</p>
            {row.kind !== 'local' && row.cooldown_until && <Button variant="outline" size="xs" className="self-start" disabled={pending} onClick={() => onRetry?.(row.source_id)}>{t('settings.models.searchServices.retry')}</Button>}
          </div></div>
          <FetchPrice candidate={row} />
          <span className="flex items-center gap-1"><span className="model-hub-tool-note mr-2 hidden xl:inline">{t('settings.models.webFetch.use')}</span><ToggleSwitch enabled={row.settings.enabled} disabled={pending} label={t('settings.models.webFetch.use')} onClick={() => onSave({ services: { [row.source_id]: { enabled: !row.settings.enabled } } })} />{([-1, 1] as const).map((direction) => <Button key={direction} type="button" variant="ghost" size="icon" data-direction={direction} className="model-hub-route-action model-hub-order-row-action" aria-label={t(direction < 0 ? 'settings.models.tools.order.moveUp' : 'settings.models.tools.order.moveDown', { source: label })} disabled={!row.settings.enabled || (direction < 0 ? index === 0 : index === enabled.length - 1)} aria-disabled={pending || undefined} onClick={() => move(index, direction)}>{direction < 0 ? <ArrowUp aria-hidden /> : <ArrowDown aria-hidden />}</Button>)}</span>
        </li>;
      })}
    </ol>
    <p className="model-hub-tool-note">{t('settings.models.webFetch.egress')}</p>
    <div className="flex flex-wrap items-center justify-between gap-3"><Button variant="outline" size="sm" disabled={pending} onClick={(event) => onAdd?.(event.currentTarget)}><Plus aria-hidden />{t('settings.models.webFetch.add')}</Button><p className="model-hub-tool-note flex items-start gap-2"><LockKeyhole className="mt-0.5 size-3.5 shrink-0" aria-hidden />{t('settings.models.webFetch.custody')}</p></div>
    {detail && <Dialog open onOpenChange={(open) => { if (!open) setDetail(null); }}><DialogContent className="max-w-xl border-border-strong bg-surface" closeLabel={t('common.close')}><DialogHeader><DialogTitle>{name(detail)}</DialogTitle><DialogDescription>{t('settings.models.webFetch.custody')}</DialogDescription></DialogHeader><FetchServiceSettings sourceId={detail} tool={tool} pending={pending} onSave={onSave} onRetry={(id) => onRetry?.(id)} /></DialogContent></Dialog>}
  </div>;
};
