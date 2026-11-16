import { useEffect } from 'react';
import { VersionBadge } from '../components/VersionBadge';
import { DESKTOP_OPEN_UPDATES_EVENT } from '../lib/desktopShell';
import { DesktopAccountRowView } from './DesktopAccountRowView';
import { useDesktopClientProjection, usePresentDesktopClient } from './useDesktopClient';

/** Nonsecret footer. Account actions present native-owned UI. This computer's update is the shared
 *  update panel (ui/design: update/panel): on a page this computer serves, VersionBadge acts over the
 *  desktop app's IPC; on another computer's page, the version opens the trusted update view. */
export function DesktopAccountRow() {
  const state = useDesktopClientProjection();
  const present = usePresentDesktopClient();
  const remote = state?.connection.committed.kind === 'remote';
  useEffect(() => {
    const open = (event: Event) => {
      // Local Workbench retains VersionBadge's original updater event handler.
      // Remote pages have presentation URLs only, never privileged update IPC.
      if (!remote) return;
      event.preventDefault();
      window.__AVIBE_DESKTOP_UPDATES_HANDLED__ = true;
      present('updates');
    };
    window.addEventListener(DESKTOP_OPEN_UPDATES_EVENT, open);
    return () => window.removeEventListener(DESKTOP_OPEN_UPDATES_EVENT, open);
  }, [remote, present]);
  if (!state) return null;
  return <DesktopAccountRowView state={state} onPresent={present} localUpdater={<VersionBadge openUpward />} />;
}
