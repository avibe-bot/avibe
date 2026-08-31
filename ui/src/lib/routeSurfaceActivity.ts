import { createContext, useContext } from 'react';

export const RouteSurfaceActiveContext = createContext(true);

export const useRouteSurfaceActive = (): boolean => useContext(RouteSurfaceActiveContext);
