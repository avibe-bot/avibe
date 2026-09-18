import { useCallback } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { resolvePath, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { Navigator } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { RouteSurfaceActivityBoundary } from '@/components/RouteSurfaceActivityBoundary';
import { Dialog, DialogSurfaceContent, DialogTitle } from '@/components/ui/dialog';
import { useSetupHandoffDeparture } from '@/components/workbench/backendReadiness';
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
  // The route this surface renders behind any overlay — the retained origin
  // while Settings is open, the foreground route otherwise.
  const backgroundLocation = settingsSurfaceOpen ? origin.location : location;
  // Which route is rendered here is also the only thing that can tell a real
  // departure from the home apart from the route guard unmounting and remounting
  // it in place, so the setup handoff reads it from here rather than guessing
  // from a component's lifecycle.
  useSetupHandoffDeparture(backgroundLocation.pathname);
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
          <Routes location={backgroundLocation}>
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
              // The overlay covers the work area and stops at the shell sidebar,
              // which is what keeps the origin project/session visible behind it.
              // This overrides the primitive's historical 240 default with the
              // sidebar's own live width: the dialog is portaled outside the
              // shell's subtree, so that document-level custom property is what
              // keeps the two edges together while the sidebar is being dragged.
              //
              // Below md the surface is the whole viewport, so the primitive's
              // left border would draw a hairline down the screen edge and make
              // Settings-from-home look different from a direct Settings link.
              // There is no sidebar to divide from until the offset applies.
              className="border-l-0 md:left-[var(--app-sidebar-w)] md:border-l"
              aria-describedby={undefined}
              onInteractOutside={(event) => {
                const target = event.target;
                if (
                  target instanceof Element
                  // The toggle closes the overlay itself, and the sidebar's
                  // resize edge is not a dismissal at all: it moves this
                  // surface's own left edge, so grabbing it must not close what
                  // the drag is laying out.
                  && target.closest('[data-settings-toggle="true"], [data-sidebar-resizer="true"]')
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
