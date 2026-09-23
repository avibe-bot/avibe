import * as React from 'react';

// The lightbox handle, split from the provider so the shared Markdown renderer
// and chat images can ask to open one without importing the lightbox itself.
// ``useImageViewer()`` returns null where no provider is mounted (e.g. the
// agent-config editor preview), which makes the click a documented no-op.

// A caller outside the transcript opens its own images, and what it pages
// through has to be a *stated* property of the open call: which of its URLs the
// session gallery happens to hold is an accident of when a message is sent, and
// would change under an open viewer the moment the queue flushes.
//
// ``gallery`` names that set — the composer's staged attachments, in attachment
// order — and the viewer pages within exactly it, never the transcript's images.
// ``isolated`` is the one-image case of the same idea (the queue strip): shown on
// its own, never paging, whatever the session gallery contains. Setting both is a
// contradiction; ``isolated`` wins.
export type ImageViewerOpenOptions = { isolated?: boolean; gallery?: string[] };

type ImageViewerContextValue = { open: (src: string, options?: ImageViewerOpenOptions) => void };

export const ImageViewerContext = React.createContext<ImageViewerContextValue | null>(null);

export function useImageViewer(): ImageViewerContextValue | null {
  return React.useContext(ImageViewerContext);
}
