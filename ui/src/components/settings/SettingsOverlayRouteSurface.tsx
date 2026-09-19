import { useCallback, useEffect, useRef } from 'react';
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
  const lastFocusRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement && !event.target.closest('[data-settings-overlay]')) {
        lastFocusRef.current = event.target;
      }
    };
    document.addEventListener('focusin', onFocusIn);
    return () => document.removeEventListener('focusin', onFocusIn);
  }, []);
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
        className={settingsSurfaceOpen ? 'hidden' : 'contents'}
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
              // Settings owns the whole viewport. The retained Workbench origin
              // remains mounted behind this portal for drafts and return state,
              // but its sidebar is not part of the Settings surface at any width.
              className="left-0 border-l-0 md:left-0 md:border-l-0"
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
              onOpenAutoFocus={() => {
                // Freeze the origin focus for this visit. Retained app windows
                // may focus themselves on return, before Radix's deferred close
                // callback runs; that must not replace the initiating control.
                returnFocusRef.current = lastFocusRef.current;
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                const target = returnFocusRef.current;
                window.requestAnimationFrame(() => {
                  if (target?.isConnected && !target.closest('[inert]')) {
                    target.focus({ preventScroll: true });
                    return;
                  }
                  const fallback = Array.from(
                    document.querySelectorAll<HTMLElement>('[data-settings-toggle="true"]'),
                  ).find((candidate) => !candidate.closest('[inert]'));
                  fallback?.focus({ preventScroll: true });
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
