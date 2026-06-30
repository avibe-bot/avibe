import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Braces,
  ChevronDown,
  ChevronRight,
  FileCode2,
  FilePlus,
  FileText,
  File as FileIcon,
  FolderPlus,
  Hash,
  Image as ImageIcon,
  Loader2,
  Pencil,
  Trash2,
} from 'lucide-react';
import clsx from 'clsx';

import {
  deletePath,
  fileBrowserErrorMessage,
  isPlainEntryName,
  joinPath,
  listDir,
  makeDir,
  parentDir,
  renamePath,
  writeFile,
  type FsEntry,
} from '../../lib/filesApi';

// Icon + accent per file extension (matches the explorer in design dnYPx).
const EXT: Record<string, { Icon: typeof FileIcon; color: string }> = {
  ts: { Icon: FileCode2, color: 'var(--cyan)' },
  tsx: { Icon: FileCode2, color: 'var(--cyan)' },
  js: { Icon: FileCode2, color: 'var(--gold)' },
  jsx: { Icon: FileCode2, color: 'var(--gold)' },
  json: { Icon: Braces, color: 'var(--gold)' },
  css: { Icon: Hash, color: 'var(--violet)' },
  scss: { Icon: Hash, color: 'var(--violet)' },
  md: { Icon: FileText, color: 'var(--mint)' },
  png: { Icon: ImageIcon, color: 'var(--muted)' },
  jpg: { Icon: ImageIcon, color: 'var(--muted)' },
  svg: { Icon: ImageIcon, color: 'var(--muted)' },
};

function fileIcon(e: FsEntry) {
  return EXT[e.ext?.toLowerCase()] ?? { Icon: FileIcon, color: 'var(--muted)' };
}

function sortEntries(entries: FsEntry[]): FsEntry[] {
  return [...entries].sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'dir' ? -1 : 1));
}

// One inline edit at a time across the whole tree: creating a new entry inside `dir`, or renaming
// `target` inside `dir`.
type EditSession = { dir: string; mode: 'new-file' | 'new-folder' | 'rename'; target?: string };
// A cursor-positioned context menu. `entry` is null for the blank/root background (offers New only).
type MenuState = { x: number; y: number; dir: string; entry: FsEntry | null };

type TreeCtx = {
  activePath: string | null;
  showHidden: boolean;
  onOpenFile: (path: string, entry: FsEntry) => void;
  versionOf: (path: string) => number;
  edit: EditSession | null;
  startEdit: (s: EditSession) => void;
  cancelEdit: () => void;
  commitEdit: (value: string) => void;
  openMenu: (ev: React.MouseEvent, dir: string, entry: FsEntry | null) => void;
};

const Ctx = createContext<TreeCtx | null>(null);
const useTree = (): TreeCtx => {
  const c = useContext(Ctx);
  if (!c) throw new Error('FileTree context missing');
  return c;
};

// Shared inline name editor for both new-entry and rename rows: autofocus, select (rename keeps the
// name so it can be tweaked), Enter commits, Esc/blur cancels.
const InlineNameInput: React.FC<{ initial: string; placeholder?: string; onCommit: (v: string) => void; onCancel: () => void }> = ({
  initial,
  placeholder,
  onCommit,
  onCancel,
}) => {
  const [value, setValue] = useState(initial);
  // Cancel on blur UNLESS we just committed (Enter), else committing also fires blur → double-fire.
  const committed = useRef(false);
  return (
    <input
      autoFocus
      value={value}
      placeholder={placeholder}
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          committed.current = true;
          onCommit(value);
        } else if (e.key === 'Escape') {
          e.preventDefault();
          committed.current = true;
          onCancel();
        }
      }}
      onBlur={() => {
        if (!committed.current) onCancel();
      }}
      className="min-w-0 flex-1 rounded border border-cyan bg-surface px-1 py-0 text-[12.5px] text-foreground placeholder:text-muted focus:outline-none"
    />
  );
};

const Row: React.FC<{
  depth: number;
  onClick?: () => void;
  onContextMenu?: (e: React.MouseEvent) => void;
  active?: boolean;
  children: React.ReactNode;
}> = ({ depth, onClick, onContextMenu, active, children }) => (
  <button
    type="button"
    onClick={onClick}
    onContextMenu={onContextMenu}
    style={{ paddingLeft: 8 + depth * 14 }}
    className={clsx(
      'flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-[12.5px] transition',
      active ? 'bg-cyan-soft text-foreground' : 'text-foreground hover:bg-foreground/[0.05]',
    )}
  >
    {children}
  </button>
);

