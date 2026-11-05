import { useCallback, useEffect, useState } from 'react';

import { useApi } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import { isDesktopShell } from '../../lib/desktopShell';
import type { SetupItem } from '../../lib/homePreferences';
import { isNativeShell } from '../../lib/native';
import { getEnabledPlatforms } from '../../lib/platforms';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { useIsDesktop } from '../../lib/useIsDesktop';
import { enableWebPush, readWebPushDeviceState } from '../../lib/webPush';
import { readComputerUseStatus } from '../workbench/computerUseStatus';

/** What the home has observed about each fact; `null` is "not known", never "missing". */
type Facts = {
  notifications: 'on' | 'off' | 'unavailable' | null;
  computerUseReady: boolean | null;
  chatAppsConnected: boolean | null;
  phonePaired: boolean | null;
};

const UNKNOWN: Facts = { notifications: null, computerUseReady: null, chatAppsConnected: null, phonePaired: null };

export interface SetupGaps {
  /** The setup still missing, in the order Getting started lists it. Only what was observed to be
   *  missing: a fact not read yet, or unreadable, is not a gap. */
  pending: SetupItem[];
  /** An IM platform is enabled; `null` until the config has been read. */
  chatAppsConnected: boolean | null;
  /** Turn on this device's notifications in place, then read again. */
  enableNotifications: () => Promise<void>;
}

/**
 * The readiness facts Getting started reads (ui/design: home/home/*), each from where the UI
 * already observes it, and each item fixed where it says:
 * - `model`: the main Bot's backend reports no model (`noModel`, read by the home) → Settings · Models;
 * - `notifications`: this device's Web Push is off where it could be on → turned on here;
 * - `computer_use`: the desktop app's Computer Use is not ready → its guide, Settings · Computer Use;
 * - `chat_apps`: no IM platform is enabled in the config → Settings · Platforms;
 * - `phone`: Avibe Cloud remote access is not paired → Settings · Remote access.
 * Everything but notifications is the instance's, so it is the Owner's to fix; a phone does not
 * offer to continue on a phone. The desktop app's own notification switch is not observable from
 * the page, so in the app only Web Push speaks for notifications. Read when shown, again when the
 * home is uncovered (the way back from the place it was fixed) and when the window regains focus.
 */
export function useSetupGaps({ owner, noModel }: { owner: boolean; noModel: boolean }): SetupGaps {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const desktop = useIsDesktop();
  const { capabilities } = useInstanceAuthorization();
  const canPush = capabilities.can_read_instance && !isNativeShell();
  const [facts, setFacts] = useState<Facts>(UNKNOWN);

  const read = useCallback(() => {
    const learn = (patch: Partial<Facts>) => setFacts((current) => ({ ...current, ...patch }));
    if (canPush) void readWebPushDeviceState(api).then((notifications) => learn({ notifications }));
    if (!owner) return;
    if (isDesktopShell()) {
      void readComputerUseStatus().then((read) => {
        if (read) learn({ computerUseReady: read.kind === 'status' ? read.status.status === 'ready' : null });
      });
    }
    void api.getConfig({ cache: false })
      .then((config) => learn({ chatAppsConnected: getEnabledPlatforms(config).length > 0 }))
      .catch(() => {});
    void api.remoteAccessStatus()
      .then((status) => {
        if (status?.ok !== false && typeof status?.paired === 'boolean') learn({ phonePaired: status.paired });
      })
      .catch(() => {});
  }, [api, canPush, owner]);

  useEffect(() => {
    if (!surfaceActive) return;
    queueMicrotask(read);
    window.addEventListener('focus', read);
    return () => window.removeEventListener('focus', read);
  }, [read, surfaceActive]);

  const enableNotifications = useCallback(async () => {
    if (!canPush) return;
    try {
      await enableWebPush(api);
    } catch {
      // The browser's prompt was refused or failed; the read below says where it stands.
    }
    const notifications = await readWebPushDeviceState(api);
    setFacts((current) => ({ ...current, notifications }));
  }, [api, canPush]);

  const pending: SetupItem[] = [];
  if (owner && noModel) pending.push('model');
  if (facts.notifications === 'off') pending.push('notifications');
  if (owner && facts.computerUseReady === false) pending.push('computer_use');
  if (owner && facts.chatAppsConnected === false) pending.push('chat_apps');
  if (owner && desktop && facts.phonePaired === false) pending.push('phone');
  return { pending, chatAppsConnected: owner ? facts.chatAppsConnected : null, enableNotifications };
}
