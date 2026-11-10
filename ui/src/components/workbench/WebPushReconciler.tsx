import { useCallback, useEffect, type FC } from 'react';

import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useNotificationPreferences } from '@/context/NotificationPreferencesContext';
import { hasNativeCapability } from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';
import { reconcileWebPush } from '@/lib/webPush';

/** Keeps an enabled native subscription registered without requiring Settings to be open. */
export const WebPushReconciler: FC = () => {
  const api = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { preferences, loaded } = useNotificationPreferences();
  const canReconcile = capabilities.can_read_instance
    && loaded
    && preferences.enabled
    && hasNativeCapability('push');

  const reconcile = useCallback(() => {
    if (!canReconcile) return;
    void reconcileWebPush(api);
  }, [api, canReconcile]);

  useEffect(() => {
    reconcile();
    return onPageReactivated(reconcile);
  }, [reconcile]);

  return null;
};
