import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { Blocks, Bug, Clock, CodeXml, FilePlus, Files, FileText, FolderOpen, GitBranch, Image as ImageIcon, Search, Settings, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useWindowCloseGuard, useWindowManager } from '../../context/WindowManagerContext';
import { contentUrl, downloadFile, fileMeta, joinPath, parentDir, writeFile, type FsEntry } from '../../lib/filesApi';
import { isEditableFile, isEditableMeta, previewOverlayKind, previewRenderKind } from '../../lib/filePreview';
import { FileTree } from './FileTree';
import { FilePreview } from '../ui/file-preview';
import { FilePicker, type FilePickerMode } from './FilePicker';
import { EditorSearchView } from './EditorSearchView';

const FileEditorPane = lazy(() => import('./FileEditorPane').then((m) => ({ default: m.FileEditorPane })));

// A tab carries the file's mtime captured at open time, so saving stays conditional on it
// (writeFile's expected_mtime) and the backend can surface a conflict instead of silently
// clobbering newer on-disk content. `path` is null for an unsaved "untitled" buffer (a VS
// Code-style new file): it lives only in memory until the first save picks a location. Tabs are
// keyed by a synthetic `id` (not the path) so an untitled tab survives becoming a saved file.
// `kind` defaults to 'edit' (a Monaco buffer). A 'preview' tab renders the read-only FilePreview
// kernel instead — for a raster image / PDF / Office doc, which have no editable text form.
type Tab = { id: string; path: string | null; name: string; mtime: number | null; reload?: number; kind?: 'edit' | 'preview' };

// A pending file dialog, rendered as the in-window FilePicker overlay.
type PickerState = {
  mode: FilePickerMode;
  initialPath: string | null;
  defaultName?: string;
  onConfirm: (result: { path: string; name?: string }) => Promise<void> | void;
};

// Human language label for the status bar (design dnYPx shows e.g. "TypeScript React").
const LANGUAGE_LABEL: Record<string, string> = {
  ts: 'TypeScript',
  tsx: 'TypeScript React',
  cts: 'TypeScript',
  mts: 'TypeScript',
  js: 'JavaScript',
  jsx: 'JavaScript React',
  cjs: 'JavaScript',
  mjs: 'JavaScript',
  json: 'JSON',
  jsonc: 'JSON',
  css: 'CSS',
  scss: 'SCSS',
  less: 'Less',
  html: 'HTML',
  md: 'Markdown',
  markdown: 'Markdown',
  py: 'Python',
  rb: 'Ruby',
  go: 'Go',
  rs: 'Rust',
  java: 'Java',
  c: 'C',
  cpp: 'C++',
  cs: 'C#',
  php: 'PHP',
  sh: 'Shell',
  bash: 'Shell',
  yml: 'YAML',
  yaml: 'YAML',
  toml: 'TOML',
  sql: 'SQL',
};

// Canonical language identifier for the status bar (e.g. "TypeScript React"). These are product
// names shown untranslated, exactly like VS Code's language indicator; the generic "plain text"
// fallback IS localized, at the call site. Returns undefined when there's no specific language.
function languageLabel(filename: string | undefined): string | undefined {
  if (!filename) return undefined;
  const lower = filename.toLowerCase();
  if (lower === 'dockerfile') return 'Dockerfile';
  const ext = lower.includes('.') ? lower.split('.').pop()! : '';
  return LANGUAGE_LABEL[ext];
}

