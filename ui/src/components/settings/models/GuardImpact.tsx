import * as React from 'react';
import { Info } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { GuardGapList } from './GuardGapList';
import type { RouteHopRef, SupplyGap } from './types';

/** The shared evidence body for every guarded Model Hub mutation and its result. */
export const GuardImpact: React.FC<{
  hops: RouteHopRef[];
  gaps: SupplyGap[];
  committed?: boolean;
}> = ({ hops, gaps, committed = false }) => {
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
