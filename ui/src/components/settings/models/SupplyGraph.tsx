import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import type { SupplyRelation, SupplyRelationKind } from './supplyRelations';

type DrawnRelation = SupplyRelation & {
  path: string;
  startX: number;
  startY: number;
  endX: number;
  endY: number;
};

type HighlightedEndpoint =
  | { kind: 'source'; id: string }
  | { kind: 'agent'; id: string };

const endpointFromTarget = (target: EventTarget | null, container: HTMLElement): HighlightedEndpoint | null => {
  if (!(target instanceof Element)) return null;
  const source = target.closest<HTMLElement>('[data-source-id]');
  if (source && container.contains(source) && source.dataset.sourceId) return { kind: 'source', id: source.dataset.sourceId };
  const agent = target.closest<HTMLElement>('[data-agent-backend]');
  if (agent && container.contains(agent) && agent.dataset.agentBackend) return { kind: 'agent', id: agent.dataset.agentBackend };
  return null;
};

const sameEndpoint = (left: HighlightedEndpoint | null, right: HighlightedEndpoint | null): boolean =>
  left?.kind === right?.kind && left?.id === right?.id;

export const SupplyGraph: React.FC<{
  containerRef: React.RefObject<HTMLDivElement | null>;
  relations: SupplyRelation[];
}> = ({ containerRef, relations }) => {
  const [drawing, setDrawing] = React.useState<{ width: number; height: number; wires: DrawnRelation[] } | null>(null);
  const [hoveredEndpoint, setHoveredEndpoint] = React.useState<HighlightedEndpoint | null>(null);
  const [focusedEndpoint, setFocusedEndpoint] = React.useState<HighlightedEndpoint | null>(null);

  React.useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const measure = () => {
      const root = container.getBoundingClientRect();
      const wires: DrawnRelation[] = [];
      for (const relation of relations) {
        const source = Array.from(container.querySelectorAll<HTMLElement>('[data-source-id]')).find((element) => element.dataset.sourceId === relation.sourceId);
        const backend = Array.from(container.querySelectorAll<HTMLElement>('[data-agent-backend]')).find((element) => element.dataset.agentBackend === relation.backend);
        if (!source || !backend) continue;
        const sourceBounds = source.getBoundingClientRect();
        const backendBounds = backend.getBoundingClientRect();
        const startX = sourceBounds.right - root.left;
        const startY = sourceBounds.top - root.top + sourceBounds.height / 2;
        const endX = backendBounds.left - root.left;
        const endY = backendBounds.top - root.top + backendBounds.height / 2;
        const railX = startX + (endX - startX) / 2;
        wires.push({ ...relation, startX, startY, endX, endY, path: `M ${startX} ${startY} C ${railX} ${startY}, ${railX} ${endY}, ${endX} ${endY}` });
      }
      setDrawing({ width: root.width, height: root.height, wires });
    };
    measure();
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    resize?.observe(container);
    container.addEventListener('scroll', measure, true);
    window.addEventListener('resize', measure);
    return () => {
      resize?.disconnect();
      container.removeEventListener('scroll', measure, true);
      window.removeEventListener('resize', measure);
    };
  }, [containerRef, relations]);

  React.useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const handlePointerOver = (event: PointerEvent) => setHoveredEndpoint(endpointFromTarget(event.target, container));
    const handlePointerOut = (event: PointerEvent) => {
      const previous = endpointFromTarget(event.target, container);
      const next = endpointFromTarget(event.relatedTarget, container);
      if (previous && !sameEndpoint(previous, next)) setHoveredEndpoint(next);
    };
    const handlePointerLeave = () => setHoveredEndpoint(null);
    const handleFocusIn = (event: FocusEvent) => setFocusedEndpoint(endpointFromTarget(event.target, container));
    const handleFocusOut = (event: FocusEvent) => {
      const previous = endpointFromTarget(event.target, container);
      const next = endpointFromTarget(event.relatedTarget, container);
      if (previous && !sameEndpoint(previous, next)) setFocusedEndpoint(next);
    };
    container.addEventListener('pointerover', handlePointerOver);
    container.addEventListener('pointerout', handlePointerOut);
    container.addEventListener('pointerleave', handlePointerLeave);
    container.addEventListener('focusin', handleFocusIn);
    container.addEventListener('focusout', handleFocusOut);
    return () => {
      container.removeEventListener('pointerover', handlePointerOver);
      container.removeEventListener('pointerout', handlePointerOut);
      container.removeEventListener('pointerleave', handlePointerLeave);
      container.removeEventListener('focusin', handleFocusIn);
      container.removeEventListener('focusout', handleFocusOut);
    };
  }, [containerRef]);

  if (!drawing || drawing.wires.length === 0) return null;
  const sourceAnchors = Array.from(new Map(drawing.wires.map((wire) => [wire.sourceId, wire])).values());
  const agentAnchors = Array.from(new Map(drawing.wires.map((wire) => [wire.backend, wire])).values());
  return (
    <svg aria-hidden="true" className="pointer-events-none absolute inset-0 z-10 hidden size-full overflow-hidden xl:block" viewBox={`0 0 ${drawing.width} ${drawing.height}`} preserveAspectRatio="none">
      {drawing.wires.map((wire) => {
        const highlighted = [hoveredEndpoint, focusedEndpoint].some((endpoint) => endpoint?.kind === 'source'
          ? endpoint.id === wire.sourceId
          : endpoint?.kind === 'agent' && endpoint.id === wire.backend);
        return (
          <path
            key={`${wire.sourceId}:${wire.backend}`}
            className={cn(`model-hub-wire model-hub-wire--${wire.kind}`, highlighted && 'model-hub-wire--highlighted')}
            data-wire-source-id={wire.sourceId}
            data-wire-agent-backend={wire.backend}
            d={wire.path}
          />
        );
      })}
      {sourceAnchors.map((anchor) => (
        <circle key={anchor.sourceId} className="model-hub-wire-node model-hub-wire-node--shared-anchor" cx={anchor.startX} cy={anchor.startY} />
      ))}
      {agentAnchors.map((anchor) => (
        <circle key={anchor.backend} className="model-hub-wire-node model-hub-wire-node--shared-anchor" cx={anchor.endX} cy={anchor.endY} />
      ))}
    </svg>
  );
};

