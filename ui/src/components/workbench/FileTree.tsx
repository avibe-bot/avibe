import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Braces, ChevronDown, ChevronRight, FileCode2, FileText, File as FileIcon, Hash, Image as ImageIcon, Loader2 } from 'lucide-react';
import clsx from 'clsx';

import { fileBrowserErrorMessage, joinPath, listDir, type FsEntry } from '../../lib/filesApi';

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

const Row: React.FC<{ depth: number; onClick: () => void; active?: boolean; children: React.ReactNode }> = ({ depth, onClick, active, children }) => (
  <button
    type="button"
    onClick={onClick}
    style={{ paddingLeft: 8 + depth * 14 }}
    className={clsx(
      'flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-[12.5px] transition',
      active ? 'bg-cyan-soft text-foreground' : 'text-foreground hover:bg-foreground/[0.05]',
    )}
  >
    {children}
  </button>
);

const Dir: React.FC<{ path: string; name: string; depth: number; activePath: string | null; showHidden: boolean; onOpenFile: (path: string, entry: FsEntry) => void }> = ({
  path,
  name,
  depth,
  activePath,
  showHidden,
  onOpenFile,
}) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(depth === 0);
  const [entries, setEntries] = useState<FsEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listDir(path, showHidden)
      .then((r) => setEntries(sortEntries(r.entries)))
      .catch((e: unknown) => {
        // Don't render an unlistable folder as an empty one — surface the failure so the user
        // knows entries are hidden by an error (permission/deletion/transient), not absent.
        setEntries([]);
        setError(fileBrowserErrorMessage(e, t, t('apps.fileBrowser.errors.listFailed')));
      })
      .finally(() => setLoading(false));
  }, [path, showHidden, t]);

  useEffect(() => {
    if (open && entries === null) load();
  }, [open, entries, load]);
  // Re-list an open folder when the hidden toggle flips.
  useEffect(() => {
    if (open) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showHidden]);

  return (
    <div>
      {depth >= 0 && (
        <Row depth={depth} onClick={() => setOpen((o) => !o)}>
          {open ? <ChevronDown className="size-3.5 shrink-0 text-muted" /> : <ChevronRight className="size-3.5 shrink-0 text-muted" />}
          <span className="truncate font-medium text-muted">{name}</span>
        </Row>
      )}
      {open && (
        <div>
          {loading && entries === null && (
            <div style={{ paddingLeft: 8 + (depth + 1) * 14 }} className="py-1">
              <Loader2 className="size-3.5 animate-spin text-muted" />
            </div>
          )}
          {error && (
            // Clickable so a transient failure (perms fixed, dir recreated, network/auth hiccup) is
            // retryable in place — storing [] on error would otherwise wedge the entries===null
            // load guard and keep showing the stale error until the whole tree remounts.
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
              <Dir key={e.name} path={joinPath(path, e.name)} name={e.name} depth={depth + 1} activePath={activePath} showHidden={showHidden} onOpenFile={onOpenFile} />
            ) : (
              (() => {
                const full = joinPath(path, e.name);
                const { Icon, color } = fileIcon(e);
                return (
                  <Row key={e.name} depth={depth + 1} active={activePath === full} onClick={() => onOpenFile(full, e)}>
                    <Icon className="size-3.5 shrink-0" style={{ color }} />
                    <span className="truncate">{e.name}</span>
                  </Row>
                );
              })()
            ),
          )}
        </div>
      )}
    </div>
  );
};

// The VS-Code-style explorer tree (design dnYPx): a root folder whose subfolders lazily
// expand via listDir. Reused by the Editor IDE; emits file opens upward.
export const FileTree: React.FC<{ rootPath: string; rootName: string; activePath: string | null; showHidden?: boolean; onOpenFile: (path: string, entry: FsEntry) => void }> = ({
  rootPath,
  rootName,
  activePath,
  showHidden = false,
  onOpenFile,
}) => (
  // key on rootPath so changing the opened folder remounts the tree with fresh state instead of
  // leaving the previous folder's expanded entries behind.
  <Dir key={rootPath} path={rootPath} name={rootName} depth={0} activePath={activePath} showHidden={showHidden} onOpenFile={onOpenFile} />
);
