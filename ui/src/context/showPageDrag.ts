import { createContext, useContext } from 'react';

export type DockDropAction = () => void | Promise<void>;

export interface ShowPageDragValue {
  active: boolean;
  begin: (onDropToDock: DockDropAction) => void;
  end: () => void;
  dropToDock: () => void;
}

export const ShowPageDragContext = createContext<ShowPageDragValue | null>(null);

export function useShowPageDrag(): ShowPageDragValue {
  const context = useContext(ShowPageDragContext);
  if (!context) throw new Error('useShowPageDrag must be used within ShowPageDragProvider');
  return context;
}