const FileRow: React.FC<{ path: string; dir: string; entry: FsEntry; depth: number }> = ({ path, dir, entry, depth }) => {
  const tree = useTree();
  const { Icon, color } = fileIcon(entry);
  const renaming = tree.edit?.mode === 'rename' && tree.edit.target === entry.name && tree.edit.dir === dir;
  if (renaming) {
    return (
      <div style={{ paddingLeft: 8 + depth * 14 }} className="flex items-center gap-1.5 py-1 pr-2">
        <Icon className="size-3.5 shrink-0" style={{ color }} />
        <InlineNameInput initial={entry.name} onCommit={tree.commitEdit} onCancel={tree.cancelEdit} />
      </div>
    );
  }
  return (
    <Row depth={depth} active={tree.activePath === path} onClick={() => tree.onOpenFile(path, entry)} onContextMenu={(e) => tree.openMenu(e, dir, entry)}>
      <Icon className="size-3.5 shrink-0" style={{ color }} />
      <span className="truncate">{entry.name}</span>
    </Row>
  );
};

const Dir: React.FC<{ path: string; name: string; depth: number }> = ({ path, name, depth }) => {
  const { t } = useTranslation();
  const tree = useTree();
  const [open, setOpen] = useState(depth === 0);
  const [entries, setEntries] = useState<FsEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const version = tree.versionOf(path);
  // `menu.dir` is always the PARENT of the clicked entry (consistent with file rows), so new/rename/
  // delete resolve the same way for files and folders. The ROOT row (depth 0) is special: it has no
  // meaningful parent here and must not be renamed/deleted, so it opens the menu as blank space
  // (entry=null, dir=path) — offering only New File/Folder INSIDE the root.
  const isRoot = depth === 0;
  const parent = parentDir(path);
  const renamingSelf = !isRoot && tree.edit?.mode === 'rename' && tree.edit.dir === parent && tree.edit.target === name;

  // Drop out-of-order responses: a post-mutation reload (bump) can race an in-flight pre-mutation
  // listDir for the same folder, and the stale one landing last would hide a just-created item or
  // resurrect a renamed/deleted row. Only the latest request's result is applied.
  const loadSeq = useRef(0);
  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    listDir(path, tree.showHidden)
      .then((r) => {
        if (seq === loadSeq.current) setEntries(sortEntries(r.entries));
      })
      .catch((e: unknown) => {
        if (seq !== loadSeq.current) return;
        // Don't render an unlistable folder as an empty one — surface the failure so the user knows
        // entries are hidden by an error (permission/deletion/transient), not absent.
        setEntries([]);
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.listFailed')));
      })
      .finally(() => {
        if (seq === loadSeq.current) setLoading(false);
      });
  }, [path, tree.showHidden, t]);

  // Load when opened, when the hidden toggle flips (load identity changes), and when this dir's
  // version bumps (a mutation re-lists it). Other dirs keep their cached entries.
  useEffect(() => {
    if (open) load();
  }, [open, load, version]);

  // A new-entry edit targeting this collapsed dir must expand it so the inline input is visible.
  const wantsExpand = tree.edit?.dir === path && tree.edit.mode !== 'rename';
  useEffect(() => {
    if (wantsExpand) setOpen(true);
  }, [wantsExpand]);

  const newHere = tree.edit?.dir === path && tree.edit.mode !== 'rename';

  return (
    <div>
      {depth >= 0 &&
        (renamingSelf ? (
          <div style={{ paddingLeft: 8 + depth * 14 }} className="flex items-center gap-1.5 py-1 pr-2">
            <ChevronDown className="size-3.5 shrink-0 text-muted" />
            <InlineNameInput initial={name} onCommit={tree.commitEdit} onCancel={tree.cancelEdit} />
          </div>
        ) : (
          <Row
            depth={depth}
            onClick={() => setOpen((o) => !o)}
            onContextMenu={(e) =>
              isRoot ? tree.openMenu(e, path, null) : tree.openMenu(e, parent, { name, kind: 'dir', size: null, mtime: null, ext: '' })
            }
          >
            {open ? <ChevronDown className="size-3.5 shrink-0 text-muted" /> : <ChevronRight className="size-3.5 shrink-0 text-muted" />}
            <span className="truncate font-medium text-muted">{name}</span>
          </Row>
        ))}
      {open && (
        <div>
          {newHere && (
            <div style={{ paddingLeft: 8 + (depth + 1) * 14 }} className="flex items-center gap-1.5 py-1 pr-2">
              {tree.edit?.mode === 'new-folder' ? (
                <ChevronRight className="size-3.5 shrink-0 text-muted" />
              ) : (
                <FileIcon className="size-3.5 shrink-0 text-muted" />
              )}
              <InlineNameInput
                initial=""
                placeholder={t(tree.edit?.mode === 'new-folder' ? 'apps.fileBrowser.newFolderPlaceholder' : 'apps.fileBrowser.newFilePrompt')}
                onCommit={tree.commitEdit}
                onCancel={tree.cancelEdit}
              />
            </div>
          )}
          {loading && entries === null && (
            <div style={{ paddingLeft: 8 + (depth + 1) * 14 }} className="py-1">
              <Loader2 className="size-3.5 animate-spin text-muted" />
            </div>
          )}
          {error && (
            // Clickable so a transient failure (perms fixed, dir recreated, network/auth hiccup) is
            // retryable in place.
            <button
              type="button"
              onClick={load}
              style={{ paddingLeft: 8 + (depth + 1) * 14 }}
              className="flex w-full items-center gap-1 truncate py-1 pr-2 text-left text-[11.5px] text-destructive transition hover:bg-destructive/[0.06]"
              title={`${error} — ${t('common.retry')}`}
            >
              <span className="truncate">{error}</span>
            </button>
          )}
          {entries?.map((e) =>
            e.kind === 'dir' ? (
              <Dir key={e.name} path={joinPath(path, e.name)} name={e.name} depth={depth + 1} />
            ) : (
              <FileRow key={e.name} path={joinPath(path, e.name)} dir={path} entry={e} depth={depth + 1} />
            ),
          )}
        </div>
      )}
    </div>
  );
};

