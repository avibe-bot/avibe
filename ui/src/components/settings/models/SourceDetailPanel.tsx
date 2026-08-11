import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Info, Loader2, MoreHorizontal, Plus, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { formatRelativeTime } from '@/lib/relativeTime';
import { apiFailure, modelsApi } from './modelsApi';
import { sourceStatePresentation } from './sourceStatePresentation';
import { useDeadlineClock } from './useDeadlineClock';
import { ACCENT_ICON, ACCENT_TILE, isCustomEndpoint, sourceVisual } from './vendorMeta';
import type { AdoptedBy, RouteHopRef, Source, SuppliedModel, SupplyGap } from './types';

const ManualModelMenu: React.FC<{
  model: SuppliedModel;
  busy: boolean;
  onRemove: () => void;
}> = ({ model, busy, onRemove }) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const label = `${t('settings.models.sourceDetail.row.remove')} ${model.id}`;
  return (
    <ResponsiveMenu
      open={open}
      onOpenChange={setOpen}
      sheetTitle={model.id}
      className="w-40"
      trigger={<button type="button" disabled={busy} aria-label={label} title={label} className="grid size-8 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-foreground"><MoreHorizontal className="size-4" /></button>}
    >
      <button type="button" role="menuitem" className="flex w-full items-center rounded-md px-2.5 py-2 text-left text-[12px] font-semibold text-destructive hover:bg-destructive/[0.08]" onClick={() => { setOpen(false); onRemove(); }}>{t('settings.models.sourceDetail.row.remove')}</button>
    </ResponsiveMenu>
  );
};

type GuardedAction =
  | { kind: 'refetch'; hops: RouteHopRef[]; gaps: SupplyGap[] }
  | { kind: 'remove'; model: SuppliedModel; hops: RouteHopRef[]; gaps: SupplyGap[] };

export const GuardGapList: React.FC<{ gaps: SupplyGap[] }> = ({ gaps }) => {
  const { t, i18n } = useTranslation();
  if (gaps.length === 0) return null;
  return (
    <>
      <div className="model-hub-guard-label"><p>{t('settings.models.guard.gap.label')}</p><span>{t('settings.models.gateway.modelCount', { count: gaps.length })}</span></div>
      <div className="model-hub-guard-list">
        {gaps.map((gap) => (
          <div key={`${gap.backend}:${gap.model_id}`} className="model-hub-guard-hop">
            <span className="min-w-0 flex-1">
              <strong>{t('settings.models.guard.gap.subject', { backend: t(`settings.models.backends.${gap.backend}`, { defaultValue: gap.backend }), menuModel: gap.model_id })}</strong>
              {gap.agents.length > 0 && <span>{t('settings.models.guard.gap.agents', { agents: gap.agents.join(i18n.language.startsWith('zh') ? '、' : ', ') })}</span>}
            </span>
          </div>
        ))}
      </div>
    </>
  );
};

const enteredHost = (source: Source): string | null => {
  if (!source.base_url || !isCustomEndpoint(source)) return null;
  try {
    return new URL(source.base_url).host;
  } catch {
    return null;
  }
};

