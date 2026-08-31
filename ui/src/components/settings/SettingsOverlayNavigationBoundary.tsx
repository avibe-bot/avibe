import { useContext, useMemo } from 'react';
import type { ReactNode } from 'react';
import {
  parsePath,
  UNSAFE_NavigationContext as NavigationContext,
  useLocation,
} from 'react-router-dom';
import type { Navigator, To } from 'react-router-dom';

import { settingsOverlayNavigationState } from '@/lib/settingsOverlay';

const destinationPathname = (to: To, fallback: string): string => {
  const pathname = typeof to === 'string' ? parsePath(to).pathname : to.pathname;
  return pathname ?? fallback;
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

  return (
    <NavigationContext.Provider value={scopedNavigation}>
      {children}
    </NavigationContext.Provider>
  );
};
