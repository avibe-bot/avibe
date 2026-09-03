import * as React from 'react';
import { Info } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { GuardGapList } from './GuardGapList';
import type { RouteHopRef, SupplyGap } from './types';

/**
 * What a guarded mutation would take with it.
 *
 * The two arrays travel together because the guard reports them together, the
 * confirmation shows both, and the forced retry echoes both: a caller holding
 * them apart can echo one without the other, which claims a confirmation the
 * user was never shown. Named here, beside the body that renders them, so a
 * surface that has to CARRY a plan before showing it uses the same shape.
 */
export type GuardPlan = { hops: RouteHopRef[]; gaps: SupplyGap[] };

/** The shared evidence body for every guarded Model Hub mutation and its result. */
export const GuardImpact: React.FC<GuardPlan & { committed?: boolean }> = ({ hops, gaps, committed = false }) => {
  const { t } = useTranslation();
  return (
    <>
      {hops.length > 0 && (
        <>
          <div className="model-hub-guard-label">
            <p>{t(committed ? 'settings.models.guard.result.label' : 'settings.models.guard.label')}</p>
            <span>{t('settings.models.guard.count', { count: hops.length })}</span>
          </div>
          <div className="model-hub-guard-list">
            {hops.map((hop) => (
              <div
                key={`${hop.backend}:${hop.menu_model}:${hop.position}:${hop.source_id}:${hop.model_id}`}
                className="model-hub-guard-hop"
              >
                <span className="min-w-0 flex-1">
                  <strong>
                    {t(`settings.models.backends.${hop.backend}`, { defaultValue: hop.backend })} · {hop.menu_model}
                  </strong>
                  <span>{hop.model_id} · {t('settings.models.guard.hop.position', { n: hop.position })}</span>
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      <GuardGapList
        gaps={gaps}
        labelKey={committed ? 'settings.models.guard.result.gapLabel' : undefined}
      />
      <p className={cn('model-hub-guard-hint', gaps.length > 0 && 'text-destructive-ink')}>
        <Info aria-hidden />
        {t(`settings.models.guard.${committed ? 'result.' : ''}hint.${gaps.length > 0 ? 'interrupt' : 'safe'}`)}
      </p>
    </>
  );
};
