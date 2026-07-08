import { Suspense, lazy, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Code2, Eye, FolderOpen, GitCompare, Loader2, PanelRightOpen, RotateCcw, Save, X } from 'lucide-react';
import clsx from 'clsx';

import { useTheme } from '../../context/ThemeContext';
import { useWindowCloseGuard } from '../../context/WindowManagerContext';
import { Button } from '../ui/button';
import { FilesApiError, fileBrowserErrorMessage, fileMeta, readText, writeFile } from '../../lib/filesApi';
import { editorPreviewKind } from '../../lib/filePreview';
import { FilePreview } from '../ui/file-preview';

// Monaco (the VS Code kernel) is heavy; lazy-load it so it stays out of the main
// bundle and only loads when a file is actually opened for editing.
const MonacoEditor = lazy(() => import('./MonacoEditor'));
// The save-conflict Compare view shares MonacoEditor's lazy chunk (same module), so opening it once
// the editor is up loads no extra JS. Named export → unwrap to a default for React.lazy.
const MonacoDiffEditor = lazy(() => import('./MonacoEditor').then((m) => ({ default: m.MonacoDiffEditor })));

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
  /** null = an unsaved "untitled" buffer (no file yet); ⌘S routes through onSaveAs (save-as). */
  path: string | null;
  filename: string;
  mtime: number | null;
  readOnly?: boolean;
  onPopOut?: (live: { mtime: number | null }) => void;
  /** The owning window id, when this editor lives in a window — enables the unsaved-close guard. */
  windowId?: string;
  /**
   * When provided, the header shows an "open file" button (the mobile single-file editor's way to
   * open/switch files — desktop uses the IDE explorer instead). No-op in `chromeless` mode, which has
   * no header.
   */
  onOpenFile?: () => void;
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
  /**
   * Untitled buffers (path === null) call this with the buffer text on save; the parent runs the
   * save-as picker + write, then re-points this pane at the chosen path.
   */
  onSaveAs?: (text: string) => void;
  /** Jump to + select a match (cross-file search result click). Forwarded to Monaco. */
  reveal?: { line: number; column: number; endColumn: number; nonce: number } | null;
  /** Bumped to force a re-read from disk (e.g. after a cross-file replace rewrote this file). */
  reloadNonce?: number;
  /**
   * True when this pane is the focused window's active tab. While previewing Markdown/SVG, Monaco
   * (the usual ⌘S owner) is unmounted and the IDE has no Save button, so the pane registers a
   * window-level ⌘S itself — scoped by this flag so only the foreground tab saves.
   */
  saveHotkey?: boolean;
}> = ({ path, filename, mtime, readOnly = false, onPopOut, windowId, onOpenFile, onDirtyChange, chromeless = false, onCursor, onSaveAs, reveal, reloadNonce, saveHotkey = false }) => {
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();
  const [text, setText] = useState<string | null>(null);
  const [original, setOriginal] = useState('');
  const [savedMtime, setSavedMtime] = useState<number | null>(mtime);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Save-conflict resolution. The backend rejects a save with error code `conflict` when the file's
  // mtime changed since we opened it — in this product that usually means the agent edited a file the
  // user has open. Rather than dead-end on the bare error strip, surface a conflict bar with Reload /
  // Overwrite / Compare. `comparing` opens the read-only side-by-side diff (disk vs the local buffer),
  // `diskText` holds the freshly-fetched disk content for it, and `resolving` guards the async actions.
  const [conflict, setConflict] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [diskText, setDiskText] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  // Markdown / SVG / HTML can be previewed (rendered) as well as edited, VS-Code-style. `mode` toggles
  // the body between the Monaco source and the rendered FilePreview, using the LIVE buffer so the
  // preview reflects unsaved edits.
  const [mode, setMode] = useState<'source' | 'preview'>('source');
  const previewable = useMemo(() => editorPreviewKind(filename), [filename]);
  // A new file may not be previewable — never strand the body in a preview mode it can't render.
  useEffect(() => {
    if (!previewable) setMode('source');
  }, [previewable]);
  // While previewing, Monaco (the usual ⌘S owner) is unmounted, so the foreground tab registers a
  // window-level ⌘S. `saveRef` holds the latest save() so the listener never saves a stale buffer.
  const saveRef = useRef<() => void>(() => {});
  useEffect(() => {
    if (!(saveHotkey && previewable && mode === 'preview')) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault();
        void saveRef.current();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [saveHotkey, previewable, mode]);

  // Tracks the path the last read targeted, so a rename (path changing from one real file to another)
  // can be told apart from an initial open or a forced reload.
  const prevPathRef = useRef<string | null>(path);
  useEffect(() => {
    const prev = prevPathRef.current;
    prevPathRef.current = path;
    let cancelled = false;
    setError(null);
    setSavedMtime(mtime);
    // A fresh read (open / rename adopt / forced reload) clears any prior conflict UI.
    setConflict(false);
    setComparing(false);
    setDiskText(null);
    // Untitled buffer (no path yet): start empty with no fetch. Saving opens the save-as picker;
    // once written, the parent re-points this pane at the real path and this effect re-runs to read it.
    if (path === null) {
      setText('');
      setOriginal('');
      setLoading(false);
      return;
    }
    // A tab's path only changes from one real file to another when the explorer renamed it (this tab
    // was repointed) — the bytes on disk are unchanged. Re-reading would silently replace an unsaved
    // buffer with the last-saved contents, so adopt the new save target WITHOUT reloading — BUT only
    // once a buffer actually exists to preserve. If the rename lands before the initial read finished
    // (text still null), fall through and read the new path so the pane doesn't go blank. (Initial
    // open and reloadNonce-forced reloads — where the path is unchanged — also read below.)
    if (prev !== null && prev !== path && text !== null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setText(null);
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
  }, [path, filename, mtime, reloadNonce]);

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
  const monacoPath = `${editorUid.replace(/:/g, '')}/${(path ?? 'untitled').replace(/^\/+/, '')}`;

  // Veto closing the owning window while there are unsaved edits (the close unmounts
  // this pane and the buffer only lives in React state). No-op for the full-page route.
  useWindowCloseGuard(windowId, dirty ? t('apps.editor.confirmDiscardClose') : null);

  async function save() {
    // `resolving` guards the window while a Reload/Overwrite is in flight: a stray ⌘S then would
    // race a save against a stale baseline (harmless — it just re-conflicts — but avoid the flicker).
    if (text === null || saving || resolving || readOnly) return;
    // Untitled: hand the buffer text up so the parent runs the save-as picker + write (an empty new
    // file is allowed to be saved, unlike a clean existing file which skips the no-op PUT below).
    if (path === null) {
      onSaveAs?.(text);
      return;
    }
    // ⌘S reaches here even when the buffer is clean (the visible Save button is disabled but the
    // Monaco command isn't); skip the no-op PUT so it doesn't bump mtime / wake file watchers.
    if (!dirty) return;
    setSaving(true);
    setError(null);
    try {
      const result = await writeFile(path, text, savedMtime);
      setOriginal(text);
      setSavedMtime(result.mtime);
    } catch (e: unknown) {
      // An mtime conflict (the file changed on disk since we opened it) gets the dedicated conflict
      // bar with Reload / Overwrite / Compare; every other failure keeps the plain error strip.
      if (e instanceof FilesApiError && e.code === 'conflict') {
        setConflict(true);
      } else {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
      }
    } finally {
      setSaving(false);
    }
  }
  saveRef.current = save;

  // Conflict → Reload: re-read the file from disk, dropping local edits. Confirm first (the buffer is
  // discarded), and adopt the fresh disk mtime as the new save baseline so the next save doesn't
  // immediately re-conflict. `path` is non-null here — the conflict bar only shows for a saved file.
  async function reloadFromDisk() {
    if (path === null || resolving) return;
    if (!window.confirm(t('apps.editor.confirmDiscardSwitch'))) return;
    setResolving(true);
    setError(null);
    try {
      // Stat before reading so the adopted baseline corresponds to (at most) the bytes we load: if the
      // file changes again in between, our baseline stays older than disk and the next save conflicts
      // rather than silently clobbering the newer writer.
      const meta = await fileMeta(path);
      const body = await readText(path);
      setText(body);
      setOriginal(body);
      setSavedMtime(meta.mtime);
      setConflict(false);
      setComparing(false);
      setDiskText(null);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.loadFailed')));
    } finally {
      setResolving(false);
    }
  }

  // Conflict → Overwrite: force-save the local buffer over the disk version. writeFile WITHOUT an
  // expected_mtime skips the backend's mtime check, and its returned mtime becomes the new baseline.
  async function overwrite() {
    if (path === null || text === null || resolving) return;
    setResolving(true);
    setError(null);
    try {
      const result = await writeFile(path, text, undefined);
      setOriginal(text);
      setSavedMtime(result.mtime);
      setConflict(false);
      setComparing(false);
      setDiskText(null);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
    } finally {
      setResolving(false);
    }
  }

  // Conflict → Compare: fetch fresh disk content for the diff's left side, then open the overlay.
  async function openCompare() {
    if (path === null || resolving) return;
    setResolving(true);
    setError(null);
    try {
      setDiskText(await readText(path));
      setComparing(true);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.loadFailed')));
    } finally {
      setResolving(false);
    }
  }

  // Reload + Overwrite are offered both in the conflict bar and from inside the Compare overlay, so
  // the shared pair lives here; the third slot differs (bar → Compare, overlay → Close).
  const conflictActions = (context: 'bar' | 'diff') => (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-7 gap-1.5 px-2.5 text-[12px]"
        disabled={resolving}
        onClick={() => void reloadFromDisk()}
      >
        <RotateCcw className="size-3" /> {t('apps.editor.conflict.reload')}
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-7 gap-1.5 px-2.5 text-[12px]"
        disabled={resolving}
        onClick={() => void overwrite()}
      >
        <Save className="size-3" /> {t('apps.editor.conflict.overwrite')}
      </Button>
      {context === 'bar' ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 gap-1.5 px-2.5 text-[12px]"
          disabled={resolving}
          onClick={() => void openCompare()}
        >
          <GitCompare className="size-3" /> {t('apps.editor.conflict.compare')}
        </Button>
      ) : (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7 gap-1.5 px-2.5 text-[12px]"
          disabled={resolving}
          onClick={() => setComparing(false)}
        >
          <X className="size-3" /> {t('common.close')}
        </Button>
      )}
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!chromeless && (
        <div className="flex items-center gap-2 border-b border-border px-3 py-2">
          {onOpenFile && (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-7 shrink-0 text-muted"
              aria-label={t('apps.editor.browseFiles')}
              title={t('apps.editor.browseFiles')}
              onClick={onOpenFile}
            >
              <FolderOpen className="size-3.5" />
            </Button>
          )}
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
              disabled={!dirty || saving || resolving || text === null}
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

      {/* Save-conflict bar — replaces the dead-end error strip when a save loses the mtime race.
          Rendered outside the header block so it also shows in chromeless (IDE) and on the mobile
          single-file page; wraps to two rows on narrow widths. Hidden while the Compare overlay is
          open, which carries its own copy of the actions. */}
      {conflict && !comparing && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-warning/40 bg-warning/[0.08] px-3 py-2">
          <div className="flex min-w-0 flex-1 items-center gap-1.5 text-[11.5px] text-foreground">
            <AlertTriangle className="size-3.5 shrink-0 text-warning" />
            <span className="min-w-0">{t('apps.editor.conflict.message')}</span>
          </div>
          {conflictActions('bar')}
        </div>
      )}

      {/* Source ⇄ Preview toggle — only for renderable text (Markdown / SVG). Renders the LIVE
          buffer, so the preview tracks unsaved edits. */}
      {previewable && !loading && text !== null && (
        <div className="flex items-center justify-end border-b border-border bg-surface-2/40 px-2 py-1">
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            {([
              { key: 'source' as const, Icon: Code2, label: t('apps.editor.source') },
              { key: 'preview' as const, Icon: Eye, label: t('apps.editor.preview') },
            ]).map(({ key, Icon, label }) => (
              <button
                key={key}
                type="button"
                onClick={() => setMode(key)}
                aria-pressed={mode === key}
                className={clsx(
                  'flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium transition',
                  mode === key ? 'bg-cyan-soft text-foreground' : 'text-muted hover:bg-foreground/[0.05] hover:text-foreground',
                )}
              >
                <Icon className="size-3" /> {label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className={clsx('relative min-h-0 flex-1', loading && 'grid place-items-center')}>
        {loading ? (
          <Loader2 className="size-5 animate-spin text-muted" />
        ) : text === null ? null : mode === 'preview' && previewable ? (
          <FilePreview source={{ text, name: filename }} />
        ) : (
          <Suspense fallback={<div className="p-4 text-[12px] text-muted">{t('common.loading')}</div>}>
            <MonacoEditor
              value={text}
              language={language}
              path={monacoPath}
              readOnly={readOnly}
              // Monaco is JS-themed (it can't read the window's `data-theme="dark"` CSS), so in the
              // IDE (chromeless = the dark-locked Editor window) force the dark theme; otherwise a
              // light global theme would leave a white Monaco slab inside the dark window (dnYPx is dark).
              dark={chromeless || resolvedTheme === 'dark'}
              onChange={(value) => setText(value)}
              onSave={() => void save()}
              onCursorChange={onCursor}
              reveal={reveal}
            />
          </Suspense>
        )}

        {/* Compare overlay: read-only side-by-side diff (disk left, local buffer right), laid over the
            editor rather than replacing it so Close returns to editing with the buffer + undo history
            intact. Reload / Overwrite resolve the conflict straight from here. */}
        {comparing && diskText !== null && text !== null && (
          <div className="absolute inset-0 z-10 flex flex-col bg-surface">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border bg-surface-2/40 px-3 py-1.5">
              <span className="min-w-0 flex-1 truncate text-[11.5px] text-muted">{t('apps.editor.conflict.compareTitle')}</span>
              {conflictActions('diff')}
            </div>
            <div className="min-h-0 flex-1">
              <Suspense
                fallback={
                  <div className="grid h-full w-full place-items-center bg-surface-2 text-[12px] text-muted">
                    {t('common.loading')}
                  </div>
                }
              >
                <MonacoDiffEditor
                  original={diskText}
                  modified={text}
                  language={language}
                  dark={chromeless || resolvedTheme === 'dark'}
                />
              </Suspense>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
