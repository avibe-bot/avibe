import * as React from 'react';

// The file-preview handle, split from the provider so a FileCard can ask for a
// preview without importing the provider (and the lazy modal) it lives under.
// ``useFileViewer()`` returns null where no provider is mounted, which is the
// documented fallback signal, not an error.

export type FilePreviewTarget =
  | { kind?: 'media'; url: string; name?: string }
  | {
      kind: 'local';
      path: string;
      name: string;
      size: number | null;
      mime: string | null;
      ext: string | null;
    };

type FileViewerContextValue = { open: (target: FilePreviewTarget) => void };

export const FileViewerContext = React.createContext<FileViewerContextValue | null>(null);

export function useFileViewer(): FileViewerContextValue | null {
  return React.useContext(FileViewerContext);
}