// The Editor IDE (design dnYPx + welcome w0qoC): activity bar + collapsible explorer tree +
// editor tabs + Monaco + cyan status bar; a VS-Code-style welcome when nothing is open. Monaco is
// fully integrated here. Files opens a file via openApp('editor', { params: { path } }). Open
// Folder / Save use the in-window FilePicker (browsing the real filesystem), and New File opens an
// untitled buffer that picks its location only on first save.
export const EditorApp: React.FC<{ windowId?: string; params?: Record<string, unknown> }> = ({ windowId, params }) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const [root, setRoot] = useState<string | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState<string | null>(null); // active tab id
  const [dirty, setDirty] = useState<Record<string, boolean>>({}); // keyed by tab id
  const [cursor, setCursor] = useState<{ line: number; col: number } | null>(null);
  const [picker, setPicker] = useState<PickerState | null>(null);
  const [view, setView] = useState<'files' | 'search'>('files');
  // A pending Monaco jump (from a cross-file search result), scoped to one tab. The nonce makes a
  // repeat jump to the same spot re-fire even when the tab is already open.
  const [reveal, setReveal] = useState<{ tabId: string; line: number; column: number; endColumn: number; nonce: number } | null>(null);
  const [searchFocus, setSearchFocus] = useState(0);
  // Bumped after a save-as writes a new file, so the explorer tree re-lists that folder.
  const [treeRefresh, setTreeRefresh] = useState<{ path: string; nonce: number } | null>(null);
  const tabSeq = useRef(0);
  const untitledSeq = useRef(0);
  const revealSeq = useRef(0);
  const treeRefreshSeq = useRef(0);
  // Latest tabs + dirty for async callbacks (reloadTabs) so a reload landing after an in-flight
  // replace never acts on a stale snapshot and clobbers a buffer the user just edited.
  const dirtyRef = useRef(dirty);
  const tabsRef = useRef(tabs);
  useEffect(() => {
    dirtyRef.current = dirty;
    tabsRef.current = tabs;
  });

  const openFile = useCallback(
    (path: string, name: string, mtime: number | null, target?: { line: number; column: number; endColumn: number }) => {
      setRoot((r) => r ?? parentDir(path));
      if (!target) setCursor(null);
      setTabs((ts) => {
        // Dedup inside the updater so two concurrent opens of the same file (e.g. a fast double-click
        // firing two fileMeta requests) can't both append a tab — both see the same current `ts`.
        const existing = ts.find((x) => x.path === path);
        const id = existing ? existing.id : `t${++tabSeq.current}`;
        setActive(id);
        if (target) setReveal({ tabId: id, line: target.line, column: target.column, endColumn: target.endColumn, nonce: ++revealSeq.current });
        if (!existing) return [...ts, { id, path, name, mtime }];
        // Jumping to an already-open tab whose file changed on disk since it was opened (e.g. a
        // search hit in newer content): reload the clean buffer first so the revealed line matches
        // what the search showed. A dirty tab keeps its edits.
        if (mtime != null && existing.mtime !== mtime && !dirtyRef.current[existing.id]) {
          return ts.map((x) => (x.id === id ? { ...x, mtime, reload: (x.reload ?? 0) + 1 } : x));
        }
        return ts;
      });
    },
    [],
  );

  // Open (or focus) a rich file (raster image / PDF / Office doc) as a read-only preview tab (no
  // Monaco). Dedups by path inside the updater so a fast double-open can't append two tabs.
  const openPreview = useCallback((path: string, name: string) => {
    setRoot((r) => r ?? parentDir(path));
    setTabs((ts) => {
      const existing = ts.find((x) => x.path === path);
      const id = existing ? existing.id : `t${++tabSeq.current}`;
      setActive(id);
      setCursor(null);
      if (existing) return ts;
      return [...ts, { id, path, name, mtime: null, kind: 'preview' }];
    });
  }, []);

  // Open (or focus) a file at a search match and select the range in Monaco. Fetch fresh metadata
  // for the save baseline + re-validation, mirroring the explorer's open path.
  const onJump = useCallback(
    async (path: string, line: number, col: number, endCol: number) => {
      const name = path.split('/').filter(Boolean).pop() || path;
      const target = { line, column: col, endColumn: endCol };
      try {
        const m = await fileMeta(path);
        openFile(path, name, m.mtime, target);
      } catch {
        openFile(path, name, null, target);
      }
    },
    [openFile],
  );

  // The explorer tree emits a clicked entry. Gate it like the File Browser: only a regular,
  // supported, within-cap file opens in Monaco; a symlink (the backend refuses symlink writes),
  // oversized, or unsupported/binary entry downloads instead. The download branch runs BEFORE any
  // await so the click's user activation survives (Safari/iOS would block the popup otherwise).
  const onTreeOpen = useCallback(
    async (path: string, entry: FsEntry) => {
      // A raster image / PDF / Office doc opens in a read-only preview tab (no Monaco); svg / html /
      // markdown / code / json / csv stay editable (with a preview toggle inside the pane).
      if (previewOverlayKind(entry)) {
        openPreview(path, entry.name);
        return;
      }
      // Fetch fresh metadata (also content-sniffs `text`) and decide by CONTENT, not just the
      // extension — an extensionless / unknown-type TEXT file opens in the editor, while a
      // symlink / oversized / binary file downloads. Meta also gives the save-baseline mtime.
      try {
        const m = await fileMeta(path);
        if (isEditableMeta(m)) {
          openFile(path, entry.name, m.mtime);
        } else {
          downloadFile(path);
        }
      } catch {
        // Metadata fetch failed — fall back to the name-only guess so a known text type still opens.
        if (isEditableFile(entry)) {
          openFile(path, entry.name, entry.mtime);
        } else {
          downloadFile(path);
        }
      }
    },
    [openFile, openPreview],
  );

  // After a cross-file replace/undo rewrites files on disk, reload any open, non-dirty tab for a
  // changed file so it shows the new content with a fresh save baseline (a dirty tab keeps its
  // buffer — the mtime guard still catches a stale save). Refetch the mtime so the tab saves
  // against the post-replace revision, and bump `reload` to re-run the pane's read effect.
  const reloadTabs = useCallback(async (paths: string[]) => {
    const set = new Set(paths);
    const targets = tabsRef.current.filter((tb) => tb.path && set.has(tb.path));
    if (!targets.length) return;
    const metas = await Promise.all(
      targets.map(async (tb) => {
        try {
          return { id: tb.id, mtime: (await fileMeta(tb.path as string)).mtime };
        } catch {
          return { id: tb.id, mtime: null as number | null };
        }
      }),
    );
    const byId = new Map(metas.map((m) => [m.id, m.mtime]));
    // Re-check dirty at apply time (via ref): a tab the user edited while the request was in flight
    // must keep its unsaved buffer rather than be re-read from disk.
    setTabs((ts) => ts.map((tb) => (byId.has(tb.id) && !dirtyRef.current[tb.id] ? { ...tb, mtime: byId.get(tb.id) ?? tb.mtime, reload: (tb.reload ?? 0) + 1 } : tb)));
  }, []);

  // A tree rename moved a file (or a folder containing open files) on disk. Repoint any matching tab
  // — the renamed file itself (path + display name) and every descendant (path prefix only) — so
  // edits keep saving against the live path instead of failing as a deleted-file conflict.
  const onEntryRenamed = useCallback((from: string, to: string) => {
    setTabs((ts) =>
      ts.map((tb) => {
        if (!tb.path) return tb;
        if (tb.path === from) return { ...tb, path: to, name: to.split(/[\\/]/).filter(Boolean).pop() || tb.name };
        // Descendant of a renamed folder — reprefix the path, keeping its own name. Match either
        // separator so Windows child paths (C:\dir\file.txt) reconcile too.
        if (tb.path.startsWith(`${from}/`) || tb.path.startsWith(`${from}\\`)) {
          return { ...tb, path: `${to}${tb.path.slice(from.length)}` };
        }
        return tb;
      }),
    );
  }, []);

  // A tree delete removed a file (or a folder containing open files). Auto-close only the CLEAN
  // matching tabs (the file itself + any descendant) — a dirty tab keeps its unsaved buffer rather
  // than be silently dropped; its stale-path save then fails loudly, prompting a save-as.
  const onEntryDeleted = useCallback((deleted: string) => {
    setTabs((ts) => {
      const isGone = (p: string | null) => !!p && (p === deleted || p.startsWith(`${deleted}/`) || p.startsWith(`${deleted}\\`));
      const closeIds = new Set(ts.filter((tb) => isGone(tb.path) && !dirtyRef.current[tb.id]).map((tb) => tb.id));
      if (!closeIds.size) return ts;
      const rest = ts.filter((tb) => !closeIds.has(tb.id));
      setActive((cur) => (cur && closeIds.has(cur) ? (rest.length ? rest[rest.length - 1].id : null) : cur));
      setDirty((d) => {
        const n = { ...d };
        closeIds.forEach((id) => delete n[id]);
        return n;
      });
      return rest;
    });
  }, []);

  // Open the launch file (from the File Browser) once on mount, OR — when the File Browser's
  // "New File" launched us with a target dir — root the explorer there and start a fresh untitled
  // buffer (its first save lands in that dir).
  useEffect(() => {
    const p = typeof params?.path === 'string' ? params.path : null;
    if (p) {
      const name = (typeof params?.filename === 'string' ? params.filename : p.split('/').filter(Boolean).pop()) || p;
      if (previewOverlayKind({ kind: 'file', name, size: null })) openPreview(p, name);
      else openFile(p, name, typeof params?.mtime === 'number' ? params.mtime : null);
      return;
    }
    const dir = typeof params?.newFileDir === 'string' ? params.newFileDir : null;
    if (dir) {
      setRoot(dir);
      newFile();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const anyDirty = Object.values(dirty).some(Boolean);
  useWindowCloseGuard(windowId, anyDirty ? t('apps.editor.confirmDiscardClose') : null);

  // Reflect the active file in the window title, so the Dock + titlebar identify which file this
  // editor window holds (important when several editor windows are open). Clears to the app title
  // when nothing is open. setTitle no-ops when unchanged, so this can run on every tab change.
  useEffect(() => {
    if (windowId) wm.setTitle(windowId, tabs.find((x) => x.id === active)?.name);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowId, active, tabs]);

  const closeTab = (id: string) => {
    // Closing a dirty tab drops its in-memory buffer (only the window-level guard existed before),
    // so confirm first when there are unsaved edits.
    if (dirty[id] && !window.confirm(t('apps.editor.confirmDiscardClose'))) return;
    setTabs((ts) => {
      const rest = ts.filter((x) => x.id !== id);
      setActive((cur) => (cur === id ? (rest.length ? rest[rest.length - 1].id : null) : cur));
      return rest;
    });
    setDirty((d) => {
      const n = { ...d };
      delete n[id];
      return n;
    });
  };

  // Open Folder: pick a folder via the in-window FilePicker (no manual path typing), set it as the
  // explorer root. The picker only resolves to listable folders, so no extra validation is needed.
  const openFolder = useCallback(() => {
    setPicker({
      mode: 'open-directory',
      initialPath: root,
      onConfirm: ({ path }) => {
        setRoot(path);
        setPicker(null);
      },
    });
  }, [root]);

  // New File: open an empty untitled buffer (VS Code-style). It has no path until the first save.
  const newFile = useCallback(() => {
    const id = `t${++tabSeq.current}`;
    setTabs((ts) => [...ts, { id, path: null, name: t('apps.editor.untitled', { n: ++untitledSeq.current }), mtime: null }]);
    setActive(id);
    setCursor(null);
  }, [t]);

  // Save As (for an untitled buffer): pick a folder + filename, write create-only (so it can't
  // clobber an existing file), then re-point the tab at the chosen path. A name clash throws
  // errors.exists, which keeps the picker open with the message so the user can rename.
  const saveAs = useCallback(
    (tabId: string, text: string) => {
      setPicker({
        mode: 'save-file',
        initialPath: root,
        onConfirm: async ({ path, name }) => {
          const full = joinPath(path, name as string);
          const result = await writeFile(full, text, undefined, true);
          setTabs((ts) => ts.map((x) => (x.id === tabId ? { ...x, path: full, name: name as string, mtime: result.mtime } : x)));
          setRoot((r) => r ?? path);
          // Re-list the folder the new file landed in, so it appears in the explorer tree.
          setTreeRefresh({ path, nonce: ++treeRefreshSeq.current });
          setPicker(null);
        },
      });
    },
    [root],
  );

  // ⌘O Open Folder · ⌘N New File — only while THIS editor window holds focus (several windows can
  // be open). Capture phase + preventDefault so ⌘O doesn't fall through to the browser's open dialog.
  useEffect(() => {
    if (!windowId) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      // Only the frontmost editor window, and not while its own dialog is open. Scope by the window
      // manager's focused id (not DOM focus) so the shortcut keeps working after a dialog closes.
      if (picker || wm.focusedId !== windowId) return;
      const k = e.key.toLowerCase();
      // ⇧⌘F opens the cross-file Search view and focuses its query input. (Plain ⌘F is left to
      // Monaco's built-in in-file find widget when the editor has focus.)
      if (e.shiftKey) {
        if (k !== 'f') return;
        e.preventDefault();
        setView('search');
        setExplorerCollapsed(false); // ⇧⌘F must reveal the panel even when it's collapsed
        setSearchFocus((n) => n + 1);
        return;
      }
      if (k !== 'o' && k !== 'n') return;
      e.preventDefault();
      if (k === 'o') openFolder();
      else newFile();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [windowId, openFolder, newFile, picker, wm.focusedId]);

  // The left panel collapses (toggled by the active activity-bar icon) and resizes (drag its right
  // border). Width persists for the window's lifetime (state, not navigation).
  const [explorerCollapsed, setExplorerCollapsed] = useState(false);
  const [explorerWidth, setExplorerWidth] = useState(240);
  // Holds the in-flight drag's teardown so an unmount mid-drag (window closed while dragging) can
  // still remove the window listeners and restore the body cursor / user-select.
  const resizeTeardown = useRef<(() => void) | null>(null);
  const startResize = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = explorerWidth;
      const onMove = (ev: MouseEvent) => setExplorerWidth(Math.max(168, Math.min(520, startW + ev.clientX - startX)));
      const teardown = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', teardown);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        resizeTeardown.current = null;
      };
      resizeTeardown.current = teardown;
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', teardown);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },
    [explorerWidth],
  );
  useEffect(() => () => resizeTeardown.current?.(), []);

  const showWelcome = root == null && tabs.length === 0;
  const activeTab = tabs.find((x) => x.id === active);
  const activeName = activeTab?.name;

  return (
    <div className="relative flex h-full min-h-0 w-full flex-col bg-surface">
      <div className="flex min-h-0 flex-1">
        {/* Activity bar — Files / Search switch the left panel; the rest are inert placeholders. */}
        <div className="flex w-12 shrink-0 flex-col items-center justify-between border-r border-border bg-surface-3 py-3">
          <div className="flex flex-col items-center gap-[18px]">
            {([
              { Icon: Files, key: 'files' as const, label: t('apps.editor.explorer') },
              { Icon: Search, key: 'search' as const, label: t('apps.editor.search.title') },
            ]).map(({ Icon, key, label }) => (
              <button
                key={key}
                type="button"
                onClick={() => {
                  // Clicking the already-active view's icon toggles the panel collapsed (VS Code);
                  // switching to the other view always re-opens the panel.
                  if (view === key) {
                    setExplorerCollapsed((c) => !c);
                  } else {
                    setView(key);
                    setExplorerCollapsed(false);
                    if (key === 'search') setSearchFocus((n) => n + 1);
                  }
                }}
                aria-label={label}
                aria-pressed={view === key && !explorerCollapsed}
                title={label}
                className={clsx(
                  'relative grid h-6 w-12 place-items-center transition',
                  view === key && !explorerCollapsed ? 'text-foreground' : 'text-muted hover:text-foreground',
                )}
              >
                {view === key && !explorerCollapsed && <span className="absolute left-0 top-0 h-full w-0.5 bg-cyan" />}
                <Icon className="size-5" />
              </button>
            ))}
            {[GitBranch, Bug, Blocks].map((Icon, i) => (
              <Icon key={i} className="size-5 text-muted" />
            ))}
          </div>
          <Settings className="size-5 text-muted" />
        </div>

        {/* Left panel — Explorer (Files view) or the cross-file Search view. Collapses via the
            active activity-bar icon; drag its right border to resize. Explorer is ALWAYS present in
            Files view (design w0qoC keeps it in the welcome state). */}
        {!explorerCollapsed && (
        <div className="relative flex shrink-0 flex-col overflow-hidden border-r border-border bg-surface-2" style={{ width: explorerWidth }}>
          {view === 'search' ? (
            <EditorSearchView root={root} focusNonce={searchFocus} onOpenFolder={openFolder} onJump={onJump} onFilesChanged={reloadTabs} />
          ) : (
            <>
              <div className="flex items-center gap-0.5 px-3 pb-1 pt-2.5">
                <span className="flex-1 truncate font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('apps.editor.explorer')}</span>
                <button
                  type="button"
                  onClick={newFile}
                  title={`${t('apps.fileBrowser.newFile')} (⌘N)`}
                  aria-label={t('apps.fileBrowser.newFile')}
                  className="grid size-5 place-items-center rounded text-muted transition hover:bg-foreground/10 hover:text-foreground"
                >
                  <FilePlus className="size-3.5" />
                </button>
                <button
                  type="button"
                  onClick={openFolder}
                  title={`${t('apps.editor.openFolder')} (⌘O)`}
                  aria-label={t('apps.editor.openFolder')}
                  className="grid size-5 place-items-center rounded text-muted transition hover:bg-foreground/10 hover:text-foreground"
                >
                  <FolderOpen className="size-3.5" />
                </button>
              </div>
              {root == null ? (
                <div className="flex flex-col gap-2 px-3 py-2">
                  <div className="text-[12px] text-muted">{t('apps.editor.noFolder')}</div>
                  <button
                    type="button"
                    onClick={openFolder}
                    className="flex items-center justify-center gap-1.5 rounded-md border border-mint/40 bg-mint/[0.08] px-2.5 py-1.5 text-[12px] font-semibold text-mint transition hover:bg-mint/[0.14]"
                  >
                    <FolderOpen className="size-3.5" />
                    {t('apps.editor.openFolder')}
                  </button>
                </div>
              ) : (
                <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
                  <FileTree
                    rootPath={root}
                    rootName={root.split('/').filter(Boolean).pop() || root}
                    activePath={tabs.find((x) => x.id === active)?.path ?? null}
                    onOpenFile={onTreeOpen}
                    refreshSignal={treeRefresh}
                    onEntryRenamed={onEntryRenamed}
                    onEntryDeleted={onEntryDeleted}
                  />
                </div>
              )}
            </>
          )}
          {/* Drag handle on the right border to resize the panel (VS Code). */}
          <div
            onMouseDown={startResize}
            className="absolute right-0 top-0 z-10 h-full w-1 cursor-col-resize bg-transparent transition hover:bg-cyan/40"
            aria-hidden
          />
        </div>
        )}

        {/* Main area: welcome (nothing open) · tabs + Monaco (a file or untitled buffer open) ·
            select-a-file hint (folder open, no tab). */}
        <div className="flex min-w-0 flex-1 flex-col bg-surface">
          {showWelcome ? (
            <Welcome onOpenFolder={openFolder} onNewFile={newFile} />
          ) : (
            <>
              {tabs.length > 0 && (
                <div className="flex items-center overflow-x-auto border-b border-border bg-surface-2">
                  {tabs.map((tab) => (
                    <div
                      key={tab.id}
                      className={clsx(
                        'group/tab flex shrink-0 items-center gap-2 border-r border-border px-3 py-2 text-[12px] transition',
                        active === tab.id ? 'bg-surface text-foreground shadow-[inset_0_2px_0_0_var(--cyan)]' : 'text-muted hover:bg-foreground/[0.04]',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          if (tab.id !== active) setCursor(null);
                          setActive(tab.id);
                        }}
                        className="flex items-center gap-1.5"
                      >
                        {tab.kind === 'preview' ? (
                          previewRenderKind(tab.name) === 'image' ? <ImageIcon className="size-3.5 text-violet" /> : <FileText className="size-3.5 text-violet" />
                        ) : (
                          <CodeXml className="size-3.5 text-cyan" />
                        )}
                        {tab.name}
                        {dirty[tab.id] && <span className="size-1.5 rounded-full bg-mint" />}
                      </button>
                      <button
                        type="button"
                        onClick={() => closeTab(tab.id)}
                        aria-label={t('common.close')}
                        className="grid size-4 place-items-center rounded text-muted opacity-0 transition hover:bg-foreground/10 hover:text-foreground group-hover/tab:opacity-100"
                      >
                        <X className="size-3" strokeWidth={2.5} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="relative min-h-0 flex-1">
                {tabs.length === 0 ? (
                  <div className="grid h-full place-items-center text-[12.5px] text-muted">{t('apps.editor.selectFileHint')}</div>
                ) : (
                  tabs.map((tab) => (
                    <div key={tab.id} className={clsx('absolute inset-0', active === tab.id ? 'block' : 'hidden')}>
                      {tab.kind === 'preview' && tab.path ? (
                        <FilePreview source={{ url: contentUrl(tab.path), name: tab.name }} />
                      ) : (
                        <Suspense fallback={<div className="grid h-full place-items-center text-[12px] text-muted">{t('common.loading')}</div>}>
                          <FileEditorPane
                            path={tab.path}
                            filename={tab.name}
                            mtime={tab.mtime}
                            chromeless
                            onDirtyChange={(d) => setDirty((prev) => (prev[tab.id] === d ? prev : { ...prev, [tab.id]: d }))}
                            onCursor={active === tab.id ? (line, col) => setCursor({ line, col }) : undefined}
                            onSaveAs={(textValue) => saveAs(tab.id, textValue)}
                            reveal={reveal?.tabId === tab.id ? { line: reveal.line, column: reveal.column, endColumn: reveal.endColumn, nonce: reveal.nonce } : null}
                            reloadNonce={tab.reload}
                            saveHotkey={active === tab.id && wm.focusedId === windowId}
                          />
                        </Suspense>
                      )}
                    </div>
                  ))
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Status bar (cyan, design dnYPx). The design mock shows a git branch + problem counts on
          the left, but we have no real git/diagnostics data for an arbitrary folder — showing a
          hardcoded "master · 0 ⚠ 0" would be misleading, so those are omitted until wired. The
          right side (cursor / indentation / language) is real. */}
      <div className="flex items-center gap-3.5 bg-cyan px-3.5 py-1 font-mono text-[10.5px] font-semibold text-[#06222B]">
        {active && activeTab?.kind === 'preview' ? (
          <span className="ml-auto truncate">{t('preview.title')}</span>
        ) : active ? (
          <>
            <span className="ml-auto tabular-nums">{t('apps.editor.lineCol', { line: cursor?.line ?? 1, col: cursor?.col ?? 1 })}</span>
            <span>{t('apps.editor.spaces', { n: 2 })}</span>
            <span className="truncate">{languageLabel(activeName) ?? t('apps.editor.plainText')}</span>
          </>
        ) : (
          <span className="ml-auto truncate opacity-80">{t('apps.editor.label')}</span>
        )}
      </div>

      {picker && (
        <FilePicker
          mode={picker.mode}
          initialPath={picker.initialPath}
          defaultName={picker.defaultName}
          onCancel={() => setPicker(null)}
          onConfirm={picker.onConfirm}
        />
      )}
    </div>
  );
};

const Welcome: React.FC<{ onOpenFolder: () => void; onNewFile: () => void }> = ({ onOpenFolder, onNewFile }) => {
  const { t } = useTranslation();
  const actions: { Icon: typeof FolderOpen; color: string; label: string; onClick: () => void; sc?: string }[] = [
    { Icon: FolderOpen, color: 'text-cyan', label: t('apps.editor.openFolder'), onClick: onOpenFolder, sc: '⌘O' },
    { Icon: FilePlus, color: 'text-mint', label: t('apps.fileBrowser.newFile'), onClick: onNewFile, sc: '⌘N' },
  ];
  return (
    <div className="grid min-w-0 flex-1 place-items-center bg-surface p-10">
      <div className="flex w-[440px] max-w-full flex-col gap-6">
        <div className="flex items-center gap-3.5">
          <span className="grid size-14 place-items-center rounded-2xl border border-cyan/60 bg-cyan-soft">
            <CodeXml className="size-7 text-cyan" />
          </span>
          <div className="flex flex-col gap-1">
            <div className="text-[24px] font-bold text-foreground">{t('apps.editor.label')}</div>
            <div className="text-[12.5px] text-muted">{t('apps.editor.welcomeSub')}</div>
          </div>
        </div>
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.1em] text-muted">{t('apps.editor.start')}</div>
        <div className="flex flex-col gap-1.5">
          {actions.map((a) => (
            <button key={a.label} type="button" onClick={a.onClick} className="flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-left transition hover:bg-foreground/[0.06]">
              <a.Icon className={clsx('size-4', a.color)} />
              <span className="text-[13.5px] font-semibold text-foreground">{a.label}</span>
              <span className="flex-1" />
              {a.sc && <span className="font-mono text-[11px] text-muted">{a.sc}</span>}
            </button>
          ))}
        </div>
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.1em] text-muted">{t('apps.editor.recent')}</div>
        <div className="flex items-center gap-2 text-[12.5px] text-muted">
          <Clock className="size-3.5" /> {t('apps.editor.noRecent')}
        </div>
      </div>
    </div>
  );
};
