import { createContext } from 'react';
import type { DesktopNotificationsActions } from '../lib/desktopShell';

/** An embedding may provide preference actions; absent a provider, the control uses its shell. */
export const DesktopNotificationActionsContext = createContext<
  Pick<DesktopNotificationsActions, 'get' | 'set' | 'authorization'> | undefined
>(undefined);
