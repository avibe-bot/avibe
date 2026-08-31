import { useContext, useMemo } from 'react';
import type { ReactNode } from 'react';
import {
  UNSAFE_NavigationContext as NavigationContext,
} from 'react-router-dom';
import type { Navigator } from 'react-router-dom';

import { RouteSurfaceActiveContext } from '@/lib/routeSurfaceActivity';

export const RouteSurfaceActivityBoundary = ({
  active,
  children,
  inactiveReplace,
}: {
  active: boolean;
  children: ReactNode;
  inactiveReplace?: Navigator['replace'];
}) => {
  const navigation = useContext(NavigationContext);
  // A retained route owns visual state, not the foreground URL. Deny route
  // maintenance effects while it is hidden so they cannot dismiss the overlay.
  const navigator = useMemo<Navigator>(() => {
    if (active) return navigation.navigator;
    return {
      ...navigation.navigator,
      go: () => {},
      push: () => {},
      replace: inactiveReplace ?? (() => {}),
    };
  }, [active, inactiveReplace, navigation.navigator]);
  const scopedNavigation = useMemo(
    () => ({ ...navigation, navigator }),
    [navigation, navigator],
  );

  return (
    <RouteSurfaceActiveContext.Provider value={active}>
      <NavigationContext.Provider value={scopedNavigation}>
        {children}
      </NavigationContext.Provider>
    </RouteSurfaceActiveContext.Provider>
  );
};
