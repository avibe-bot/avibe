import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell, BellOff, Loader2, Smartphone } from 'lucide-react';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useToast } from '@/context/ToastContext';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import {
  disableWebPush,
  enableWebPush,
  getExistingWebPushSubscription,
  reconcileWebPush,
  type WebPushReconcileStatus,
  type WebPushSupportState,
} from '@/lib/webPush';
import { hasNativeCapability, nativePushStatus } from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';

type Status = 'checking' | WebPushReconcileStatus;
type DisableFeedback = 'not_saved';

/** How long to wait before each further attempt to register a waiting native subscription. */
const REGISTRATION_RETRY_DELAYS_MS = [2_000, 4_000, 8_000, 16_000, 30_000];

/** The instance has no enabled subscription for the endpoint a test was sent to. */
function isNoSubscription(error: unknown): boolean {
  const { status, code } = (error ?? {}) as { status?: unknown; code?: unknown };
  return status === 404 && code === 'no_subscription';
}

export const WebPushControl: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { showToast } = useToast();
  const [status, setStatus] = useState<Status>('checking');
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [support, setSupport] = useState<WebPushSupportState | null>(null);
  const [disableFeedback, setDisableFeedback] = useState<DisableFeedback | null>(null);
  const canRegister = capabilities.can_read_instance;
  // Attempts made since this device's subscription began waiting for the instance.
  const [registrationAttempt, setRegistrationAttempt] = useState(0);

  const refresh = useCallback(async () => {
    const result = await reconcileWebPush(api);
    setSupport(result.support);
    setStatus(result.status);
    if (result.status !== 'registering') setRegistrationAttempt(0);
    return result;
  }, [api]);

  useEffect(() => {
    if (!canRegister) return;
    void refresh();
    // eslint-disable-next-line react-hooks-js/exhaustive-deps
  }, [canRegister]);

  // In the App, the enable flow can be interrupted by its own permission prompt
  // or by leaving the App, and the foreground reconcile (WebPushReconciler) is
  // what finishes it; read the result again when the page comes back, so this
  // control does not keep showing a state that reconcile has since moved past.
  useEffect(() => {
    if (!canRegister || !hasNativeCapability('push')) return;
    return onPageReactivated(() => {
      void refresh();
    });
  }, [canRegister, refresh]);

  // While the instance has yet to store this device's native subscription, keep
  // registering it here rather than leaving it to the next foreground: each
  // reconcile posts the waiting subscription again.
  useEffect(() => {
    if (status !== 'registering' || busy) return;
    const delay = REGISTRATION_RETRY_DELAYS_MS[
      Math.min(registrationAttempt, REGISTRATION_RETRY_DELAYS_MS.length - 1)
    ];
    const timer = window.setTimeout(() => {
      void refresh().then((result) => {
        if (result.status === 'registering') setRegistrationAttempt((attempt) => attempt + 1);
      });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [status, busy, registrationAttempt, refresh]);

  const onEnable = async () => {
    setBusy(true);
    // The native flow (permission, relay, then the instance) can take a while;
    // say what it is doing. Only the instance accepting it turns this to enabled.
    if (hasNativeCapability('push')) setStatus('registering');
    try {
      await enableWebPush(api);
      setDisableFeedback(null);
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
      const result = await disableWebPush(api);
      setStatus(
        result.status === 'disabled'
          ? 'disabled'
          : result.status === 'pending'
            ? 'disable_pending'
            : 'disable_failed',
      );
      setDisableFeedback(null);
      if (result.status === 'pending') {
        showToast(t('workbench.inbox.notifications.disablePending'), 'warning');
      } else if (result.status === 'failed') {
        showToast(t('workbench.inbox.notifications.disableFailed'), 'error');
      }
    } catch {
      setDisableFeedback('not_saved');
      showToast(t('workbench.inbox.notifications.disableNotSaved'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const onTest = async () => {
    setTesting(true);
    try {
      const subscription = await getExistingWebPushSubscription();
      const nativeStatus = hasNativeCapability('push')
        ? await api
          .getWebPushVapidPublicKey()
          .then(({ public_key }) => nativePushStatus(public_key))
        : null;
      const endpoint = subscription?.endpoint
        ?? (nativeStatus?.state === 'on' ? nativeStatus.endpoint : null);
      if (!endpoint) {
        showToast(t('workbench.inbox.notifications.testFailed', { count: 0 }), 'error');
        await refresh();
        return;
      }
      const result = await api.sendWebPushTest({
        title: t('workbench.inbox.notifications.testTitle'),
        body: t('workbench.inbox.notifications.testBody'),
        url: '/inbox',
        endpoint,
      });
      if (result.ok) {
        const blocked = result.normal_delivery;
        if (blocked && blocked.authorized === false) {
          // The channel works, but the normal-path authorization gate would
          // currently skip this owner — explain which gate decided.
          showToast(
            `${t('workbench.inbox.notifications.testSent')} — ${t(
              'workbench.inbox.notifications.normalBlocked',
              {
                disposition: blocked.disposition ?? 'unknown',
                policy: blocked.policy ?? 'unknown',
              },
            )}`,
            'warning',
          );
        } else {
          showToast(t('workbench.inbox.notifications.testSent'), 'success');
        }
      } else {
        showToast(
          t('workbench.inbox.notifications.testFailed', { count: result.failed ?? 0 }),
          'error',
        );
      }
    } catch (error) {
      if (hasNativeCapability('push') && isNoSubscription(error)) {
        // The App's subscription has not reached the instance (yet): register it
        // again, and say so instead of reporting a failed send.
        showToast(t('workbench.inbox.notifications.testRegistering'), 'warning');
        setStatus('registering');
        return;
      }
      showToast(t('workbench.inbox.notifications.testFailed', { count: 0 }), 'error');
    } finally {
      setTesting(false);
    }
  };

  if (!canRegister) {
    return (
      <Badge variant="secondary" className="h-8 rounded-lg px-3">
        <BellOff className="size-3" />
        {t('workbench.inbox.notifications.localOnly')}
      </Badge>
    );
  }

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

  if (status === 'registering') {
    return (
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge variant="secondary" className="h-8 min-w-0 rounded-lg px-3">
          <Loader2 className="size-3 animate-spin" />
          <span className="truncate">{t('workbench.inbox.notifications.registering')}</span>
        </Badge>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={onDisable}
          disabled={busy}
          className="shrink-0"
        >
          <BellOff className="size-3.5" />
          {t('workbench.inbox.notifications.disable')}
        </Button>
      </div>
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
        {disableFeedback && (
          <span role="status" className="text-[10px] leading-tight text-muted">
            {t('workbench.inbox.notifications.disableNotSaved')}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <Button type="button" variant="outline-cyan" size="xs" onClick={onEnable} disabled={busy}>
        {busy ? <Loader2 className="animate-spin" /> : <Bell className="size-3.5" />}
        {t('workbench.inbox.notifications.enable')}
      </Button>
      {(status === 'disable_pending' || status === 'disable_failed') && (
        <>
          <Button type="button" variant="secondary" size="xs" onClick={onDisable} disabled={busy}>
            {busy ? <Loader2 className="animate-spin" /> : <BellOff className="size-3.5" />}
            {t('common.retry')}
          </Button>
          <span role="status" className="text-[10px] leading-tight text-muted">
            {t(
              status === 'disable_pending'
                ? 'workbench.inbox.notifications.disablePending'
                : 'workbench.inbox.notifications.disableFailed',
            )}
          </span>
        </>
      )}
    </div>
  );
};
