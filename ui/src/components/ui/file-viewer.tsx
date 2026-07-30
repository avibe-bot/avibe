import * as React from 'react';

// Session-scoped in-app file preview. ChatPage mounts the provider; a FileCard
// with a previewable proxy file or a classified local rich file opens the modal. The
// heavy modal (Shiki, the JSON viewer, the CSV parser) is ``React.lazy``-loaded,
// so none of it touches the main app bundle — it arrives only when the first
// preview opens. Mirrors ImageViewerProvider so the two media viewers stay
// structurally consistent; FileCard falls back to a plain link where no
// provider is mounted (``useFileViewer()`` returns null).

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

const FileViewerContext = React.createContext<FileViewerContextValue | null>(null);

export function useFileViewer(): FileViewerContextValue | null {
  return React.useContext(FileViewerContext);
}

const FileViewerModal = React.lazy(() => import('@/components/ui/file-viewer-modal'));

export const FileViewerProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [target, setTarget] = React.useState<FilePreviewTarget | null>(null);
  const open = React.useCallback((next: FilePreviewTarget) => setTarget(next), []);
  const close = React.useCallback(() => setTarget(null), []);
  const ctx = React.useMemo(() => ({ open }), [open]);

  return (
    <FileViewerContext.Provider value={ctx}>
      {children}
      {target && (
        <React.Suspense fallback={null}>
          {/* Key by source so opening a different file remounts the modal: state
              resets to ``loading`` without a setState-in-effect. */}
          <FileViewerModal
            key={target.kind === 'local' ? `local:${target.path}` : target.url}
            target={target}
            onClose={close}
          />
        </React.Suspense>
      )}
    </FileViewerContext.Provider>
  );
};