const TierEditor: React.FC<{
  source: Source;
  model: SuppliedModel;
  onMutating: () => void;
  onChanged: () => Promise<void> | void;
}> = ({ source, model, onMutating, onChanged }) => {
  const { t } = useTranslation();
  const [tiers, setTiers] = React.useState(model.reasoning_efforts ?? []);
  const [draft, setDraft] = React.useState('');
  const [editing, setEditing] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [failedNext, setFailedNext] = React.useState<string[] | null>(null);
  React.useEffect(() => setTiers(model.reasoning_efforts ?? []), [model.reasoning_efforts]);

  const commit = async (next: string[]): Promise<boolean> => {
    if (saving) return false;
    const previous = tiers;
    onMutating();
    setFailedNext(null);
    setTiers(next);
    setSaving(true);
    try {
      await modelsApi.updateModelReasoningEfforts(source.id, model.id, next);
      await onChanged();
      return true;
    } catch (error) {
      setTiers(previous);
      setFailedNext(next);
      if (apiFailure(error)?.code === 'source_not_found') await onChanged();
      return false;
    } finally {
      setSaving(false);
    }
  };
  const add = async () => {
    const value = draft.trim();
    if (!value || tiers.includes(value)) return;
    if (await commit([...tiers, value])) setDraft('');
  };
  const retry = async () => {
    if (!failedNext) return;
    if (await commit(failedNext) && draft.trim() && failedNext.includes(draft.trim())) setDraft('');
  };
  if (!editing) {
    return (
      <button
        type="button"
        className="flex min-w-0 flex-wrap items-center gap-1.5 text-left"
        onClick={() => { setFailedNext(null); setEditing(true); }}
      >
        {tiers.length > 0 ? tiers.map((tier) => (
          <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex rounded-full border border-border text-foreground">{tier}</span>
        )) : <span className="model-hub-source-tier text-muted">{t('settings.models.sourceDetail.tiers.empty')}</span>}
        <span className="model-hub-source-tier font-semibold text-mint">{t(tiers.length > 0 ? 'settings.models.sourceDetail.tiers.add' : 'settings.models.sourceDetail.tiers.addFirst')}</span>
      </button>
    );
  }
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {tiers.map((tier) => (
        <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex items-center gap-1 rounded-full border border-border text-foreground">
          {tier}
          <button type="button" disabled={saving} onClick={() => void commit(tiers.filter((item) => item !== tier))} aria-label={t('settings.models.sourceDetail.tiers.remove', { tier }) as string} className="text-muted hover:text-foreground disabled:opacity-50">
            <X className="size-2.5" />
          </button>
        </span>
      ))}
      <Input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => { if (!failedNext) { setDraft(''); setEditing(false); } }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); void add(); }
          if (event.key === 'Escape') { event.preventDefault(); setDraft(''); event.currentTarget.blur(); }
        }}
        disabled={saving}
        placeholder={t('settings.models.sourceDetail.tiers.inputHint') as string}
        className="model-hub-source-tier h-7 w-28 rounded-full border-mint/40 px-2.5"
      />
      {failedNext && <span className="model-hub-source-tier inline-flex items-center gap-1.5 text-destructive">
        {t('settings.models.sourceDetail.fail.tier')}
        <button type="button" disabled={saving} onMouseDown={(event) => event.preventDefault()} onClick={() => void retry()} className="font-semibold underline underline-offset-2 disabled:opacity-50">{t('settings.models.sourceDetail.retry')}</button>
      </span>}
    </div>
  );
};

const DraftTiers: React.FC<{
  tiers: string[];
  onChange: (tiers: string[]) => void;
}> = ({ tiers, onChange }) => {
  const { t } = useTranslation();
  const [draft, setDraft] = React.useState('');
  const add = () => {
    const value = draft.trim();
    if (!value || tiers.includes(value)) return;
    onChange([...tiers, value]);
    setDraft('');
  };
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {tiers.map((tier) => (
        <span key={tier} className="model-hub-source-tier model-hub-source-tier-chip inline-flex items-center gap-1 rounded-full border border-border text-foreground">
          {tier}
          <button type="button" onClick={() => onChange(tiers.filter((item) => item !== tier))} aria-label={t('settings.models.sourceDetail.tiers.remove', { tier }) as string} className="text-muted hover:text-foreground"><X className="size-2.5" /></button>
        </span>
      ))}
      <Input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') { event.preventDefault(); add(); }
          if (event.key === 'Escape') { event.preventDefault(); setDraft(''); event.currentTarget.blur(); }
        }}
        placeholder={t('settings.models.sourceDetail.tiers.inputHint') as string}
        className="model-hub-source-tier h-7 w-36 rounded-full border-mint/40 px-2.5"
      />
    </div>
  );
};

