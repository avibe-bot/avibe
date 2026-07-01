import { useTranslation } from 'react-i18next';
import { Download, Pencil } from 'lucide-react';

import { useWindowManager } from '../../context/WindowManagerContext';
import { previewKind } from '../../lib/filePreview';
import { contentUrl, downloadFile } from '../../lib/filesApi';
import { Button } from '../ui/button';
import { FilePreview } from '../ui/file-preview';

// The standalone "Preview" window app: a read-only viewer for images, PDFs, Office docs, and
// Markdown, opened on demand from the File Browser (double-click) — not a Dock-resident app (it's
// intentionally absent from APP_LIST). The window titlebar carries the filename; this body is a slim
// action bar (Open in Editor for editable text like Markdown, plus Download) over the shared
// <FilePreview> renderer, which classifies the file and picks the right viewer.
export const AppsPreviewPage: React.FC<{ windowId?: string; params?: Record<string, unknown> }> = ({ params }) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const path = typeof params?.path === 'string' ? params.path : '';
  const name = typeof params?.name === 'string' && params.name ? params.name : path.split(/[\\/]/).pop() || '';
  // Only editable text (Markdown, among the types routed here) offers "Open in Editor"; images and
  // Office docs have no editor form, so previewKind is null and the button is hidden.
  const editable = name ? previewKind(name) != null : false;

  if (!path) {
    return <div className="grid h-full w-full place-items-center bg-surface text-[12px] text-muted">{t('apps.preview.empty')}</div>;
  }

  return (
    <div className="flex h-full w-full flex-col bg-surface">
      <div className="flex items-center justify-end gap-1 border-b border-border bg-surface-2/60 px-2 py-1.5">
        {editable && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-7 gap-1.5 px-2 text-[12px] text-muted"
            onClick={() => wm.openApp('editor', { title: name, params: { path, filename: name } })}
          >
            <Pencil className="size-3.5" /> {t('apps.preview.openInEditor')}
          </Button>
        )}
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-7 text-mint"
          aria-label={t('apps.fileBrowser.download')}
          onClick={() => downloadFile(path)}
        >
          <Download className="size-3.5" />
        </Button>
      </div>
      <div className="min-h-0 flex-1">
        <FilePreview source={{ url: contentUrl(path), name }} />
      </div>
    </div>
  );
};
