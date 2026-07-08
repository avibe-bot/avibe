import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ClipboardCopy, TextSelect } from 'lucide-react';
import * as monaco from 'monaco-editor';
import { DiffEditor, Editor, loader, type OnChange, type OnMount } from '@monaco-editor/react';

import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';
import jsonWorker from 'monaco-editor/esm/vs/language/json/json.worker?worker';
import tsWorker from 'monaco-editor/esm/vs/language/typescript/ts.worker?worker';
import cssWorker from 'monaco-editor/esm/vs/language/css/css.worker?worker';
import htmlWorker from 'monaco-editor/esm/vs/language/html/html.worker?worker';

// The VS Code kernel. ALL monaco imports live in THIS module so that
// `React.lazy(() => import('./MonacoEditor'))` keeps monaco-editor + its language
// workers out of the main entry chunk — they load only when an editor first opens
// (verified: monaco is its own content-hashed chunk in `npm run build`).
//
// Self-hosted: Avibe serves the UI from the user's own machine, so the workers are
// emitted as local chunks (no CDN). We register the bundled instance with
// @monaco-editor/react via loader.config() instead of letting it fetch the AMD
// build remotely. Worker set is trimmed to the languages that ship a worker —
// editor service + json + ts/js + css + html; everything else (markdown, python,
// go, rust, yaml, …) uses Monaco's worker-less Monarch tokenizer and still
// highlights without a dedicated worker.
self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    switch (label) {
      case 'json':
        return new jsonWorker();
      case 'css':
      case 'scss':
      case 'less':
        return new cssWorker();
      case 'html':
      case 'handlebars':
      case 'razor':
        return new htmlWorker();
      case 'typescript':
      case 'javascript':
        return new tsWorker();
      default:
        return new editorWorker();
    }
  },
};

loader.config({ monaco });

// One-time global setup, run on first mount of any editor.
let configured = false;
function setupMonaco(): void {
  if (configured) return;
  configured = true;

  // An avibe-flavoured dark theme: vs-dark, but with the editor surface matched to
  // the window's --surface-2 so Monaco blends into the AppWindow chrome instead of
  // sitting on its own near-black slab.
  monaco.editor.defineTheme('avibe-dark', {
    base: 'vs-dark',
    inherit: true,
    rules: [],
    colors: {
      'editor.background': '#11111c',
      'editorGutter.background': '#11111c',
      'minimap.background': '#11111c',
      'editorLineNumber.foreground': '#4b5163',
      'editorLineNumber.activeForeground': '#9ba3b8',
      'editorWidget.background': '#0e0e18',
      'editor.lineHighlightBackground': '#ffffff0a',
      'editor.selectionBackground': '#3fe0e533',
      'editorIndentGuide.background1': '#ffffff12',
    },
  });

  // This is a whole-machine file editor, not a project IDE: a lone file would
  // otherwise light up with false "cannot find module" / "duplicate identifier"
  // semantic errors. Keep syntax checking, drop semantic validation for ts/js.
  // (0.55 moved the language namespaces to the top level — monaco.typescript.)
  for (const defaults of [monaco.typescript.typescriptDefaults, monaco.typescript.javascriptDefaults]) {
    defaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: false });
  }
}

// Symbol keys phones can't easily reach (mirrors the Terminal accessory bar).
const SYMBOL_KEYS = ['(', ')', '{', '}', '[', ']', ';', ':', '=', '"', "'", '`', '|', '/', '\\', '-', '_'];

