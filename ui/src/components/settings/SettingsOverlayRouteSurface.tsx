import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
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
import { useStandaloneSettingsMenu } from '@/lib/settingsMenuPlacement';

type SettingsOverlayRouteSurfaceProps = {
  children: ReactNode;
  fallbackElement: ReactElement;
};

const isForegroundFocusOwner = (element: Element | null): element is HTMLElement => {
  if (!(element instanceof HTMLElement) || !element.isConnected) return false;
  if (element.closest('[data-settings-overlay], [inert], [aria-hidden="true"]')) return false;
  // Retained modal owners can recreate their editor while Settings is open. A
  // recreated input is the owner of the return focus, but it cannot be the
  // frozen target because that DOM node did not exist when Settings opened.
  // Radix's modal DialogContent intentionally exposes role=dialog and its
  // open state, but does not emit aria-modal. AppWindow also uses role=dialog;
  // its data-window-id keeps ordinary retained windows out of this branch.
  if (element.closest(
    '[role="dialog"][aria-modal="true"]:not([data-window-id]), '
      + '[role="dialog"][data-state="open"][aria-labelledby]:not([data-window-id])',
  )) {
    return true;
  }
  return false;
};

export const SettingsOverlayRouteSurface = ({
  children,
  fallbackElement,
}: SettingsOverlayRouteSurfaceProps) => {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const origin = useSettingsOverlayOrigin(location);
  const standaloneMenu = useStandaloneSettingsMenu();
  const settingsSurfaceOpen = isSettingsEntryPath(location.pathname) && origin !== null;
  const lastFocusRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const settingsSurfaceOpenRef = useRef(settingsSurfaceOpen);
  const locationRef = useRef(location);
  const settingsVisitRef = useRef(0);
  const focusFrameRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    settingsSurfaceOpenRef.current = settingsSurfaceOpen;
    locationRef.current = location;
  }, [location, settingsSurfaceOpen]);
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof HTMLElement && !event.target.closest('[data-settings-overlay]')) {
        lastFocusRef.current = event.target;
      }
    };
    document.addEventListener('focusin', onFocusIn);
    return () => document.removeEventListener('focusin', onFocusIn);
  }, []);
  useEffect(() => () => {
    if (focusFrameRef.current !== null) window.cancelAnimationFrame(focusFrameRef.current);
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
              data-settings-menu-placement={standaloneMenu ? 'standalone' : 'inline'}
              // Standalone: Settings owns the whole viewport, so it starts at the
              // screen edge and draws no left border — there is nothing on that
              // side to divide from. The retained Workbench origin stays mounted
              // behind this portal for drafts and return state, but its sidebar
              // is not part of the surface at any width.
              //
              // Inline: the app sidebar is still there and still live, so the
              // surface starts at its trailing edge and takes the primitive's
              // own `--app-sidebar-w` offset — the same variable the sidebar
              // sizes itself with, which is what keeps the two edges together
              // while that sidebar is being dragged. Below md there is no
              // sidebar to divide from, so the border only applies from md up.
              className={standaloneMenu
                ? 'left-0 border-l-0 md:left-0 md:border-l-0'
                : 'left-0 border-l-0 md:border-l'}
              aria-describedby={undefined}
              onInteractOutside={(event) => {
                const target = event.target;
                if (
                  target instanceof Element
                  // The toggle closes the overlay itself, wherever it is drawn.
                  //
                  // The app sidebar is not "outside" at all once inline leaves
                  // it live: it is the surface this one sits beside, and its
                  // affordances already own what they do. The resize edge moves
                  // this surface's own left edge, so grabbing it must not close
                  // what the drag is laying out. A sidebar link navigates, and
                  // that navigation is what takes the user out of Settings — if
                  // dismissal also fired, `closeSettingsOverlay`'s asynchronous
                  // history traversal would race the link's synchronous push and
                  // could land on the retained origin instead of the route that
                  // was clicked. One navigation, chosen by the sidebar.
                  && target.closest('[data-settings-toggle="true"], [data-app-sidebar="true"]')
                ) {
                  event.preventDefault();
                }
              }}
              onOpenAutoFocus={() => {
                if (focusFrameRef.current !== null) {
                  window.cancelAnimationFrame(focusFrameRef.current);
                  focusFrameRef.current = null;
                }
                settingsVisitRef.current += 1;
                // Freeze the origin focus for this visit. Retained app windows
                // may focus themselves on return, before Radix's deferred close
                // callback runs; that must not replace the initiating control.
                returnFocusRef.current = lastFocusRef.current;
              }}
              onCloseAutoFocus={(event) => {
                event.preventDefault();
                const visit = settingsVisitRef.current;
                const expectedOrigin = origin;
                const target = returnFocusRef.current;
                focusFrameRef.current = window.requestAnimationFrame(() => {
                  focusFrameRef.current = null;
                  // A close callback may outlive a rapid Settings reopen or a
                  // route change. Its old focus decision must not cross either
                  // boundary and land in the new foreground surface.
                  if (
                    settingsSurfaceOpenRef.current
                    || settingsVisitRef.current !== visit
                    || expectedOrigin === null
                    || locationPath(locationRef.current) !== locationPath(expectedOrigin.location)
                  ) return;
                  if (target?.isConnected && !target.closest('[inert]')) {
                    target.focus({ preventScroll: true });
                    return;
                  }
                  if (isForegroundFocusOwner(document.activeElement)) return;
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
