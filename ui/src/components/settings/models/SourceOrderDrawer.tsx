import * as React from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { CirclePlus, GripVertical, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useToast } from '@/context/ToastContext';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { initialSeedState, savedSourcesKey, seedStep, type PendingWrite } from './asyncLifetime';
import { eligibilityOf } from './eligibility';
import { MenuDrawer } from './menus/MenuDrawer';
import { modelsApi } from './modelsApi';
import { movedOrder, sameIds } from './reorder';
import { ACCENT_ICON, ACCENT_TILE, backendVisual, sourceVisual } from './vendorMeta';
import type { AgentSourcesPut, AgentSupply, Source, SourcePolicy } from './types';

const ROW = 'flex items-center gap-2.5 rounded-xl border px-3 py-2.5 sm:gap-3 sm:px-3.5 sm:py-3';
const NUMBER =
  'grid size-5 shrink-0 place-items-center rounded-md border border-border bg-foreground/[0.03] text-[10.5px] font-bold text-muted sm:size-[22px] sm:text-[11px]';

const SourceTile: React.FC<{ source: Source }> = ({ source }) => {
  const { Icon, accent } = sourceVisual(source);
  return (
    <span className={cn('flex size-[30px] shrink-0 items-center justify-center rounded-[9px] sm:size-[34px]', ACCENT_TILE[accent])}>
      <Icon className={cn('size-3.5 sm:size-4', ACCENT_ICON[accent])} />
    </span>
  );
};

const Identity: React.FC<{ source: Source; detail?: string }> = ({ source, detail }) => (
  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
    <span className="truncate text-[13px] font-semibold text-foreground sm:text-[13.5px]">{source.display_name}</span>
    {(source.account_label || source.masked_credential || detail) && (
      <span className="truncate font-mono text-[10px] text-muted sm:text-[11px]">
        {[source.account_label ?? source.masked_credential, detail].filter(Boolean).join(' · ')}
      </span>
    )}
  </span>
);

const GroupHeader: React.FC<{ label: string; first?: boolean; children?: React.ReactNode }> = ({ label, first, children }) => (
  <div className={cn('flex items-center justify-between gap-3 px-1', first ? 'pt-0.5' : 'pt-2')}>
    <span className="text-[10px] font-bold tracking-[1px] text-muted">{label}</span>
    {children}
  </div>
);

const EnabledRow: React.FC<{
  source: Source;
  index: number;
  busy: boolean;
  onCommit: () => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
}> = ({ source, index, busy, onCommit, onMove, onRemove }) => {
  const { t } = useTranslation();
  const controls = useDragControls();
  return (
    <Reorder.Item
      value={source.id}
      dragListener={false}
      dragControls={controls}
      onDragEnd={onCommit}
      className={cn(ROW, 'list-none border-border bg-background')}
    >
      <button
        type="button"
        aria-label={t('settings.models.source.reorder') as string}
        aria-keyshortcuts="ArrowUp ArrowDown"
        disabled={busy}
        onPointerDown={(event) => controls.start(event)}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
          event.preventDefault();
          onMove(event.key === 'ArrowUp' ? -1 : 1);
        }}
        className="relative flex size-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted/60 active:cursor-grabbing disabled:cursor-default disabled:opacity-50"
      >
        <GripVertical className="size-4" />
        <span className="absolute -inset-3 sm:hidden" aria-hidden />
      </button>
      <span className={NUMBER}>{index + 1}</span>
      <SourceTile source={source} />
      <Identity source={source} />
      <button
        type="button"
        aria-label={t('settings.models.order.disable') as string}
        onClick={onRemove}
        disabled={busy}
        className="relative flex size-7 shrink-0 items-center justify-center rounded-md text-muted transition hover:bg-surface-2 hover:text-foreground disabled:opacity-50"
      >
        <X className="size-4" />
        <span className="absolute -inset-2 sm:hidden" aria-hidden />
      </button>
    </Reorder.Item>
  );
};