const MenuItem: React.FC<{ icon: React.ReactNode; label: string; danger?: boolean; onClick: () => void }> = ({ icon, label, danger, onClick }) => (
  <button
    type="button"
    role="menuitem"
    onClick={onClick}
    className={clsx(
      'flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-[12.5px] transition',
      danger ? 'text-destructive hover:bg-destructive/[0.1]' : 'text-foreground hover:bg-cyan-soft',
    )}
  >
    <span className="grid size-4 shrink-0 place-items-center">{icon}</span>
    {label}
  </button>
);

// The VS-Code-style explorer tree (design dnYPx): a root folder whose subfolders lazily expand via
// listDir. Reused by the Editor IDE; emits file opens upward. Owns inline new-file/new-folder/rename
// + delete via a right-click menu, and re-lists the affected folder after each mutation.
export const FileTree: React.FC<{
  rootPath: string;
  rootName: string;
  activePath: string | null;
  showHidden?: boolean;
  onOpenFile: (path: string, entry: FsEntry) => void;
  /** Bumped by the editor after a save-as creates a file, so the tree re-lists that folder. */
  refreshSignal?: { path: string; nonce: number } | null;
  /** A rename happened in the tree (old → new absolute path); the editor repoints any open tab for
   *  that file or its descendants so saves don't fail against the removed path. */
  onEntryRenamed?: (from: string, to: string) => void;
  /** An entry was deleted from the tree (absolute path); the editor reconciles tabs for that file or
   *  its descendants. */
  onEntryDeleted?: (path: string) => void;
}> = ({ rootPath, rootName, activePath, showHidden = false, onOpenFile, refreshSignal, onEntryRenamed, onEntryDeleted }) => {
  const { t } = useTranslation();
  const [versions, setVersions] = useState<Record<string, number>>({});
  const [edit, setEdit] = useState<EditSession | null>(null);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editRef = useRef<EditSession | null>(null);
  editRef.current = edit;

  const bump = useCallback((path: string) => setVersions((v) => ({ ...v, [path]: (v[path] ?? 0) + 1 })), []);
  const versionOf = useCallback((path: string) => versions[path] ?? 0, [versions]);

  // External refresh (editor save-as created a file in `path`).
  useEffect(() => {
    if (refreshSignal?.path) bump(refreshSignal.path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal?.nonce]);

  const startEdit = useCallback((s: EditSession) => {
    setError(null);
    setEdit(s);
  }, []);
  const cancelEdit = useCallback(() => setEdit(null), []);

  const commitEdit = useCallback(
    async (value: string) => {
      const session = editRef.current;
      if (!session) return;
      const name = value.trim();
      if (name === '') {
        setEdit(null);
        return;
      }
      if (!isPlainEntryName(name)) {
        setError(t('apps.fileBrowser.errors.invalid_name'));
        return;
      }
      try {
        if (session.mode === 'rename') {
          if (name === session.target) {
            setEdit(null);
            return;
          }
          const from = joinPath(session.dir, session.target as string);
          const res = await renamePath(from, name);
          onEntryRenamed?.(from, res.path);
        } else if (session.mode === 'new-folder') {
          await makeDir(joinPath(session.dir, name));
        } else {
          // create-only: the backend atomically refuses a name clash, so this can't clobber a file.
          await writeFile(joinPath(session.dir, name), '', undefined, true);
        }
        setEdit(null);
        setError(null);
        bump(session.dir);
      } catch (e: unknown) {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.saveFailed')));
      }
    },
    [t, bump, onEntryRenamed],
  );

  const openMenu = useCallback((ev: React.MouseEvent, dir: string, entry: FsEntry | null) => {
    ev.preventDefault();
    ev.stopPropagation();
    setMenu({ x: ev.clientX, y: ev.clientY, dir, entry });
  }, []);
  const closeMenu = useCallback(() => setMenu(null), []);

  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenu(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [menu]);

  const removeEntry = useCallback(
    async (dir: string, entry: FsEntry) => {
      setMenu(null);
      if (!window.confirm(t('apps.fileBrowser.confirmDelete', { name: entry.name }))) return;
      const full = joinPath(dir, entry.name);
      try {
        await deletePath(full, entry.kind === 'dir');
        setError(null);
        bump(dir);
        onEntryDeleted?.(full);
      } catch (e: unknown) {
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.deleteFailed')));
      }
    },
    [t, bump, onEntryDeleted],
  );

  const ctx = useMemo<TreeCtx>(
    () => ({ activePath, showHidden, onOpenFile, versionOf, edit, startEdit, cancelEdit, commitEdit, openMenu }),
    [activePath, showHidden, onOpenFile, versionOf, edit, startEdit, cancelEdit, commitEdit, openMenu],
  );

  // Clamp the menu inside the viewport (the tree scrolls, so a cursor-positioned fixed menu near the
  // bottom/right edge would otherwise overflow). Width/height are estimates — good enough to nudge.
  const MENU_W = 196;
  const menuItemCount = menu ? (menu.entry ? (menu.entry.kind === 'dir' ? 4 : 3) : 2) : 0;
  const MENU_H = menuItemCount * 34 + 12;
  const menuX = menu ? Math.min(menu.x, window.innerWidth - MENU_W - 8) : 0;
  const menuY = menu ? Math.min(menu.y, window.innerHeight - MENU_H - 8) : 0;

  return (
    <Ctx.Provider value={ctx}>
      {/* The whole tree area is a context-menu target: right-clicking empty space offers New in the
          root folder. */}
      <div onContextMenu={(e) => openMenu(e, rootPath, null)}>
        {error && (
          <div className="mx-1 mb-1 flex items-start gap-1 rounded border border-destructive/40 bg-destructive/[0.06] px-2 py-1 text-[11px] text-destructive">
            <span className="min-w-0 flex-1">{error}</span>
            <button type="button" onClick={() => setError(null)} className="shrink-0 font-bold">
              ×
            </button>
          </div>
        )}
        <Dir key={rootPath} path={rootPath} name={rootName} depth={0} />
      </div>

      {menu && (
        <>
          <div className="fixed inset-0 z-40" onClick={closeMenu} onContextMenu={(e) => { e.preventDefault(); closeMenu(); }} aria-hidden />
          <div
            role="menu"
            style={{ left: menuX, top: menuY, width: MENU_W }}
            className="fixed z-50 rounded-lg border border-border bg-surface-3 p-1 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
          >
            {menu.entry && menu.entry.kind === 'file' && (
              <MenuItem
                icon={<FileText className="size-3.5 text-cyan" />}
                label={t('apps.fileBrowser.open')}
                onClick={() => {
                  const e = menu.entry as FsEntry;
                  onOpenFile(joinPath(menu.dir, e.name), e);
                  closeMenu();
                }}
              />
            )}
            {(!menu.entry || menu.entry.kind === 'dir') && (
              <>
                <MenuItem
                  icon={<FilePlus className="size-3.5 text-mint" />}
                  label={t('apps.fileBrowser.newFile')}
                  onClick={() => {
                    startEdit({ dir: menu.entry ? joinPath(menu.dir, menu.entry.name) : menu.dir, mode: 'new-file' });
                    closeMenu();
                  }}
                />
                <MenuItem
                  icon={<FolderPlus className="size-3.5 text-gold" />}
                  label={t('apps.fileBrowser.newFolder')}
                  onClick={() => {
                    startEdit({ dir: menu.entry ? joinPath(menu.dir, menu.entry.name) : menu.dir, mode: 'new-folder' });
                    closeMenu();
                  }}
                />
              </>
            )}
            {menu.entry && (
              <>
                <MenuItem
                  icon={<Pencil className="size-3.5" />}
                  label={t('apps.fileBrowser.rename')}
                  onClick={() => {
                    const e = menu.entry as FsEntry;
                    startEdit({ dir: menu.dir, mode: 'rename', target: e.name });
                    closeMenu();
                  }}
                />
                <MenuItem
                  icon={<Trash2 className="size-3.5" />}
                  label={t('apps.fileBrowser.delete')}
                  danger
                  onClick={() => removeEntry(menu.dir, menu.entry as FsEntry)}
                />
              </>
            )}
          </div>
        </>
      )}
    </Ctx.Provider>
  );
};
