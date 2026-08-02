import * as React from 'react';

// The lightbox handle, split from the provider so the shared Markdown renderer
// and chat images can ask to open one without importing the lightbox itself.
// ``useImageViewer()`` returns null where no provider is mounted (e.g. the
// agent-config editor preview), which makes the click a documented no-op.

type ImageViewerContextValue = { open: (src: string) => void };

export const ImageViewerContext = React.createContext<ImageViewerContextValue | null>(null);

export function useImageViewer(): ImageViewerContextValue | null {
  return React.useContext(ImageViewerContext);
}
