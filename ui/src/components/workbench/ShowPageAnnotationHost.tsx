import type { ReactNode } from 'react';

import { ShowPageAnnotationHostContext } from './ShowPageAnnotationHostContext';
import { useShowPageAnnotation } from './useShowPageAnnotation';

export const ShowPageAnnotationHost: React.FC<{
  src: string | null;
  shortcutActive: boolean;
  children: ReactNode;
}> = ({ src, shortcutActive, children }) => {
  const annotation = useShowPageAnnotation(src, shortcutActive);
  return (
    <ShowPageAnnotationHostContext.Provider value={{ src, annotation }}>
      {children}
    </ShowPageAnnotationHostContext.Provider>
  );
};