const LEGEND_ORDER: SupplyRelationKind[] = ['native', 'gateway', 'passthrough', 'connected_unused', 'takeover', 'unavailable'];
const LEGEND_COPY: Record<SupplyRelationKind, string> = {
  native: 'native',
  gateway: 'viaGateway',
  passthrough: 'passthrough',
  connected_unused: 'connectedUnused',
  takeover: 'takeover',
  unavailable: 'unavailable',
};

export const SupplyLegend: React.FC<{ relations: SupplyRelation[] }> = ({ relations }) => {
  const { t } = useTranslation();
  const visible = new Set(relations.map((relation) => relation.kind));
  if (visible.size === 0) return null;
  return (
    <div className="model-hub-legend flex flex-wrap items-center justify-between gap-x-5 gap-y-2 px-0.5 py-2 text-[11px] text-muted">
      <div className="flex flex-wrap items-center gap-x-[18px] gap-y-2">
        {LEGEND_ORDER.filter((kind) => visible.has(kind)).map((kind) => (
          <span key={kind} className="flex items-center gap-[7px] font-medium">
            <span className={cn(`model-hub-legend-swatch model-hub-legend-swatch--${kind}`, {
              'bg-cyan': kind === 'native',
              'bg-mint': kind === 'gateway',
              'bg-foreground/15': kind === 'connected_unused',
              'bg-violet': kind === 'takeover',
              'bg-gold': kind === 'unavailable',
            })} />
            {t(`settings.models.legend.${LEGEND_COPY[kind]}`)}
          </span>
        ))}
      </div>
      <span className="model-hub-ink-59 flex items-center gap-1.5">
        <span className="hidden sm:inline">{t('settings.models.legend.note')}</span>
        <ModelHubInfoHint label={t('settings.models.legend.note')} content={t('settings.models.legend.note')} className="model-hub-ink-59 size-[20px]" align="end" />
      </span>
    </div>
  );
};
