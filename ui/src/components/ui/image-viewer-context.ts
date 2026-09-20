import * as React from 'react';

// The lightbox handle, split from the provider so the shared Markdown renderer
// and chat images can ask to open one without importing the lightbox itself.
// ``useImageViewer()`` returns null where no provider is mounted (e.g. the
// agent-config editor preview), which makes the click a documented no-op.

// ``isolated`` opens the image on its own: the lightbox shows exactly that one
// src and never pages, whatever the session gallery happens to contain. A caller
// outside the transcript (the queue strip) needs that as a *stated* property —
// its URL not being in ``images`` today is an accident of when the message is
// sent, and would silently stop holding the moment the same file also appears in
// the transcript, or the queue flushes while the viewer is open.
export type ImageViewerOpenOptions = { isolated?: boolean };

type ImageViewerContextValue = { open: (src: string, options?: ImageViewerOpenOptions) => void };

export const ImageViewerContext = React.createContext<ImageViewerContextValue | null>(null);

export function useImageViewer(): ImageViewerContextValue | null {
  return React.useContext(ImageViewerContext);
}
