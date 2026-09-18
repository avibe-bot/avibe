import type { ReactNode } from 'react';
import { Check, Download, RefreshCw, Sliders } from 'lucide-react';
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
  connection?: 'subscription' | 'api_key';
}

export function AssistantRow({ backend, status, installing, detecting, error, lifecycle, enabledControl,
  onInstall, onDetect, onConfigure, configuringDisabled = false, connection, onAddKey, onRefreshConnection, connectionPending, connectionError }: AssistantRowProps) {
  const { t } = useTranslation();
  const label = getBackendUiMeta(backend).label;
  return (
    <Card className="onboarding-assistant" aria-label={label}>
      <div className="onboarding-assistant-main">
        <div className="onboarding-assistant-logo"><BackendIcon backend={backend} variant="brand" brandFit="mark" size={28} aria-hidden="true" /></div>
        <div className="onboarding-assistant-identity">
          <h3>{label}</h3>
          <p title={t(`onboarding.setup.${backend}Description`)}>{t(connection ? `onboarding.setup.${connection}` : `onboarding.setup.${backend}Description`)}</p>
        </div>
        <div className="onboarding-assistant-status">
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
        <div className="onboarding-assistant-actions">
          {status === 'missing' && <Button variant="secondary" size="sm" className="h-[34px]" onClick={onInstall} disabled={installing || detecting}>
            {installing ? <RefreshCw size={14} className="motion-safe:animate-spin" /> : <Download size={14} />}
            {t(installing ? 'agentDetection.installing' : error ? 'common.retry' : 'onboarding.setup.install')}
          </Button>}
          {status === 'unknown' && !detecting && <Button variant="secondary" size="sm" onClick={onDetect}><RefreshCw size={14} />{t('common.retry')}</Button>}
          <Button variant="secondary" size="sm" className="h-[34px]" onClick={onConfigure} disabled={configuringDisabled || installing || detecting}>
            {connection ? <Check size={14} className="text-mint-ink" /> : <Sliders size={14} />}
            {t(connection ? `onboarding.setup.${connection}Connected` : 'onboarding.connection.addSubscription')}
          </Button>
          {!connection && onAddKey && <Button variant="secondary" size="sm" className="h-[34px]" onClick={onAddKey} disabled={configuringDisabled || installing || detecting}>{t('onboarding.connection.addKey')}</Button>}
          {enabledControl}
        </div>
      </div>
      {(connectionError || connectionPending) && <div className="onboarding-assistant-error" role={connectionError ? 'alert' : 'status'}>
        {connectionPending ? t('common.loading') : connectionError}
        {!connectionPending && onRefreshConnection && <Button variant="link" size="xs" onClick={onRefreshConnection}>{t('common.retry')}</Button>}
      </div>}
      {error && <div className="onboarding-assistant-error" role="alert">
        <p>{error.message}</p>
        {error.output && <details><summary>{t('onboarding.details')}</summary><pre>{error.output}</pre></details>}
      </div>}
    </Card>
  );
}