export interface MonacoEditorProps {
  value: string;
  /** Monaco language id (e.g. `typescript`); falls back to plaintext when omitted. */
  language?: string;
  /**
   * Model path/URI for the file. Monaco's TS/JS worker keys JSX/TSX script kind off
   * the model URI's extension, so `.tsx`/`.jsx` files need a path here or they parse as
   * plain TS/JS and show bogus syntax errors. Should be unique per open editor.
   */
  path?: string;
  readOnly?: boolean;
  /** Resolved app theme — drives the dark VS Code theme vs the light one. */
  dark?: boolean;
  onChange?: (value: string) => void;
  /** Save the file — bound to ⌘S / Ctrl+S inside the editor (the IDE has no visible save button). */
  onSave?: () => void;
  /** Live 1-based cursor position, for an IDE status bar (`Ln x, Col y`). */
  onCursorChange?: (line: number, column: number) => void;
  /**
   * Jump to + select a match (from cross-file search). `line` is 1-based; `column`/`endColumn`
   * are 0-based offsets within the line (the search backend's convention). `nonce` makes a
   * repeated jump to the same spot re-fire.
   */
  reveal?: { line: number; column: number; endColumn: number; nonce: number } | null;
}

function applyReveal(editor: monaco.editor.IStandaloneCodeEditor, reveal: { line: number; column: number; endColumn: number }) {
  const line = Math.max(1, reveal.line);
  const startColumn = Math.max(1, reveal.column + 1);
  const endColumn = Math.max(startColumn, reveal.endColumn + 1);
  const range = { startLineNumber: line, startColumn, endLineNumber: line, endColumn };
  editor.revealRangeInCenter(range);
  editor.setSelection(range);
  editor.focus();
}

export default function MonacoEditor({ value, language, path, readOnly, dark = true, onChange, onSave, onCursorChange, reveal }: MonacoEditorProps) {
  const { t } = useTranslation();
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  // Keep the latest callbacks in refs: the ⌘S command + cursor listener are bound
  // once on mount (Monaco can't cleanly rebind a command), so they must read live
  // closures rather than capture the first render's.
  const onSaveRef = useRef(onSave);
  const onCursorRef = useRef(onCursorChange);
  // Hold the latest reveal so handleMount can apply one that arrived before the editor existed
  // (opening a not-yet-open file from a search result mounts the editor with reveal already set).
  const revealRef = useRef(reveal);
  useEffect(() => {
    onSaveRef.current = onSave;
    onCursorRef.current = onCursorChange;
    revealRef.current = reveal;
  });

  const handleMount: OnMount = (editor) => {
    editorRef.current = editor;
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSaveRef.current?.());
    const pos = editor.getPosition();
    if (pos) onCursorRef.current?.(pos.lineNumber, pos.column);
    editor.onDidChangeCursorPosition((e) => onCursorRef.current?.(e.position.lineNumber, e.position.column));
    if (revealRef.current) applyReveal(editor, revealRef.current);
  };

  // Already-open file: a new reveal (its nonce changes) jumps + selects the match in place.
  useEffect(() => {
    if (editorRef.current && reveal) applyReveal(editorRef.current, reveal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal?.nonce]);
  const handleChange: OnChange = (next) => onChange?.(next ?? '');

  // Accessory actions operate on the live editor instance.
  const insert = (text: string) => {
    const editor = editorRef.current;
    if (!editor || readOnly) return;
    editor.focus();
    const selection = editor.getSelection();
    if (selection) editor.executeEdits('accessory', [{ range: selection, text, forceMoveMarkers: true }]);
  };
  const indent = () => {
    const editor = editorRef.current;
    if (!editor || readOnly) return;
    editor.focus();
    // The `tab` command honours the model's insertSpaces / tabSize settings.
    editor.trigger('accessory', 'tab', null);
  };
  const dismissKeyboard = () => {
    // On phones Esc has no real editor meaning; use it to drop focus and hide the
    // soft keyboard so the user can read the file.
    (document.activeElement as HTMLElement | null)?.blur();
  };
  const selectAll = () => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    editor.getAction('editor.action.selectAll')?.run();
  };
  const copyAll = () => {
    const editor = editorRef.current;
    if (!editor) return;
    void navigator.clipboard?.writeText(editor.getValue());
  };

  const accessoryBtn =
    'shrink-0 rounded-md border border-border-strong px-2.5 py-1.5 font-mono text-[12px] text-foreground active:bg-foreground/[0.08]';

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">
        <Editor
          theme={dark ? 'avibe-dark' : 'light'}
          path={path}
          language={language}
          value={value}
          onChange={handleChange}
          onMount={handleMount}
          beforeMount={setupMonaco}
          loading={
            <div className="grid h-full w-full place-items-center bg-surface-2 text-[12px] text-muted">
              {t('common.loading')}
            </div>
          }
          options={{
            readOnly,
            fontSize: 13,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            automaticLayout: true,
            tabSize: 2,
            renderWhitespace: 'selection',
            smoothScrolling: true,
            cursorBlinking: 'smooth',
            padding: { top: 10, bottom: 10 },
            scrollbar: { useShadows: false },
          }}
        />
      </div>

      {/* Touch accessory bar — Monaco has no touch affordances, so coarse-pointer devices get the
          symbol keys + select/copy helpers. Keyed off pointer type rather than the md: viewport
          breakpoint so tablets keep it and desktops (a hardware keyboard already has these keys)
          don't — the same rule TerminalView's key bar uses (PR #796). */}
      <div className="hidden items-center gap-1 overflow-x-auto border-t border-border bg-surface px-2 py-1.5 pointer-coarse:flex">
        {!readOnly &&
          SYMBOL_KEYS.map((key) => (
            <button key={key} type="button" onClick={() => insert(key)} className={accessoryBtn}>
              {key}
            </button>
          ))}
        {!readOnly && (
          <button type="button" onClick={indent} className={accessoryBtn}>
            {t('apps.terminal.keys.tab')}
          </button>
        )}
        <button type="button" onClick={dismissKeyboard} className={accessoryBtn}>
          {t('apps.terminal.keys.esc')}
        </button>
        <span className="mx-0.5 h-5 w-px shrink-0 bg-border-strong" />
        <button
          type="button"
          onClick={selectAll}
          className="flex shrink-0 items-center gap-1 rounded-md border border-border-strong px-2.5 py-1.5 text-[12px] text-foreground active:bg-foreground/[0.08]"
        >
          <TextSelect className="size-3.5" />
          {t('apps.editor.selectAll')}
        </button>
        <button
          type="button"
          onClick={copyAll}
          className="flex shrink-0 items-center gap-1 rounded-md border border-border-strong px-2.5 py-1.5 text-[12px] text-foreground active:bg-foreground/[0.08]"
        >
          <ClipboardCopy className="size-3.5" />
          {t('apps.editor.copyAll')}
        </button>
      </div>
    </div>
  );
}

