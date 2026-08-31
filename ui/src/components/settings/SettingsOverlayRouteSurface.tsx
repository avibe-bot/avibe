import type { ReactElement, ReactNode } from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { RouteSurfaceActivityBoundary } from '@/components/RouteSurfaceActivityBoundary';
import { useIsDesktop } from '@/lib/useIsDesktop';
import {
  isSettingsRoutePath,
  SettingsOverlayOriginContext,
  useSettingsOverlayOrigin,
} from '@/lib/settingsOverlay';

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
  const { t } = useTranslation();
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
        <RouteSurfaceActivityBoundary active={!overlayOpen}>
          <Routes location={overlayOpen ? origin.location : location}>
            {children}
            <Route path="*" element={fallbackElement} />
          </Routes>
        </RouteSurfaceActivityBoundary>
      </div>
      {overlayOpen ? (
        <SettingsOverlayOriginContext.Provider value={origin}>
          <div
            role="dialog"
            aria-label={t('nav.settings')}
            aria-modal="true"
            data-settings-overlay="true"
            className="fixed inset-y-0 left-[240px] right-0 z-30 overflow-hidden border-l border-border bg-background shadow-2xl"
          >
            <Routes location={location}>
              {settingsRoute}
              <Route path="*" element={fallbackElement} />
            </Routes>
          </div>
        </SettingsOverlayOriginContext.Provider>
      ) : null}
    </>
  );
};
