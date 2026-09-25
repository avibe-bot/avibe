import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { SETTINGS_LANDING_PATH } from '@/lib/adminNavigation';
import { DESKTOP_OPEN_SETTINGS_EVENT, isDesktopShell } from '@/lib/desktopShell';

/**
 * Answers the desktop shell's Settings… menu item. It navigates like the
 * sidebar's own Settings control, so the Settings overlay boundary it renders
 * under records the covered surface and leaving Settings returns to it. Mount it
 * inside `SettingsOverlayNavigationBoundary`.
 */
export const DesktopSettingsCommand = () => {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  useEffect(() => {
    if (!isDesktopShell()) return undefined;
    const open = (event: Event) => {
      event.preventDefault();
      if (pathname !== SETTINGS_LANDING_PATH) navigate(SETTINGS_LANDING_PATH);
    };
    window.addEventListener(DESKTOP_OPEN_SETTINGS_EVENT, open);
    return () => window.removeEventListener(DESKTOP_OPEN_SETTINGS_EVENT, open);
  }, [navigate, pathname]);

  return null;
};
