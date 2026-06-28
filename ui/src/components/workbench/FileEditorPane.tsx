import { Suspense, lazy, useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, PanelRightOpen, Save } from 'lucide-react';
import clsx from 'clsx';

import { useTheme } from '../../context/ThemeContext';
import { useWindowCloseGuard } from '../../context/WindowManagerContext';
import { Button } from '../ui/button';
import { fileBrowserErrorMessage, readText, writeFile } from '../../lib/filesApi';

// Monaco (the VS Code kernel) is heavy; lazy-load it so it stays out of the main
// bundle and only loads when a file is actually opened for editing.
const MonacoEditor = lazy(() => import('./MonacoEditor'));

// Map a filename to a Monaco language id. Monaco colours unknown languages as
// plaintext, so this only needs to cover the common cases; the heavy semantic
// languages (ts/js/json/css/html) also have workers wired in MonacoEditor.
const LANGUAGE_BY_EXT: Record<string, string> = {
  ts: 'typescript',
  tsx: 'typescript',
  cts: 'typescript',
  mts: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  cjs: 'javascript',
  mjs: 'javascript',
  json: 'json',
  jsonc: 'json',
  css: 'css',
  scss: 'scss',
  less: 'less',
  html: 'html',
  htm: 'html',
  xml: 'xml',
  svg: 'xml',
  md: 'markdown',
  markdown: 'markdown',
  py: 'python',
  rb: 'ruby',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  php: 'php',
  swift: 'swift',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  yml: 'yaml',
  yaml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  sql: 'sql',
  dockerfile: 'dockerfile',
  graphql: 'graphql',
  lua: 'lua',
};

function monacoLanguage(filename: string): string | undefined {
  const lower = filename.toLowerCase();
  if (lower === 'dockerfile') return 'dockerfile';
  const ext = lower.includes('.') ? lower.split('.').pop()! : '';
  return LANGUAGE_BY_EXT[ext];
}

// Read + edit + save one text/code file. Read-only is just `readOnly`. When
// `onPopOut` is provided (the in-Files editor pane), a button pops the file out
// into a standalone Editor window. Pop-out reports the editor's *live* mtime so
// the new window saves against the current revision (this pane may have saved
// since the row was opened), not the stale metadata the row carried.
export const FileEditorPane: React.FC<{
  path: string;
  filename: string;
  mtime: number | null;
  readOnly?: boolean;
  onPopOut?: (live: { mtime: number | null }) => void;
  /** The owning window id, when this editor lives in a window — enables the unsaved-close guard. */
  windowId?: string;
  /** Report dirty state up (used by the Editor IDE to aggregate one close guard over its tabs). */
  onDirtyChange?: (dirty: boolean) => void;
  /**
   * Drop the filename + save header strip. The Editor IDE (design dnYPx) shows the
   * filename + dirty dot in the tab and saves via ⌘S, so the per-pane header would
   * be a redundant second header. Standalone uses keep the header (default).
   */
  chromeless?: boolean;
  /** Live 1-based cursor position, surfaced in the IDE status bar. */
  onCursor?: (line: number, column: number) => void;
}> = ({ path, filename, mtime, readOnly = false, onPopOut, windowId, onDirtyChange, chromeless = false, onCursor }) => {
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();
  const [text, setText] = useState<string | null>(null);
  const [original, setOriginal] = useState('');
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
        if (!cancelled) setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.loadFailed')));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path, filename, mtime]);

  const dirty = !readOnly && text !== null && text !== original;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  const language = monacoLanguage(filename);
  // Monaco reuses models by `path`, so each editor INSTANCE needs a unique model URI.
  // Otherwise two panes on the same file share one model while keeping separate
  // original/savedMtime — e.g. the full-page /apps/files route mounts both the desktop
  // and the md:hidden mobile ContentPane (both windowId-less), so a save in one leaves the
  // other looking dirty and false-conflicting. Prefix a per-instance id; keep the file
  // extension so Monaco's TS worker still picks JSX/TSX for .tsx/.jsx.
  const editorUid = useId();
  const monacoPath = `${editorUid.replace(/:/g, '')}/${path.replace(/^\/+/, '')}`;

  // Veto closing the owning window while there are unsaved edits (the close unmounts
  // this pane and the buffer only lives in React state). No-op for the full-page route.
  useWindowCloseGuard(windowId, dirty ? t('apps.editor.confirmDiscardClose') : null);

  async function save() {
    // ⌘S reaches here even when the buffer is clean (the visible Save button is disabled but the
    // Monaco command isn't); skip the no-op PUT so it doesn't bump mtime / wake file watchers.
    if (text === null || saving || readOnly || !dirty) return;
    setSaving(true);
    setError(null);
    try {
      const result = await writeFile(path, text, savedMtime);
      setOriginal(text);
      setSavedMtime(result.mtime);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!chromeless && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-2">
          <span className="flex-1 truncate font-mono text-[12px] text-foreground">{filename}</span>
          {dirty && <span className="size-1.5 shrink-0 rounded-full bg-mint" title={t('apps.fileBrowser.unsaved')} />}
          {onPopOut && (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0 text-muted"
              aria-label={t('apps.editor.openInWindow')}
              // Block pop-out while dirty: the new window reloads from disk, so
              // popping out unsaved edits would silently drop them. Save first.
              title={dirty ? t('apps.editor.saveBeforePopOut') : t('apps.editor.openInWindow')}
              disabled={dirty}
              onClick={() => onPopOut({ mtime: savedMtime })}
            >
              <PanelRightOpen className="size-3.5" />
            </Button>
          )}
          {!readOnly && (
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
          )}
        </div>
      )}

      {error && (
        <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive">
          {error}
        </div>
      )}

      <div className={clsx('min-h-0 flex-1', loading && 'grid place-items-center')}>
        {loading ? (
          <Loader2 className="size-5 animate-spin text-muted" />
        ) : text === null ? null : (
          <Suspense fallback={<div className="p-4 text-[12px] text-muted">{t('common.loading')}</div>}>
            <MonacoEditor
              value={text}
              language={language}
              path={monacoPath}
              readOnly={readOnly}
              // Monaco is JS-themed (it can't read the window's `data-theme="dark"` CSS), so in the
              // IDE (chromeless = the always-dark Editor window) force the dark theme; otherwise a
              // light global theme leaves a white Monaco slab inside a dark window (design dnYPx is dark).
              dark={chromeless || resolvedTheme === 'dark'}
              onChange={(value) => setText(value)}
              onSave={() => void save()}
              onCursorChange={onCursor}
            />
          </Suspense>
        )}
      </div>
    </div>
  );
};