export const SourceDetailPanel: React.FC<{
  source: Source;
  adoptedBy?: readonly AdoptedBy[];
  onChanged: () => Promise<void> | void;
}> = ({ source, adoptedBy = [], onChanged }) => {
  const { t, i18n } = useTranslation();
  const now = useDeadlineClock(source.state.status === 'cooldown' ? source.state.retry_at : null);
  const { Icon, accent } = sourceVisual(source);
  const [busy, setBusy] = React.useState(false);
  const [manualDraft, setManualDraft] = React.useState<{ modelId: string; tiers: string[]; failed: boolean } | null>(null);
  const [guard, setGuard] = React.useState<GuardedAction | null>(null);
  const [result, setResult] = React.useState<{ added: string[]; removed: string[] } | null>(null);
  const [refetchFailed, setRefetchFailed] = React.useState(false);
  const [removeFailure, setRemoveFailure] = React.useState<string | null>(null);
  const models = source.models;

  const guardedFailure = (error: unknown): { hops: RouteHopRef[]; gaps: SupplyGap[] } | null => {
    const failure = apiFailure(error);
    if (!failure || (failure.wouldRemoveHops.length === 0 && failure.wouldInterrupt.length === 0)) return null;
    return { hops: failure.wouldRemoveHops, gaps: failure.wouldInterrupt };
  };
  const refetch = async (force = false) => {
    setResult(null);
    setRefetchFailed(false);
    setBusy(true);
    const before = new Set(source.models.map((model) => model.id));
    try {
      const answer = await modelsApi.refreshSource(source.id, force);
      const after = new Set(answer.source.models.map((model) => model.id));
      const added = [...after].filter((id) => !before.has(id));
      const removed = [...before].filter((id) => !after.has(id));
      setResult({ added, removed });
      setGuard(null);
      await onChanged();
    } catch (error) {
      const refusal = guardedFailure(error);
      if (refusal && !force) setGuard({ kind: 'refetch', ...refusal });
      else {
        setRefetchFailed(true);
        await onChanged();
      }
    } finally {
      setBusy(false);
    }
  };
  const addManualModel = async () => {
    if (!manualDraft || busy) return;
    const modelId = manualDraft.modelId.trim();
    if (!modelId || source.models.some((model) => model.id === modelId)) return;
    setResult(null);
    setRefetchFailed(false);
    setBusy(true);
    try {
      await modelsApi.addCustomModel(source.id, {
        model_id: modelId,
        display_name: null,
        reasoning_efforts: manualDraft.tiers,
      });
      setManualDraft(null);
      setResult(null);
      await onChanged();
    } catch (error) {
      if (apiFailure(error)?.code === 'source_not_found') await onChanged();
      setManualDraft((current) => current ? { ...current, failed: true } : current);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (model: SuppliedModel, force = false) => {
    setResult(null);
    setRefetchFailed(false);
    setRemoveFailure(null);
    setBusy(true);
    try {
      await modelsApi.deleteCustomModel(source.id, model.id, force);
      setGuard(null);
      await onChanged();
    } catch (error) {
      const refusal = guardedFailure(error);
      if (refusal && !force) setGuard({ kind: 'remove', model, ...refusal });
      else {
        // A lost DELETE response is an unknown outcome. Re-read before another
        // non-idempotent attempt so an already-committed removal disappears.
        await onChanged();
        setRemoveFailure(model.id);
      }
    } finally {
      setBusy(false);
    }
  };
  const confirmGuard = () => {
    if (!guard) return;
    if (guard.kind === 'refetch') void refetch(true);
    else void remove(guard.model, guard.hops.length > 0 || guard.gaps.length > 0);
  };
  const adoptedBackends = [...new Set(adoptedBy.map(({ backend }) => t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string))];
  const state = sourceStatePresentation(source.state, 'detail', i18n.language, now, {
    backends: adoptedBackends,
    native: source.supply_channel === 'native_cli',
  });
  const host = enteredHost(source);

  return (
    <div className="model-hub-source-detail">
      <section className="model-hub-source-bar flex flex-col border border-border bg-surface sm:flex-row sm:items-center">
        <span className={cn('model-hub-source-tile flex shrink-0 items-center justify-center', ACCENT_TILE[accent])}><Icon className={cn('size-[18px]', ACCENT_ICON[accent])} /></span>
        <div className="min-w-0 flex-1">
          <h2 className="model-hub-source-title truncate font-bold text-foreground">{source.display_name}</h2>
          <p className={cn('mt-1 flex flex-wrap items-center gap-x-2 gap-y-1', state.textClass)}>
            <span className="model-hub-source-state flex items-center gap-1.5"><span className={cn('size-[5px] rounded-full', state.dotClass)} />{state.key && t(state.key, state.values)}</span>
            {source.last_discovered_at && <span className="model-hub-source-age text-muted">{t('settings.models.sourceDetail.status.listUpdated', { time: formatRelativeTime(source.last_discovered_at, t) })}</span>}
          </p>
          <p className="model-hub-source-summary mt-1 truncate font-mono">{t(host ? 'settings.models.sourceDetail.summary' : 'settings.models.gateway.modelCount', { host, count: source.models.length })}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" className="model-hub-source-action" disabled={busy} onClick={() => void refetch()}>{busy ? <Loader2 className="animate-spin" /> : <RefreshCw />}{t('settings.models.sourceDetail.action.refetch')}</Button>
          {source.kind === 'api_key' && <Button variant="secondary" size="sm" className="model-hub-source-action text-mint" disabled={busy || manualDraft !== null} onClick={() => setManualDraft({ modelId: '', tiers: [], failed: false })}><Plus />{t('settings.models.sourceDetail.action.addModel')}</Button>}
        </div>
      </section>
      {result && result.removed.length > 0 && <p className="rounded-lg border border-gold/25 bg-gold/10 px-3 py-2 text-[11.5px] text-gold">{t('settings.models.sourceDetail.refetch.removed', { count: result.removed.length, models: result.removed.join(', ') })}</p>}
      {result && result.added.length === 0 && result.removed.length === 0 && <p className="rounded-lg border border-mint/25 bg-mint-soft/40 px-3 py-2 text-[11.5px] text-mint">{t('settings.models.sourceDetail.refetch.unchangedOnly')}</p>}
      {refetchFailed && <p className="rounded-lg border border-destructive/25 bg-destructive/[0.08] px-3 py-2 text-[11.5px] text-destructive">{t('settings.models.sourceDetail.fail.refetch')}</p>}
      <section className="model-hub-source-table overflow-hidden border border-border bg-surface">
        <div className="model-hub-source-table-head hidden border-b border-border font-semibold md:grid">
          <span>{t('settings.models.sourceDetail.col.id')}</span><span>{t('settings.models.sourceDetail.col.entry')}</span><span className="flex items-center gap-1">{t('settings.models.sourceDetail.col.tiers')}<Info className="size-3" /></span><span />
        </div>
        {models.length === 0 && !manualDraft ? <p className="px-5 py-12 text-center text-[12px] text-muted">{t(source.last_discovered_at ? 'settings.models.sourceDetail.empty' : 'settings.models.sourceDetail.emptyNeverFetched')}</p> : models.map((model) => (
          <div key={model.id} className="model-hub-source-table-row grid gap-3 border-b border-border last:border-b-0 md:items-center md:gap-y-0">
            <span className="flex min-w-0 items-center gap-2"><span className="model-hub-source-model truncate font-mono text-foreground" title={model.id}>{model.id}</span>{result?.added.includes(model.id) && <span className="model-hub-source-pill rounded-full border border-mint/30 bg-mint-soft px-2 py-0.5 font-semibold text-mint">{t('settings.models.sourceDetail.refetch.added')}</span>}</span>
            <span className="model-hub-source-pill model-hub-source-entry-pill w-fit rounded-full border border-border font-semibold text-muted">{t(`settings.models.sourceDetail.entry.${model.provenance === 'discovered' ? 'auto' : 'manual'}`)}</span>
            <TierEditor source={source} model={model} onMutating={() => { setResult(null); setRefetchFailed(false); }} onChanged={onChanged} />
            <div className="flex items-center justify-end gap-2">
              {removeFailure === model.id && <span className="model-hub-source-tier text-right text-destructive">{t('settings.models.sourceDetail.fail.removeModel')} <button type="button" disabled={busy} onClick={() => void remove(model)} className="font-semibold underline underline-offset-2">{t('settings.models.sourceDetail.retry')}</button></span>}
              {model.provenance === 'manual' && <ManualModelMenu model={model} busy={busy} onRemove={() => void remove(model)} />}
            </div>
          </div>
        ))}
        {manualDraft && (
          <div data-manual-model-draft className="model-hub-source-table-draft grid gap-3 border-y border-mint/20 bg-mint/[0.05] md:items-center md:gap-y-0">
            <Input
              autoFocus
              value={manualDraft.modelId}
              onChange={(event) => setManualDraft((current) => current ? { ...current, modelId: event.target.value, failed: false } : current)}
              placeholder={t('settings.models.sourceDetail.col.id') as string}
              className="model-hub-source-manual-id model-hub-source-model h-8 font-mono"
            />
            <span className="model-hub-source-pill model-hub-source-entry-pill w-fit rounded-full border border-border font-semibold text-muted">{t('settings.models.sourceDetail.entry.manual')}</span>
            <DraftTiers tiers={manualDraft.tiers} onChange={(tiers) => setManualDraft((current) => current ? { ...current, tiers, failed: false } : current)} />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="xs" disabled={busy} onClick={() => setManualDraft(null)}>{t('common.cancel')}</Button>
              <Button size="xs" disabled={busy || !manualDraft.modelId.trim() || source.models.some((model) => model.id === manualDraft.modelId.trim())} onClick={() => void addManualModel()}>{manualDraft.failed ? t('settings.models.sourceDetail.retry') : t('settings.models.sourceDetail.action.addModel')}</Button>
            </div>
            {manualDraft.failed && <p className="text-[11px] text-destructive md:col-span-4">{t('settings.models.sourceDetail.fail.addModel')}</p>}
          </div>
        )}
        {source.kind === 'api_key' && !manualDraft && (
          <button type="button" className="model-hub-source-table-add model-hub-source-model flex w-full items-center gap-2 border-b border-border text-left font-semibold text-mint" onClick={() => setManualDraft({ modelId: '', tiers: [], failed: false })}>
            <Plus className="size-3.5" />
            {t('settings.models.sourceDetail.action.addModel')}
            <span className="text-[10.5px] font-normal text-muted">{t('settings.models.sourceDetail.addRow.hint')}</span>
          </button>
        )}
      </section>
      <p className="model-hub-source-footnote leading-relaxed">{t('settings.models.sourceDetail.footnote')}</p>
      <DialogPrimitive.Root open={guard !== null} onOpenChange={(open) => !open && !busy && setGuard(null)}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="model-hub-guard-overlay fixed inset-0 z-50" />
          <DialogPrimitive.Content className="model-hub-guard-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto border border-border-strong bg-surface outline-none">
            <header className="model-hub-guard-head">
              <div className="flex items-center justify-between gap-3">
                <DialogPrimitive.Title className="model-hub-guard-title text-foreground">{t(`settings.models.guard.title.${guard?.kind === 'remove' ? 'removeModel' : 'refetch'}`, { model: guard?.kind === 'remove' ? guard.model.id : undefined, source: source.display_name })}</DialogPrimitive.Title>
                <DialogPrimitive.Close asChild><Button type="button" variant="ghost" size="icon" className="model-hub-guard-close" disabled={busy} aria-label={t('settings.models.guard.cancel')} title={t('settings.models.guard.cancel')}><X aria-hidden /></Button></DialogPrimitive.Close>
              </div>
              <DialogPrimitive.Description className="model-hub-guard-subtitle">{t(`settings.models.guard.subtitle.${guard?.kind === 'remove' ? 'removeModel' : 'refetch'}`)}</DialogPrimitive.Description>
            </header>
            {guard && <div className="model-hub-guard-body">
              {guard.hops.length > 0 && <>
                <div className="model-hub-guard-label"><p>{t('settings.models.guard.label')}</p><span>{t('settings.models.guard.count', { count: guard.hops.length })}</span></div>
                <div className="model-hub-guard-list">{guard.hops.map((hop) => <div key={`${hop.backend}:${hop.menu_model}:${hop.source_id}:${hop.model_id}`} className="model-hub-guard-hop"><span className="min-w-0 flex-1"><strong>{t(`settings.models.backends.${hop.backend}`, { defaultValue: hop.backend })} · {hop.menu_model}</strong><span>{hop.model_id}</span></span></div>)}</div>
              </>}
              <GuardGapList gaps={guard.gaps} />
              <p className={cn('model-hub-guard-hint', guard.gaps.length > 0 && 'text-destructive')}><Info aria-hidden />{t(`settings.models.guard.hint.${guard.gaps.length > 0 ? 'interrupt' : 'safe'}`)}</p>
            </div>}
            <footer className="model-hub-guard-foot"><Button variant="outline" className="model-hub-guard-action" onClick={() => setGuard(null)} disabled={busy}>{t('settings.models.guard.cancel')}</Button><Button variant="destructive" className="model-hub-guard-action" onClick={confirmGuard} disabled={busy}>{busy && <Loader2 className="animate-spin" />}{t(`settings.models.guard.confirm.${guard?.kind === 'remove' ? 'removeModel' : 'refetch'}`)}</Button></footer>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </div>
  );
};
