import type { ReactNode } from 'react';
import { Check, Download, ExternalLink, RefreshCw, Sliders } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { BackendIcon } from '../visual';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { Card } from '../ui/card';
import { getBackendUiMeta } from '@/lib/agentBackends';
import type { AssistantId } from './collaborationTimeline';

export interface AssistantRowProps {
  backend: AssistantId;
  status: 'unknown' | 'ok' | 'missing';
  installing: boolean;
  detecting: boolean;
  error?: { message: string; output?: string | null };
  lifecycle: ReactNode;
  enabledControl: ReactNode;
  onInstall: () => void;
  onDetect: () => void;
  onConfigure: () => void;
  onAddKey?: () => void;
  onRefreshConnection?: () => void;
  connectionPending?: boolean;
  connectionError?: string;
  configuringDisabled?: boolean;
  /** Presentation only. The connection owner must supply confirmed state. */
  connection?: 'subscription' | 'api_key' | 'hub';
}

/**
 * One assistant, drawn as the card the welcome already showed it in.
 *
 * The frame, the column it sits in and the identity header at its top are the story
 * card's, from the same classes; only the body under them changes. The reference says
 * it directly — "the assistant logo and name stay at the top in both states; connection
 * state replaces roles with enable switches" — so this card reuses that header and puts
 * its switch in the slot the story fills with a role. Moving from the intro to the
 * connection therefore reads as the cards taking on work rather than as one screen
 * being swapped for another. `geometry.spec.ts` holds both steps to the same boxes,
 * which is what keeps that true as either step changes.
 */
export function AssistantRow({ backend, status, installing, detecting, error, lifecycle, enabledControl,
  onInstall, onDetect, onConfigure, configuringDisabled = false, connection, onAddKey, onRefreshConnection, connectionPending, connectionError }: AssistantRowProps) {
  const { t } = useTranslation();
  const label = getBackendUiMeta(backend).label;
  return (
    <Card className="onboarding-assistant" aria-label={label}>
      <div className="onboarding-card-identity">
        <BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" />
        {/* A heading here and a `strong` on the welcome: the setup's three cards are
            a real document structure a person navigates, the welcome's are an
            illustration the card already labels. Same class, so same geometry. */}
        <h3 className="onboarding-card-name">{label}</h3>
        <div className="onboarding-assistant-enable">{enabledControl}</div>
      </div>
      <div className="onboarding-assistant-state">
        {installing || detecting || status === 'unknown' ? (
          <Badge variant="secondary"><RefreshCw size={12} className={installing || detecting ? 'motion-safe:animate-spin' : ''} />
            {t(installing ? 'agentDetection.installing' : detecting ? 'onboarding.welcome.detecting' : 'common.notChecked')}
          </Badge>
        ) : status === 'ok' ? lifecycle : (
          <Badge variant={error ? 'destructive' : 'secondary'}><span className="size-2 rounded-full bg-current" />
            {t(error ? 'onboarding.setup.installFailed' : 'onboarding.setup.notInstalled')}
          </Badge>
        )}
      </div>
      <div className="onboarding-assistant-body">
        <p className="onboarding-assistant-note" title={t(`onboarding.setup.${backend}Description`)}>
          {connection === 'hub' ? t('settings.backends.nativeAuthHubOwned')
            : t(connection ? `onboarding.setup.${connection}` : `onboarding.setup.${backend}Description`)}
        </p>
        <div className="onboarding-assistant-actions">
          {status === 'missing' && <Button variant="secondary" size="sm" className="h-[34px]" onClick={onInstall} disabled={installing || detecting}>
            {installing ? <RefreshCw size={14} className="motion-safe:animate-spin" /> : <Download size={14} />}
            {t(installing ? 'agentDetection.installing' : error ? 'common.retry' : 'onboarding.setup.install')}
          </Button>}
          {status === 'unknown' && !detecting && <Button variant="secondary" size="sm" onClick={onDetect}><RefreshCw size={14} />{t('common.retry')}</Button>}
          <Button variant="secondary" size="sm" className="h-[34px]" onClick={onConfigure} disabled={configuringDisabled || installing || detecting}>
            {connection === 'hub' ? <ExternalLink size={14} /> : connection ? <Check size={14} className="text-mint-ink" /> : <Sliders size={14} />}
            {connection === 'hub' ? t('settings.backends.openModelHub')
              : t(connection ? `onboarding.setup.${connection}Connected` : 'onboarding.connection.addSubscription')}
          </Button>
          {!connection && onAddKey && <Button variant="secondary" size="sm" className="h-[34px]" onClick={onAddKey} disabled={configuringDisabled || installing || detecting}>{t('onboarding.connection.addKey')}</Button>}
        </div>
        {(connectionError || connectionPending) && <div className="onboarding-assistant-error" role={connectionError ? 'alert' : 'status'}>
          {connectionPending ? t('common.loading') : connectionError}
          {!connectionPending && onRefreshConnection && <Button variant="link" size="xs" onClick={onRefreshConnection}>{t('common.retry')}</Button>}
        </div>}
        {error && <div className="onboarding-assistant-error" role="alert">
          <p>{error.message}</p>
          {error.output && <details><summary>{t('onboarding.details')}</summary><pre>{error.output}</pre></details>}
        </div>}
      </div>
    </Card>
  );
}