export const SourceOrderDrawer: React.FC<{
  open: boolean;
  agent: AgentSupply;
  agents: AgentSupply[];
  sources: Source[];
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  orderWrite: PendingWrite;
}> = ({ open, agent, agents, sources, onClose, onSaved, orderWrite }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const mobile = useIsMobile();
  const { Icon, accent } = backendVisual(agent.backend);
  const [policy, setPolicy] = React.useState<SourcePolicy>(agent.sources?.policy ?? 'follow');
  const [order, setOrder] = React.useState<string[]>(agent.sources?.order ?? []);
  const saving = orderWrite.pending;
  const saved = React.useRef<{ policy: SourcePolicy; order: string[] }>({ policy, order });
  const seed = React.useRef(initialSeedState);
  const authoritative = savedSourcesKey(agent);

  React.useEffect(() => {
    if (!open) return;
    const step = seedStep(seed.current, authoritative);
    seed.current = step.state;
    if (!step.reseed) return;
    const next = { policy: agent.sources?.policy ?? 'follow', order: agent.sources?.order ?? [] };
    saved.current = next;
    setPolicy(next.policy);
    setOrder(next.order);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, authoritative]);

  const backendName = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend }) as string;
  const byId = React.useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);
  const enabledSources = order.map((sourceId) => byId.get(sourceId)).filter((source): source is Source => Boolean(source));
  const enabledIds = enabledSources.map((source) => source.id);
  const rest = sources.filter((source) => !order.includes(source.id));
  const disabledSources = rest.filter((source) => eligibilityOf(agent, source.id).eligible);
  const ineligible = rest
    .map((source) => ({ source, ...eligibilityOf(agent, source.id) }))
    .filter((entry) => !entry.eligible);

  const backendLabel = (backend: string) =>
    t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;
  const vendorLabel = (source: Source) =>
    t(`settings.models.addKey.vendors.${source.vendor}`, { defaultValue: source.vendor }) as string;
  const ineligibleDetail = (source: Source, reasonKey: string | null): string => {
    if (!reasonKey) return t('settings.models.order.ineligibleUnknown', { backend: backendName }) as string;
    if (reasonKey === 'models.eligibility.subscription_wrong_client') {
      const owners = agents.filter(
        (candidate) => candidate.backend !== agent.backend && eligibilityOf(candidate, source.id).eligible,
      );
      return owners.length === 1
        ? t(reasonKey, { vendor: vendorLabel(source), backend: backendLabel(owners[0].backend) }) as string
        : t('settings.models.order.ineligibleClientUnknown', { vendor: vendorLabel(source) }) as string;
    }
    return t(reasonKey, { vendor: vendorLabel(source) }) as string;
  };

  const persist = (body: AgentSourcesPut, next: { policy: SourcePolicy; order: string[] }) =>
    orderWrite.track(async () => {
      const previous = saved.current;
      setPolicy(next.policy);
      setOrder(next.order);
      try {
        const echoed = await modelsApi.putAgentSources(agent.backend, body);
        const adopted = {
          policy: echoed.sources?.policy ?? next.policy,
          order: echoed.sources?.order ?? next.order,
        };
        saved.current = adopted;
        setPolicy(adopted.policy);
        setOrder(adopted.order);
        await Promise.resolve(onSaved(echoed)).catch(() => {});
      } catch {
        saved.current = previous;
        setPolicy(previous.policy);
        setOrder([...previous.order]);
        showToast(t('settings.models.toast.reorderFailed') as string, 'error');
      }
    });

  const commitOrder = (nextOrder: string[]) => {
    if (sameIds(saved.current.order, nextOrder)) {
      setOrder([...saved.current.order]);
      return;
    }
    void persist({ policy: 'custom', order: nextOrder }, { policy: 'custom', order: nextOrder });
  };
  const restoreDefault = () => void persist({ policy: 'follow' }, { policy: 'follow', order });

  return (
    <MenuDrawer
      open={open}
      onClose={onClose}
      Icon={Icon}
      accent={accent}
      title={t('settings.models.order.title', { backend: backendName }) as string}
      subtitle={t(mobile ? 'settings.models.order.subtitleShort' : 'settings.models.order.subtitle', { backend: backendName }) as string}
      footer={
        <Button variant="brand" size="sm" className="h-10" onClick={onClose}>
          {t('settings.models.menus.done')}
        </Button>
      }
    >
      <div className="flex flex-col gap-2.5">
        <GroupHeader label={t('settings.models.order.groupEnabled', { count: enabledIds.length }) as string} first>
          {policy === 'custom' && (
            <span className="text-[11.5px] font-medium text-muted">
              {t('settings.models.order.customized')}
              <span aria-hidden> · </span>
              <button
                type="button"
                onClick={restoreDefault}
                disabled={saving}
                className="font-semibold text-mint hover:text-mint/80 disabled:opacity-50"
              >
                {t('settings.models.order.restore')}
              </button>
            </span>
          )}
        </GroupHeader>

        {enabledSources.length === 0 ? (
          <p className="rounded-xl border border-border bg-foreground/[0.02] px-3.5 py-3 text-[12px] text-muted">
            {t('settings.models.order.enabledEmpty', { backend: backendName })}
          </p>
        ) : (
          <Reorder.Group axis="y" values={enabledIds} onReorder={setOrder} className="flex list-none flex-col gap-2.5">
            {enabledSources.map((source, index) => (
              <EnabledRow
                key={source.id}
                source={source}
                index={index}
                busy={saving}
                onCommit={() => commitOrder(enabledIds)}
                onMove={(delta) => commitOrder(movedOrder(enabledIds, index, delta))}
                onRemove={() => commitOrder(enabledIds.filter((sourceId) => sourceId !== source.id))}
              />
            ))}
          </Reorder.Group>
        )}

        {disabledSources.length > 0 && (
          <>
            <GroupHeader label={t('settings.models.order.groupDisabled', { count: disabledSources.length }) as string} />
            <div className="flex flex-col gap-2.5 rounded-xl border border-dashed border-border p-2">
              {disabledSources.map((source) => (
                <button
                  key={source.id}
                  type="button"
                  aria-label={t('settings.models.order.enableAria', { name: source.display_name }) as string}
                  onClick={() => commitOrder([...enabledIds, source.id])}
                  disabled={saving}
                  className={cn(ROW, 'w-full border-transparent bg-background text-left transition hover:border-border-strong disabled:opacity-50')}
                >
                  <CirclePlus className="size-[17px] shrink-0 text-mint" />
                  <SourceTile source={source} />
                  <Identity source={source} />
                  <span className="shrink-0 text-[11.5px] font-semibold text-foreground">{t('settings.models.order.enable')}</span>
                </button>
              ))}
            </div>
          </>
        )}

        {ineligible.length > 0 && (
          <>
            <GroupHeader label={t('settings.models.order.groupIneligible', { count: ineligible.length }) as string} />
            {ineligible.map(({ source, reasonKey }) => (
              <div key={source.id} className={cn(ROW, 'border-border opacity-55')}>
                <SourceTile source={source} />
                <Identity source={source} detail={ineligibleDetail(source, reasonKey)} />
              </div>
            ))}
          </>
        )}
      </div>
    </MenuDrawer>
  );
};

export default SourceOrderDrawer;
