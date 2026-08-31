import { useContext, useMemo } from 'react';
import type { ReactNode } from 'react';
import {
  resolvePath,
  UNSAFE_DataRouterContext as DataRouterContext,
  UNSAFE_NavigationContext as NavigationContext,
  useLocation,
} from 'react-router-dom';
import type { Navigator, RouterNavigateOptions, To } from 'react-router-dom';

import { settingsOverlayNavigationState } from '@/lib/settingsOverlay';

const destinationPathname = (to: To, fallback: string): string => {
  return resolvePath(to, fallback).pathname;
};

export const SettingsOverlayNavigationBoundary = ({
  children,
  desktop,
}: {
  children: ReactNode;
  desktop: boolean;
}) => {
  const location = useLocation();
  const navigation = useContext(NavigationContext);
  const dataNavigation = useContext(DataRouterContext);
  // Every Settings-bound navigation carries one durable origin. This is the
  // ownership point for all links, redirects, and programmatic navigation.
  const navigator = useMemo<Navigator>(() => ({
    ...navigation.navigator,
    push: (to, state, options) => navigation.navigator.push(
      to,
      settingsOverlayNavigationState({
        destinationPathname: destinationPathname(to, location.pathname),
        desktop,
        source: location,
        targetState: state,
      }),
      options,
    ),
    replace: (to, state, options) => navigation.navigator.replace(
      to,
      settingsOverlayNavigationState({
        destinationPathname: destinationPathname(to, location.pathname),
        desktop,
        source: location,
        targetState: state,
      }),
      options,
    ),
  }), [desktop, location, navigation.navigator]);
  const scopedNavigation = useMemo(
    () => ({ ...navigation, navigator }),
    [navigation, navigator],
  );
  // Data routers bypass NavigationContext and call router.navigate directly.
  // Keep the same state owner in both router modes so production and tests
  // cannot disagree about whether Settings has a background location.
  const scopedDataNavigation = useMemo(() => {
    if (!dataNavigation) return null;
    const router = Object.create(dataNavigation.router) as typeof dataNavigation.router;
    router.navigate = ((
      to: number | To | null,
      options?: RouterNavigateOptions,
    ): Promise<void> => {
      if (typeof to === 'number' || to === null) {
        return typeof to === 'number'
          ? dataNavigation.router.navigate(to)
          : dataNavigation.router.navigate(to, options);
      }
      return dataNavigation.router.navigate(to, {
        ...options,
        state: settingsOverlayNavigationState({
          destinationPathname: destinationPathname(to, location.pathname),
          desktop,
          source: location,
          targetState: options?.state,
        }),
      });
    }) as typeof dataNavigation.router.navigate;
    return { ...dataNavigation, router };
  }, [dataNavigation, desktop, location]);

  const content = scopedDataNavigation ? (
    <DataRouterContext.Provider value={scopedDataNavigation}>
      {children}
    </DataRouterContext.Provider>
  ) : children;

  return (
    <NavigationContext.Provider value={scopedNavigation}>
      {content}
    </NavigationContext.Provider>
  );
};
