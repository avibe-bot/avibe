import { Suspense, lazy } from 'react';
import { useTranslation } from 'react-i18next';
import { FileCode2 } from 'lucide-react';

// The Editor window body. Reuses FileEditorPane (Monaco) so the standalone Editor
// app and the in-Files edit pane share one editor. Files opens a file here via
// `openApp('editor', { params: { path, filename, mtime } })`; with no file it
// shows a tasteful empty state.
const FileEditorPane = lazy(() => import('./FileEditorPane').then((m) => ({ default: m.FileEditorPane })));

export const EditorApp: React.FC<{ windowId?: string; params?: Record<string, unknown> }> = ({ windowId, params }) => {
  const { t } = useTranslation();
  const path = typeof params?.path === 'string' ? params.path : null;
  const mtime = typeof params?.mtime === 'number' ? params.mtime : null;
  const filename =
    typeof params?.filename === 'string'
      ? params.filename
      : path
        ? path.split('/').filter(Boolean).pop() ?? path
        : '';

  if (!path) {
    return (
      <div className="grid h-full w-full place-items-center bg-surface-2 p-6 text-center">
        <div className="flex max-w-xs flex-col items-center gap-3">
          <div className="grid size-14 place-items-center rounded-2xl border border-border bg-foreground/[0.03]">
            <FileCode2 className="size-7 text-violet" />
          </div>
          <div className="text-[14px] font-semibold text-foreground">{t('apps.editor.empty')}</div>
          <p className="text-[12.5px] text-muted">{t('apps.editor.emptyHint')}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full bg-surface-2">
      <Suspense
        fallback={<div className="grid h-full w-full place-items-center text-[12px] text-muted">{t('common.loading')}</div>}
      >
        {/* key by path: remount per file so a stale load/save can't apply to a different file. */}
        <FileEditorPane key={path} path={path} filename={filename} mtime={mtime} windowId={windowId} />
      </Suspense>
    </div>
  );
};
