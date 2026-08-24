import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import type { AgentBackend } from './types';
import type { SupplyRelation, SupplyRelationKind } from './supplyRelations';

type DrawnRelation = SupplyRelation & {
  path: string;
  startX: number;
  startY: number;
  endX: number;
  endY: number;
};

export const SupplyGraph: React.FC<{
  containerRef: React.RefObject<HTMLDivElement | null>;
  relations: SupplyRelation[];
}> = ({ containerRef, relations }) => {
  const [drawing, setDrawing] = React.useState<{ width: number; height: number; railX: number; wires: DrawnRelation[] } | null>(null);

  React.useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const measure = () => {
      const root = container.getBoundingClientRect();
      const byBackend = new Map<AgentBackend, SupplyRelation[]>();
      for (const relation of relations) byBackend.set(relation.backend, [...(byBackend.get(relation.backend) ?? []), relation]);
      const wires: DrawnRelation[] = [];
      for (const relation of relations) {
        const source = Array.from(container.querySelectorAll<HTMLElement>('[data-source-id]')).find((element) => element.dataset.sourceId === relation.sourceId);
        const backend = Array.from(container.querySelectorAll<HTMLElement>('[data-agent-backend]')).find((element) => element.dataset.agentBackend === relation.backend);
        if (!source || !backend) continue;
        const sourceBounds = source.getBoundingClientRect();
        const backendBounds = backend.getBoundingClientRect();
        const siblings = byBackend.get(relation.backend) ?? [];
        const backendIndex = siblings.findIndex((candidate) => candidate.sourceId === relation.sourceId);
        const startX = sourceBounds.right - root.left;
        const startY = sourceBounds.top - root.top + sourceBounds.height / 2;
        const endX = backendBounds.left - root.left;
        const endY = backendBounds.top - root.top + backendBounds.height * ((backendIndex + 1) / (siblings.length + 1));
        const railX = startX + (endX - startX) / 2;
        wires.push({ ...relation, startX, startY, endX, endY, path: `M ${startX} ${startY} C ${railX} ${startY}, ${railX} ${endY}, ${endX} ${endY}` });
      }
      const railX = wires.length > 0 ? wires.reduce((sum, wire) => sum + (wire.startX + wire.endX) / 2, 0) / wires.length : 0;
      setDrawing({ width: root.width, height: root.height, railX, wires });
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

  if (!drawing || drawing.wires.length === 0) return null;
  const yValues = drawing.wires.flatMap((wire) => [wire.startY, wire.endY]);
  return (
    <svg aria-hidden="true" className="pointer-events-none absolute inset-0 z-10 hidden size-full overflow-hidden lg:block" viewBox={`0 0 ${drawing.width} ${drawing.height}`} preserveAspectRatio="none">
      <line className="model-hub-rail-line" x1={drawing.railX} x2={drawing.railX} y1={Math.min(...yValues)} y2={Math.max(...yValues)} />
      {drawing.wires.map((wire) => (
        <g key={`${wire.sourceId}:${wire.backend}`}>
          <path className={`model-hub-wire model-hub-wire--${wire.kind}`} d={wire.path} />
          <circle className={`model-hub-wire-node model-hub-wire-node--${wire.kind}`} cx={wire.startX} cy={wire.startY} />
          <circle className={`model-hub-wire-node model-hub-wire-node--${wire.kind}`} cx={wire.endX} cy={wire.endY} />
        </g>
      ))}
    </svg>
  );
};

const LEGEND_ORDER: SupplyRelationKind[] = ['native', 'gateway', 'connected_unused', 'takeover', 'unavailable'];
const LEGEND_COPY: Record<SupplyRelationKind, string> = {
  native: 'native',
  gateway: 'viaGateway',
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
