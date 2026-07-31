import { useCallback, useMemo, useRef, useState } from 'react';

import { ShowPageDragContext, type DockDropAction } from './showPageDrag';

/** Coordinates one native drag between the chat header and the sidebar Dock. */
export const ShowPageDragProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [active, setActive] = useState(false);
  const dockDropRef = useRef<DockDropAction | null>(null);

  const begin = useCallback((onDropToDock: DockDropAction) => {
    dockDropRef.current = onDropToDock;
    setActive(true);
  }, []);

  const end = useCallback(() => {
    dockDropRef.current = null;
    setActive(false);
  }, []);

  const dropToDock = useCallback(() => {
    const action = dockDropRef.current;
    dockDropRef.current = null;
    setActive(false);
    if (action) void action();
  }, []);

  const value = useMemo(() => ({ active, begin, end, dropToDock }), [active, begin, dropToDock, end]);
  return <ShowPageDragContext.Provider value={value}>{children}</ShowPageDragContext.Provider>;
};
