import { ChevronRight, RefreshCw, SlidersHorizontal } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendIcon } from '../visual';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { Card } from '../ui/card';
import { getBackendUiMeta, type BuiltinBackend } from '@/lib/agentBackends';
import type { HubSupplyBlock } from '../settings/models/featureFlags';
import type { ModelCandidate } from '../settings/models/types';
import { RouteChoice, type AssistantRouteView } from './AssistantRow';
import { INLINE_CANDIDATES, inlineCandidates, type BuiltinModelOffer } from './builtinModelOffer';
import type { BuiltinChoiceFailure } from './useBuiltinModelChoice';

export interface BuiltinAssistantRowProps {
  backend: BuiltinBackend;
  route?: AssistantRouteView;
  /** Why the Model Hub cannot supply this backend now, as the server states it. */
  block?: HubSupplyBlock;
  /** Read only while the backend's own Agent has no model. */
  offer?: BuiltinModelOffer;
  /** The candidate whose pick is being written, if any. */
  picking?: string | null;
  /** Why the last choice did not stand, if it did not. */
  failure?: BuiltinChoiceFailure | null;
  onRetryChoice?: () => void;
  connectionPending?: boolean;
  connectionError?: string;
  routeError?: string;
  configuringDisabled?: boolean;
  onConfigure: () => void;
  onPick: (candidate: ModelCandidate) => void;
  onAllModels: () => void;
  onRetryOffer?: () => void;
  onRefreshConnection?: () => void;
  onRetryRoute?: () => void;
}

/**
 * A built-in assistant, drawn in the same card frame as the CLI assistants beside it.
 *
 * It is part of the platform: there is nothing to install and no switch, so the state
 * row says so instead of showing a lifecycle. What it needs is a model. With one, the
 * card reads as an enabled assistant does — its route and the model it resolves to.
 * Without one, the card offers the models it could run on right now and writes the
 * pick, so setup can finish on this assistant alone (D3).
 */
export function BuiltinAssistantRow({ backend, route, block, offer, picking = null, failure = null, onRetryChoice,
  connectionPending, connectionError, routeError, configuringDisabled = false, onConfigure, onPick, onAllModels,
  onRetryOffer, onRefreshConnection, onRetryRoute }: BuiltinAssistantRowProps) {
  const { t } = useTranslation();
  const label = getBackendUiMeta(backend).label;
  // A choice that did not stand keeps the offer open whatever the Agent reads as now:
  // the server may have filled it with a model nobody chose.
  const unconfirmed = failure?.kind === 'failed';
  const unset = !block && (route?.kind === 'no-agent-model' || unconfirmed);
  const routeModel = route?.kind === 'route' ? route.model : null;
  const routeUnknown = route?.kind === 'pending' || route?.kind === 'failed';
  const note = block === 'gateway_off' ? t('onboarding.setup.noteGatewayOff', { name: label })
    : block === 'hub_disabled' ? t('onboarding.setup.noteHubDisabled', { name: label })
      : unset ? t('onboarding.setup.noteBuiltinUnset')
        : route?.kind === 'route' && !routeModel ? t('onboarding.setup.noteNoModels')
          : t('onboarding.setup.noteEnabled');
  const busy = !!picking || !!connectionPending;
  const candidates = offer?.kind === 'ready' ? inlineCandidates(offer.read) : [];
  return (
    <Card className="onboarding-assistant" data-backend={backend} aria-label={label}>
      <div className="onboarding-card-identity">
        <span className="onboarding-card-logo"><BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" /></span>
        <h3 className="onboarding-card-name">{label}</h3>
      </div>
      <div className="onboarding-assistant-state">
        <Badge variant="success" className="font-medium tracking-normal">
          <span className="size-2 rounded-full bg-mint" />{t('onboarding.setup.builtinAlwaysOn')}
        </Badge>
      </div>
      <div className="onboarding-assistant-body">
        <p className="onboarding-assistant-note" data-tone={block ? 'error' : undefined}>{note}</p>
        <div className="onboarding-assistant-actions">
          {unset ? (
            <div className="onboarding-model-offer" role="group" aria-label={t('onboarding.setup.pickModelNamed', { name: label })}
              aria-busy={offer?.kind === 'loading' || !!picking}>
              {offer?.kind === 'failed' ? (
                <p className="onboarding-model-offer-empty">
                  {t('onboarding.setup.modelsUnreadable')}
                  {onRetryOffer && <Button variant="link" size="xs" onClick={onRetryOffer}>{t('common.retry')}</Button>}
                </p>
              ) : offer?.kind === 'ready' ? (
                candidates.length === 0
                  ? <p className="onboarding-model-offer-empty">{t('onboarding.setup.noModelsToPick')}</p>
                  : candidates.map((candidate) => (
                    <button key={candidate.id} type="button" className="onboarding-model-offer-row"
                      disabled={busy || configuringDisabled} onClick={() => onPick(candidate)}>
                      <span className="onboarding-model-offer-id">{candidate.display_name?.trim() || candidate.id}</span>
                      {picking === candidate.id
                        ? <RefreshCw size={14} className="motion-safe:animate-spin" aria-hidden="true" />
                        : <span className="onboarding-model-offer-supplier">{candidate.suppliers[0].source_name}</span>}
                    </button>
                  ))
              ) : (
                // Pending keeps the rows' box, so the card does not grow when the read lands.
                Array.from({ length: INLINE_CANDIDATES }, (_, index) => (
                  <span key={index} className="onboarding-model-offer-row" aria-hidden="true">
                    <strong className="onboarding-model-choice-pending" />
                  </span>
                ))
              )}
              <button type="button" className="onboarding-model-offer-row onboarding-model-offer-all"
                disabled={busy || configuringDisabled} onClick={onAllModels}>
                <span>{t('onboarding.setup.allModels')}</span>
                <ChevronRight size={15} aria-hidden="true" />
              </button>
            </div>
          ) : routeModel || routeUnknown ? (
            <RouteChoice name={label} route={route} live={!block && !routeUnknown} onOpen={onConfigure}
              pending={!!connectionPending} disabled={configuringDisabled || busy} />
          ) : route?.kind === 'route' && !block ? (
            <button type="button" className="onboarding-method-connected" onClick={onConfigure}
              disabled={configuringDisabled || busy}>
              {connectionPending ? <RefreshCw size={16} className="motion-safe:animate-spin" /> : <SlidersHorizontal size={16} />}
              {t('onboarding.setup.configureRoute')}
            </button>
          ) : null}
        </div>
        {/* A failed pick belongs to the offer it was made from. */}
        {unset && failure && <div className="onboarding-assistant-error" role="alert">
          {t(unconfirmed ? 'onboarding.setup.modelPickFailed' : 'onboarding.setup.suppliersChanged')}
          {unconfirmed && onRetryChoice && <Button variant="link" size="xs" disabled={busy} onClick={onRetryChoice}>{t('common.retry')}</Button>}
        </div>}
        {connectionError && <div className="onboarding-assistant-error" role="alert">
          {connectionError}
          {onRefreshConnection && <Button variant="link" size="xs" onClick={onRefreshConnection}>{t('common.retry')}</Button>}
        </div>}
        {routeError && <div className="onboarding-assistant-error" role="alert">
          {routeError}
          {onRetryRoute && <Button variant="link" size="xs" onClick={onRetryRoute}>{t('common.retry')}</Button>}
        </div>}
      </div>
    </Card>
  );
}
