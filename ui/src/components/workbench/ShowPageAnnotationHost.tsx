import type { ReactNode } from 'react';

import { ShowPageAnnotationHostContext } from './ShowPageAnnotationHostContext';
import { useShowPageAnnotation } from './useShowPageAnnotation';

export const ShowPageAnnotationHost: React.FC<{ src: string | null; children: ReactNode }> = ({ src, children }) => {
  const annotation = useShowPageAnnotation(src);
  return (
    <ShowPageAnnotationHostContext.Provider value={{ src, annotation }}>
      {children}
    </ShowPageAnnotationHostContext.Provider>
  );
};