export interface MonacoDiffEditorProps {
  /** Left side — the on-disk content (read-only baseline). */
  original: string;
  /** Right side — the local editor buffer. */
  modified: string;
  /** Monaco language id, applied to both sides; falls back to plaintext when omitted. */
  language?: string;
  /** Resolved app theme — drives the dark VS Code theme vs the light one. */
  dark?: boolean;
}

// Read-only side-by-side diff, used by the save-conflict Compare view (disk vs the local buffer).
// Lives in THIS module so it rides the same lazy monaco chunk + `setupMonaco` theme registration as
// the editor — opening it from an already-open file loads no extra chunk. Both sides are read-only:
// this is a comparison, not a merge surface; the conflict is resolved via Reload / Overwrite instead.
export function MonacoDiffEditor({ original, modified, language, dark = true }: MonacoDiffEditorProps) {
  const { t } = useTranslation();
  return (
    <DiffEditor
      theme={dark ? 'avibe-dark' : 'light'}
      language={language}
      original={original}
      modified={modified}
      beforeMount={setupMonaco}
      loading={
        <div className="grid h-full w-full place-items-center bg-surface-2 text-[12px] text-muted">
          {t('common.loading')}
        </div>
      }
      options={{
        readOnly: true,
        originalEditable: false,
        // Side-by-side per the design; Monaco auto-collapses to inline when the pane is too narrow
        // (its default useInlineViewWhenSpaceIsLimited), so the mobile single-file page stays legible.
        renderSideBySide: true,
        fontSize: 13,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        automaticLayout: true,
        renderOverviewRuler: false,
        scrollbar: { useShadows: false },
      }}
    />
  );
}
