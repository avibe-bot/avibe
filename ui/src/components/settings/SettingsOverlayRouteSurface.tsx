import type { ReactElement, ReactNode } from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';

import { useIsDesktop } from '@/lib/useIsDesktop';
import { isSettingsRoutePath, useSettingsOverlayOrigin } from '@/lib/settingsOverlay';

type SettingsOverlayRouteSurfaceProps = {
  children: ReactNode;
  fallbackElement: ReactElement;
  settingsRoute: ReactElement;
};

export const SettingsOverlayRouteSurface = ({
  children,
  fallbackElement,
  settingsRoute,
}: SettingsOverlayRouteSurfaceProps) => {
  const location = useLocation();
  const isDesktop = useIsDesktop();
  const origin = useSettingsOverlayOrigin(location);
  const overlayOpen = isDesktop && isSettingsRoutePath(location.pathname) && origin !== null;

  return (
    <>
      <div
        className="contents"
        aria-hidden={overlayOpen || undefined}
        inert={overlayOpen || undefined}
      >
        <Routes location={overlayOpen ? origin.location : location}>
          {children}
          <Route path="*" element={fallbackElement} />
        </Routes>
      </div>
      {overlayOpen ? (
        <div
          data-settings-overlay="true"
          className="fixed inset-y-0 left-[240px] right-0 z-30 overflow-hidden border-l border-border bg-background shadow-2xl"
        >
          <Routes location={location}>
            {settingsRoute}
            <Route path="*" element={fallbackElement} />
          </Routes>
        </div>
      ) : null}
    </>
  );
};
