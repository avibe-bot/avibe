import { useCallback } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { resolvePath, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { Navigator } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { RouteSurfaceActivityBoundary } from '@/components/RouteSurfaceActivityBoundary';
import { Dialog, DialogSurfaceContent, DialogTitle } from '@/components/ui/dialog';
import {
  closeSettingsOverlay,
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
        <Dialog
          open
          modal={false}
          onOpenChange={(open) => {
            if (!open) closeSettingsOverlay(navigate, origin);
          }}
        >
          <SettingsOverlayOriginContext.Provider value={origin}>
            <DialogSurfaceContent
              data-settings-overlay="true"
              aria-describedby={undefined}
              onInteractOutside={(event) => {
                const target = event.target;
                if (
                  target instanceof Element
                  && target.closest('[data-settings-toggle="true"]')
                ) {
                  event.preventDefault();
                }
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                window.requestAnimationFrame(() => {
                  document.querySelector<HTMLElement>('[data-settings-toggle="true"]')?.focus();
                });
              }}
            >
              <DialogTitle className="sr-only">{t('nav.settings')}</DialogTitle>
              <Routes location={location}>
                {children}
                <Route path="*" element={fallbackElement} />
              </Routes>
            </DialogSurfaceContent>
          </SettingsOverlayOriginContext.Provider>
        </Dialog>
      ) : null}
    </>
  );
};
