import { createContext, useContext } from 'react';

import type { AnnotationBridge } from './useShowPageAnnotation';

export interface ShowPageAnnotationHostValue {
  src: string | null;
  annotation: AnnotationBridge;
}

export const ShowPageAnnotationHostContext = createContext<ShowPageAnnotationHostValue | null>(null);

export function useShowPageAnnotationHost(): ShowPageAnnotationHostValue | null {
  return useContext(ShowPageAnnotationHostContext);
}

export function useRequiredShowPageAnnotationHost(): ShowPageAnnotationHostValue {
  const host = useShowPageAnnotationHost();
  if (!host) throw new Error('Show Page annotation host is missing');
  return host;
}
