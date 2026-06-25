import { Suspense, lazy, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Save } from 'lucide-react';
import { languages } from '@codemirror/language-data';
import clsx from 'clsx';

import { Button } from '../ui/button';
import { FilesApiError, readText, writeFile } from '../../lib/filesApi';

// CodeMirror 6 is heavy; lazy-load it so it stays out of the main bundle (same
// approach as the file-viewer modal). @uiw/react-codemirror's default export is
// the editor component.
const CodeMirror = lazy(() => import('@uiw/react-codemirror'));

async function loadLanguageExtension(filename: string): Promise<any[]> {
  const ext = filename.includes('.') ? filename.split('.').pop()!.toLowerCase() : '';
  if (!ext) return [];
  const desc = (languages as any[]).find((lang) => lang.extensions?.includes(ext));
  if (!desc) return [];
  try {
    return [await desc.load()];
  } catch {
    return [];
  }
}

// Read + edit + save one text/code file. Read-only is just `editable={false}`.
export const FileEditorPane: React.FC<{ path: string; filename: string; mtime: number | null }> = ({
  path,
  filename,
  mtime,
}) => {
  const { t } = useTranslation();
  const [text, setText] = useState<string | null>(null);
  const [original, setOriginal] = useState('');
  const [langExt, setLangExt] = useState<any[]>([]);
  const [savedMtime, setSavedMtime] = useState<number | null>(mtime);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setText(null);
    setSavedMtime(mtime);
    readText(path)
      .then((body) => {
        if (cancelled) return;
        setText(body);
        setOriginal(body);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    void loadLanguageExtension(filename).then((ext) => {
      if (!cancelled) setLangExt(ext);
    });
    return () => {
      cancelled = true;
    };
  }, [path, filename, mtime]);

  const dirty = text !== null && text !== original;

  async function save() {
    if (text === null || saving) return;
    setSaving(true);
    setError(null);
    try {
      const result = await writeFile(path, text, savedMtime);
      setOriginal(text);
      setSavedMtime(result.mtime);
    } catch (e: unknown) {
      if (e instanceof FilesApiError && e.code === 'conflict') {
        setError(t('apps.fileBrowser.conflict'));
      } else {
        setError(e instanceof Error ? e.message : 'Save failed');
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="flex-1 truncate font-mono text-[12px] text-foreground">{filename}</span>
        {dirty && <span className="size-1.5 shrink-0 rounded-full bg-mint" title={t('apps.fileBrowser.unsaved')} />}
        <Button
          type="button"
          size="sm"
          variant="brand"
          disabled={!dirty || saving || text === null}
          onClick={() => void save()}
          className="h-7 gap-1.5 px-2.5 text-[12px]"
        >
          {saving ? <Loader2 className="size-3 animate-spin" /> : <Save className="size-3" />}
          {t('apps.fileBrowser.save')}
        </Button>
      </div>

      {error && (
        <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive">
          {error}
        </div>
      )}

      <div className={clsx('min-h-0 flex-1 overflow-auto', loading && 'grid place-items-center')}>
        {loading ? (
          <Loader2 className="size-5 animate-spin text-muted" />
        ) : text === null ? null : (
          <Suspense fallback={<div className="p-4 text-[12px] text-muted">{t('common.loading')}</div>}>
            <CodeMirror
              value={text}
              height="100%"
              extensions={langExt}
              onChange={(value: string) => setText(value)}
              basicSetup={{ lineNumbers: true, highlightActiveLine: true }}
            />
          </Suspense>
        )}
      </div>
    </div>
  );
};
