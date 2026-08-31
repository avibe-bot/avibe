import { useCallback } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { resolvePath, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { Navigator } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { RouteSurfaceActivityBoundary } from '@/components/RouteSurfaceActivityBoundary';
import {
  isSettingsEntryPath,
  locationPath,
  SettingsOverlayOriginContext,
  settingsOverlayStateForOrigin,
  useSettingsOverlayOrigin,
} from '@/lib/settingsOverlay';

type SettingsOverlayRouteSurfaceProps = {
  children: ReactNode;
  fallbackElement: ReactElement;
};

export const SettingsOverlayRouteSurface = ({
  children,
  fallbackElement,
}: SettingsOverlayRouteSurfaceProps) => {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  const settingsSurfaceOpen = isSettingsEntryPath(location.pathname) && origin !== null;
  const replaceBackground = useCallback<Navigator['replace']>((to, state) => {
    if (!origin) return;
    const path = resolvePath(to, origin.location.pathname);
    const nextOrigin = {
      ...origin,
      location: {
        ...origin.location,
        ...path,
        state,
      },
    };
    navigate(locationPath(location), {
      replace: true,
      state: settingsOverlayStateForOrigin(nextOrigin, location.state),
    });
  }, [location, navigate, origin]);

  return (
    <>
      <div
        className="contents"
        aria-hidden={settingsSurfaceOpen || undefined}
        inert={settingsSurfaceOpen || undefined}
      >
        <RouteSurfaceActivityBoundary
          active={!settingsSurfaceOpen}
          inactiveReplace={replaceBackground}
        >
          <Routes location={settingsSurfaceOpen ? origin.location : location}>
            {children}
            <Route path="*" element={fallbackElement} />
          </Routes>
        </RouteSurfaceActivityBoundary>
      </div>
      {settingsSurfaceOpen ? (
        <SettingsOverlayOriginContext.Provider value={origin}>
          <div
            role="dialog"
            aria-label={t('nav.settings')}
            aria-modal="true"
            data-settings-overlay="true"
            className="fixed inset-y-0 left-0 right-0 z-30 overflow-hidden border-l border-border bg-background shadow-2xl md:left-[240px]"
          >
            <Routes location={location}>
              {children}
              <Route path="*" element={fallbackElement} />
            </Routes>
          </div>
        </SettingsOverlayOriginContext.Provider>
      ) : null}
    </>
  );
};
