import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { Blocks, Bug, Clock, CodeXml, FilePlus, Files, FolderOpen, GitBranch, Search, Settings, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useWindowCloseGuard, useWindowManager } from '../../context/WindowManagerContext';
import { downloadFile, fileBrowserErrorMessage, fileMeta, isPlainEntryName, joinPath, listDir, parentDir, writeFile, type FsEntry } from '../../lib/filesApi';
import { isEditableFile } from '../../lib/filePreview';
import { FileTree } from './FileTree';

const FileEditorPane = lazy(() => import('./FileEditorPane').then((m) => ({ default: m.FileEditorPane })));

// A tab carries the file's mtime captured at open time, so saving stays conditional on it
// (writeFile's expected_mtime) and the backend can surface a conflict instead of silently
// clobbering newer on-disk content.
type Tab = { path: string; name: string; mtime: number | null };

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
// editor tabs + Monaco + cyan status bar; a VS-Code-style welcome when nothing is open.
// Monaco is fully integrated here. Files opens a file via openApp('editor', { params: { path } }).
export const EditorApp: React.FC<{ windowId?: string; params?: Record<string, unknown> }> = ({ windowId, params }) => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const [root, setRoot] = useState<string | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [dirty, setDirty] = useState<Record<string, boolean>>({});
  const [cursor, setCursor] = useState<{ line: number; col: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const openFile = useCallback((path: string, name: string, mtime: number | null) => {
    setRoot((r) => r ?? parentDir(path));
    setTabs((ts) => (ts.some((x) => x.path === path) ? ts : [...ts, { path, name, mtime }]));
    setCursor(null);
    setActive(path);
  }, []);

  // The explorer tree emits a clicked entry. Gate it like the File Browser: only a regular,
  // supported, within-cap file opens in Monaco; a symlink (the backend refuses symlink writes),
  // oversized, or unsupported/binary entry downloads instead. The download branch runs BEFORE any
  // await so the click's user activation survives (Safari/iOS would block the popup otherwise).
  const onTreeOpen = useCallback(
    async (path: string, entry: FsEntry) => {
      if (!isEditableFile(entry)) {
        downloadFile(path);
        return;
      }
      // Fetch fresh metadata: gives the save-baseline mtime AND re-validates (the file may have
      // grown past the cap or become a symlink since it was listed) → download if no longer editable.
      try {
        const m = await fileMeta(path);
        if (!isEditableFile(m)) {
          downloadFile(path);
          return;
        }
        openFile(path, entry.name, m.mtime);
      } catch {
        openFile(path, entry.name, entry.mtime);
      }
    },
    [openFile],
  );

  // Open the launch file (from the File Browser) once on mount.
  useEffect(() => {
    const p = typeof params?.path === 'string' ? params.path : null;
    if (p)
      openFile(
        p,
        (typeof params?.filename === 'string' ? params.filename : p.split('/').filter(Boolean).pop()) || p,
        typeof params?.mtime === 'number' ? params.mtime : null,
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const anyDirty = Object.values(dirty).some(Boolean);
  useWindowCloseGuard(windowId, anyDirty ? t('apps.editor.confirmDiscardClose') : null);

  const closeTab = (path: string) => {
    // Closing a dirty tab drops its in-memory buffer (only the window-level guard existed before),
    // so confirm first when there are unsaved edits.
    if (dirty[path] && !window.confirm(t('apps.editor.confirmDiscardClose'))) return;
    setTabs((ts) => {
      const rest = ts.filter((x) => x.path !== path);
      setActive((cur) => (cur === path ? (rest.length ? rest[rest.length - 1].path : null) : cur));
      return rest;
    });
    setDirty((d) => {
      const n = { ...d };
      delete n[path];
      return n;
    });
  };

  // Prompt for + validate a folder, set it as the explorer root, and RETURN it (or null) so
  // callers like New File can continue the flow with the chosen folder.
  const openFolder = async (): Promise<string | null> => {
    const p = window.prompt(t('apps.editor.openFolderPrompt'));
    const path = p?.trim();
    if (!path) return null;
    // Validate it's a listable directory before swapping the explorer root, so a typo doesn't
    // leave the tree stuck on a bad/non-existent path.
    try {
      await listDir(path);
      setError(null);
      setRoot(path);
      return path;
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.listFailed')));
      return null;
    }
  };
  const newFile = async () => {
    // From the welcome state there's no folder yet: prompt for one and CONTINUE creating the file
    // in it (rather than behaving like a bare "Open Folder").
    const dir = root ?? (await openFolder());
    if (!dir) return;
    const raw = window.prompt(t('apps.fileBrowser.newFilePrompt'));
    if (raw == null) return;
    const name = raw.trim();
    if (name === '') return;
    if (!isPlainEntryName(name)) {
      setError(t('apps.fileBrowser.errors.invalid_name'));
      return;
    }
    const full = joinPath(dir, name);
    try {
      // create-only: the backend atomically refuses (errors.exists) if the name is taken, so a
      // typo can never truncate an existing file.
      const result = await writeFile(full, '', undefined, true);
      openFile(full, name, result.mtime);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
    }
  };

  const ACTIVITY = [Files, Search, GitBranch, Bug, Blocks];

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-surface">
      <div className="flex min-h-0 flex-1">
        {/* Activity bar */}
        <div className="flex w-12 shrink-0 flex-col items-center justify-between border-r border-border bg-surface-3 py-3">
          <div className="flex flex-col items-center gap-[18px]">
            {ACTIVITY.map((Icon, i) => (
              <Icon key={i} className={clsx('size-5', i === 0 ? 'text-foreground' : 'text-muted')} />
            ))}
          </div>
          <Settings className="size-5 text-muted" />
        </div>

        {/* Explorer — ALWAYS present (design w0qoC keeps it in the welcome state): an
            empty "No folder opened + Open Folder" state when no folder, else the file tree. */}
        <div className="flex w-[220px] shrink-0 flex-col overflow-hidden border-r border-border bg-surface-2">
          <div className="px-3 pb-1 pt-2.5 font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('apps.editor.explorer')}</div>
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
              {/* Folder-validation errors (bad path / permission) must surface here too — the
                  main-area error banner only renders once a folder is open. */}
              {error && (
                <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-2 py-1.5 text-[11.5px] text-destructive">
                  {error}
                </div>
              )}
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
              <FileTree rootPath={root} rootName={root.split('/').filter(Boolean).pop() || root} activePath={active} onOpenFile={onTreeOpen} />
            </div>
          )}
        </div>

        {/* Main area: welcome (no folder) · tabs + Monaco (file open) · select-a-file hint (folder, no file). */}
        <div className="flex min-w-0 flex-1 flex-col bg-[#0b0b14]">
          {root == null ? (
            <Welcome onOpenFolder={openFolder} onNewFile={newFile} onOpenFiles={() => wm.openApp('files')} />
          ) : (
            <>
              {tabs.length > 0 && (
                <div className="flex items-center overflow-x-auto border-b border-border bg-surface-2">
                  {tabs.map((tab) => (
                    <div
                      key={tab.path}
                      className={clsx(
                        'group/tab flex shrink-0 items-center gap-2 border-r border-border px-3 py-2 text-[12px] transition',
                        active === tab.path ? 'bg-[#0b0b14] text-foreground shadow-[inset_0_2px_0_0_var(--cyan)]' : 'text-muted hover:bg-foreground/[0.04]',
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          if (tab.path !== active) setCursor(null);
                          setActive(tab.path);
                        }}
                        className="flex items-center gap-1.5"
                      >
                        <CodeXml className="size-3.5 text-cyan" />
                        {tab.name}
                        {dirty[tab.path] && <span className="size-1.5 rounded-full bg-mint" />}
                      </button>
                      <button
                        type="button"
                        onClick={() => closeTab(tab.path)}
                        aria-label={t('common.close')}
                        className="grid size-4 place-items-center rounded text-muted opacity-0 transition hover:bg-foreground/10 hover:text-foreground group-hover/tab:opacity-100"
                      >
                        <X className="size-3" strokeWidth={2.5} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {error && <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive">{error}</div>}

              <div className="relative min-h-0 flex-1">
                {tabs.length === 0 ? (
                  <div className="grid h-full place-items-center text-[12.5px] text-muted">{t('apps.editor.selectFileHint')}</div>
                ) : (
                  tabs.map((tab) => (
                    <div key={tab.path} className={clsx('absolute inset-0', active === tab.path ? 'block' : 'hidden')}>
                      <Suspense fallback={<div className="grid h-full place-items-center text-[12px] text-muted">{t('common.loading')}</div>}>
                        <FileEditorPane
                          path={tab.path}
                          filename={tab.name}
                          mtime={tab.mtime}
                          chromeless
                          onDirtyChange={(d) => setDirty((prev) => (prev[tab.path] === d ? prev : { ...prev, [tab.path]: d }))}
                          onCursor={active === tab.path ? (line, col) => setCursor({ line, col }) : undefined}
                        />
                      </Suspense>
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
        {active ? (
          <>
            <span className="ml-auto tabular-nums">{t('apps.editor.lineCol', { line: cursor?.line ?? 1, col: cursor?.col ?? 1 })}</span>
            <span>{t('apps.editor.spaces', { n: 2 })}</span>
            <span className="truncate">{languageLabel(tabs.find((x) => x.path === active)?.name) ?? t('apps.editor.plainText')}</span>
          </>
        ) : (
          <span className="ml-auto truncate opacity-80">{t('apps.editor.label')}</span>
        )}
      </div>
    </div>
  );
};

const Welcome: React.FC<{ onOpenFolder: () => void; onNewFile: () => void; onOpenFiles: () => void }> = ({ onOpenFolder, onNewFile, onOpenFiles }) => {
  const { t } = useTranslation();
  const actions: { Icon: typeof FolderOpen; color: string; label: string; onClick: () => void; sc?: string }[] = [
    { Icon: FolderOpen, color: 'text-cyan', label: t('apps.editor.openFolder'), onClick: onOpenFolder, sc: '⌘O' },
    { Icon: FilePlus, color: 'text-mint', label: t('apps.fileBrowser.newFile'), onClick: onNewFile, sc: '⌘N' },
    { Icon: Files, color: 'text-violet', label: t('apps.editor.browseFiles'), onClick: onOpenFiles },
  ];
  return (
    <div className="grid min-w-0 flex-1 place-items-center bg-[#0b0b14] p-10">
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
