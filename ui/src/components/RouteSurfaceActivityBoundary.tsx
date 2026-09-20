import { useContext, useInsertionEffect, useMemo, useRef } from 'react';
import type { ReactNode } from 'react';
import {
  UNSAFE_DataRouterContext as DataRouterContext,
  UNSAFE_NavigationContext as NavigationContext,
} from 'react-router-dom';
import type { Navigator, RouterNavigateOptions, To } from 'react-router-dom';

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
  const dataNavigation = useContext(DataRouterContext);
  // Old async callers retain these wrappers. Admission belongs to the current
  // committed surface, not the render that supplied their navigate callback.
  // Publish before descendants run layout effects; speculative renders cannot
  // revoke an active surface or authorize an inactive one.
  const committed = useRef({ active: false, navigation, dataNavigation, inactiveReplace });
  useInsertionEffect(() => {
    committed.current = { active, navigation, dataNavigation, inactiveReplace };
    return () => { committed.current = { ...committed.current, active: false, inactiveReplace: undefined }; };
  }, [active, navigation, dataNavigation, inactiveReplace]);
  const navigator = useMemo<Navigator>(() => ({
    ...navigation.navigator,
    go: (...args) => {
      if (committed.current.active) committed.current.navigation.navigator.go(...args);
    },
    push: (...args) => {
      if (committed.current.active) committed.current.navigation.navigator.push(...args);
    },
    replace: (...args) => {
      const current = committed.current;
      if (current.active) current.navigation.navigator.replace(...args);
      else current.inactiveReplace?.(...args);
    },
  }), [navigation.navigator]);
  const scopedNavigation = useMemo(
    () => ({ ...navigation, navigator }),
    [navigation, navigator],
  );
  const scopedDataNavigation = useMemo(() => {
    if (!dataNavigation) return null;
    // Data-router useNavigate bypasses Navigator. Scope the router facade, never
    // mutate the shared router used by foreground Settings or other surfaces.
    const router = Object.create(dataNavigation.router) as typeof dataNavigation.router;
    router.navigate = ((to: number | To | null, options?: RouterNavigateOptions): Promise<void> => {
      const current = committed.current;
      if (current.active && current.dataNavigation) {
        return typeof to === 'number'
          ? current.dataNavigation.router.navigate(to)
          : current.dataNavigation.router.navigate(to, options);
      }
      if (typeof to !== 'number' && options?.replace) {
        current.inactiveReplace?.(to ?? '.', options.state, options);
      }
      return Promise.resolve();
    }) as typeof router.navigate;
    return { ...dataNavigation, router };
  }, [dataNavigation]);

  const content = dataNavigation ? (
    <DataRouterContext.Provider value={scopedDataNavigation}>
      {children}
    </DataRouterContext.Provider>
  ) : children;
  return (
    <RouteSurfaceActiveContext.Provider value={active}>
      <NavigationContext.Provider value={scopedNavigation}>
        {content}
      </NavigationContext.Provider>
    </RouteSurfaceActiveContext.Provider>
  );
};
