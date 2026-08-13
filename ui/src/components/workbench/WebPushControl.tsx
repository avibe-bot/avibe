import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell, BellOff, Loader2, Smartphone } from 'lucide-react';

import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import {
  disableWebPush,
  enableWebPush,
  getWebPushDeviceId,
  getRememberedWebPushEndpoints,
  getExistingWebPushSubscription,
  getWebPushSupportState,
  rememberWebPushEndpoint,
  type WebPushSupportState,
} from '@/lib/webPush';

type Status = 'checking' | 'unsupported' | 'needs_install' | 'disabled' | 'enabled';

export const WebPushControl: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [status, setStatus] = useState<Status>('checking');
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [support, setSupport] = useState<WebPushSupportState | null>(null);

  const refresh = async () => {
    const nextSupport = getWebPushSupportState();
    setSupport(nextSupport);
    if (!nextSupport.supported) {
      setStatus(nextSupport.reason === 'ios_requires_standalone' ? 'needs_install' : 'unsupported');
      return;
    }
    const existing = await getExistingWebPushSubscription();
    const serverStatus = await api
      .getWebPushStatus(
        existing
          ? {
              endpoint: existing.endpoint,
              subscription: existing.toJSON(),
              device_id: getWebPushDeviceId(),
              previous_endpoints: getRememberedWebPushEndpoints(),
            }
          : undefined,
      )
      .catch(() => null);
    if (existing && serverStatus?.current_subscription_enabled) {
      rememberWebPushEndpoint(existing.endpoint);
    }
    setStatus(existing && serverStatus?.current_subscription_enabled ? 'enabled' : 'disabled');
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onEnable = async () => {
    setBusy(true);
    try {
      await enableWebPush(api);
      await refresh();
    } catch {
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const onDisable = async () => {
    setBusy(true);
    try {
      await disableWebPush(api);
      setStatus('disabled');
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    setTesting(true);
    try {
      const subscription = await getExistingWebPushSubscription();
      if (!subscription?.endpoint) {
        showToast(t('workbench.inbox.notifications.testFailed', { count: 0 }), 'error');
        await refresh();
        return;
      }
      const result = await api.sendWebPushTest({
        title: t('workbench.inbox.notifications.testTitle'),
        body: t('workbench.inbox.notifications.testBody'),
        url: '/inbox',
        endpoint: subscription.endpoint,
      });
      if (result.ok) {
        showToast(t('workbench.inbox.notifications.testSent'), 'success');
      } else {
        showToast(
          t('workbench.inbox.notifications.testFailed', { count: result.failed ?? 0 }),
          'error',
        );
      }
    } catch {
      showToast(t('workbench.inbox.notifications.testFailed', { count: 0 }), 'error');
    } finally {
      setTesting(false);
    }
  };

  if (status === 'checking') {
    return (
      <Badge variant="secondary" className="h-8 rounded-lg px-3">
        <Loader2 className="size-3 animate-spin" />
        {t('workbench.inbox.notifications.checking')}
      </Badge>
    );
  }

  if (status === 'unsupported') {
    return (
      <Badge variant="secondary" className="h-8 rounded-lg px-3">
        <BellOff className="size-3" />
        {t('workbench.inbox.notifications.unsupported')}
      </Badge>
    );
  }

  if (status === 'needs_install') {
    return (
      <Badge variant="warning" className="h-8 rounded-lg px-3">
        <Smartphone className="size-3" />
        {t('workbench.inbox.notifications.installFirst')}
      </Badge>
    );
  }

  if (status === 'enabled') {
    return (
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge variant="success" className="h-8 min-w-0 rounded-lg px-3">
          <Bell className="size-3" />
          <span className="truncate">
            {support?.supported && support.requiresStandalone
              ? t('workbench.inbox.notifications.enabledPwa')
              : t('workbench.inbox.notifications.enabled')}
          </span>
        </Badge>
        <Button
          type="button"
          variant="secondary"
          size="xs"
          onClick={onTest}
          disabled={testing || busy}
          className="shrink-0"
        >
          {testing ? <Loader2 className="animate-spin" /> : <Bell className="size-3.5" />}
          {t('workbench.inbox.notifications.test')}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={onDisable}
          disabled={busy || testing}
          className="shrink-0"
        >
          {busy ? <Loader2 className="animate-spin" /> : <BellOff className="size-3.5" />}
          {t('workbench.inbox.notifications.disable')}
        </Button>
      </div>
    );
  }

  return (
    <Button type="button" variant="outline-cyan" size="xs" onClick={onEnable} disabled={busy}>
      {busy ? <Loader2 className="animate-spin" /> : <Bell className="size-3.5" />}
      {t('workbench.inbox.notifications.enable')}
    </Button>
  );
};
