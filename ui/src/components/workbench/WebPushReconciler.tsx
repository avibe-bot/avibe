import { useCallback, useEffect, type FC } from 'react';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useNotificationPreferences } from '@/context/NotificationPreferencesContext';
import { hasNativeCapability } from '@/lib/native';
import { onPageReactivated, readPageActivity } from '@/lib/pageActivity';
import { reconcileWebPush, restoreWebPushAfterBinding } from '@/lib/webPush';

/**
 * Keeps this device's notifications registered without Settings being open. In the App: its
 * enabled native subscription. In a browser on the computer that runs Avibe: the subscription
 * that binding this Avibe to an account stopped is taken back, when the page is in the
 * foreground and the browser already allows notifications (`restoreWebPushAfterBinding`).
 */
export const WebPushReconciler: FC = () => {
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { preferences, loaded } = useNotificationPreferences();
  const canReconcile = capabilities.can_read_instance && loaded && preferences.enabled;

  const reconcile = useCallback(() => {
    if (!canReconcile) return;
    if (hasNativeCapability('push')) void reconcileWebPush(api);
    else void restoreWebPushAfterBinding(api);
  }, [api, canReconcile]);

  useEffect(() => {
    // The App reconciles whenever it loads. A browser page loaded in a background tab waits
    // until it is the one in front; coming to the front is a reactivation.
    if (hasNativeCapability('push') || readPageActivity()) reconcile();
    return onPageReactivated(reconcile);
  }, [reconcile]);

  return null;
};
