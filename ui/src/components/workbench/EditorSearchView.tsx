import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  CaseSensitive,
  ChevronDown,
  ChevronRight,
  CodeXml,
  CornerDownRight,
  FolderOpen,
  Loader2,
  Regex,
  ReplaceAll,
  Undo2,
  WholeWord,
} from 'lucide-react';
import clsx from 'clsx';

import {
  fileBrowserErrorMessage,
  replaceInFiles,
  searchFiles,
  undoReplace,
  type SearchFileResult,
  type SearchMatch,
  type SearchResponse,
} from '../../lib/filesApi';

type Props = {
  /** Absolute folder the search is rooted at (the editor's open folder), or null. */
  root: string | null;
  /** Bumped by ⇧⌘F / the activity-bar click to focus + select the query input. */
  focusNonce: number;
  onOpenFolder: () => void;
  onJump: (path: string, line: number, col: number, endCol: number) => void;
};

// A small toggle button (Aa / whole-word / regex) inside the search field.
const Toggle: React.FC<{ on: boolean; label: string; onClick: () => void; children: React.ReactNode }> = ({ on, label, onClick, children }) => (
  <button
    type="button"
    aria-pressed={on}
    title={label}
    aria-label={label}
    onClick={onClick}
    className={clsx('grid size-5 place-items-center rounded transition', on ? 'bg-cyan-soft text-cyan' : 'text-muted hover:bg-foreground/10 hover:text-foreground')}
  >
    {children}
  </button>
);

export const EditorSearchView: React.FC<Props> = ({ root, focusNonce, onOpenFolder, onJump }) => {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [replacement, setReplacement] = useState('');
  const [showReplace, setShowReplace] = useState(false);
  const [showScope, setShowScope] = useState(false);
  const [regex, setRegex] = useState(false);
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);
  const [include, setInclude] = useState('');
  const [exclude, setExclude] = useState('');
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [undo, setUndo] = useState<{ token: string; files: number; total: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusNonce]);

  // Debounced search whenever the query, options, or scope change (or a replace/undo asks for a
  // refresh). An AbortController cancels the in-flight request so fast typing doesn't race.
  useEffect(() => {
    if (!root || !query) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const ctrl = new AbortController();
    const handle = window.setTimeout(() => {
      searchFiles(root, query, { regex, caseSensitive, wholeWord, include, exclude }, ctrl.signal)
        .then((res) => {
          setData(res);
          setError(null);
        })
        .catch((e: unknown) => {
          if ((e as { name?: string }).name === 'AbortError') return;
          setError(fileBrowserErrorMessage(e, t, t('apps.editor.search.failed')));
          setData(null);
        })
        .finally(() => setLoading(false));
    }, 300);
    return () => {
      window.clearTimeout(handle);
      ctrl.abort();
    };
  }, [root, query, regex, caseSensitive, wholeWord, include, exclude, refresh, t]);

  const toggleCollapse = useCallback((path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const doReplace = useCallback(async () => {
    if (!root || !query || busy || !data?.results.length) return;
    setBusy(true);
    setError(null);
    try {
      const res = await replaceInFiles(root, query, replacement, { regex, caseSensitive, wholeWord, include, exclude });
      setUndo(res.undo_token ? { token: res.undo_token, files: res.files_changed, total: res.total_replacements } : null);
      setRefresh((n) => n + 1);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.editor.search.replaceFailed')));
    } finally {
      setBusy(false);
    }
  }, [root, query, replacement, regex, caseSensitive, wholeWord, include, exclude, busy, data, t]);

  const doUndo = useCallback(async () => {
    if (!undo || busy) return;
    setBusy(true);
    setError(null);
    try {
      await undoReplace(undo.token);
      setUndo(null);
      setRefresh((n) => n + 1);
    } catch (e: unknown) {
      setError(fileBrowserErrorMessage(e, t, t('apps.editor.search.undoFailed')));
    } finally {
      setBusy(false);
    }
  }, [undo, busy, t]);

  if (root == null) {
    return (
      <div className="flex flex-col gap-2 px-3 py-2.5">
        <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('apps.editor.search.title')}</span>
        <div className="text-[12px] text-muted">{t('apps.editor.search.needFolder')}</div>
        <button
          type="button"
          onClick={onOpenFolder}
          className="flex items-center justify-center gap-1.5 rounded-md border border-mint/40 bg-mint/[0.08] px-2.5 py-1.5 text-[12px] font-semibold text-mint transition hover:bg-mint/[0.14]"
        >
          <FolderOpen className="size-3.5" />
          {t('apps.editor.openFolder')}
        </button>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-col gap-1.5 px-3 pb-2 pt-2.5">
        <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{t('apps.editor.search.title')}</span>
        {/* Query row: chevron toggles the replace row; the field carries the Aa / word / regex toggles. */}
        <div className="flex items-start gap-1">
          <button
            type="button"
            onClick={() => setShowReplace((v) => !v)}
            aria-label={t('apps.editor.search.toggleReplace')}
            title={t('apps.editor.search.toggleReplace')}
            className="mt-1.5 grid size-4 place-items-center rounded text-muted transition hover:text-foreground"
          >
            {showReplace ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          </button>
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <div className="flex items-center gap-1 rounded-md border border-cyan bg-surface-3 px-2 py-1 focus-within:border-cyan">
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('apps.editor.search.placeholder')}
                spellCheck={false}
                className="min-w-0 flex-1 bg-transparent font-mono text-[12px] text-foreground placeholder:text-muted focus:outline-none"
              />
              <Toggle on={caseSensitive} label={t('apps.editor.search.caseSensitive')} onClick={() => setCaseSensitive((v) => !v)}>
                <CaseSensitive className="size-3.5" />
              </Toggle>
              <Toggle on={wholeWord} label={t('apps.editor.search.wholeWord')} onClick={() => setWholeWord((v) => !v)}>
                <WholeWord className="size-3.5" />
              </Toggle>
              <Toggle on={regex} label={t('apps.editor.search.regex')} onClick={() => setRegex((v) => !v)}>
                <Regex className="size-3.5" />
              </Toggle>
            </div>
            {showReplace && (
              <div className="flex items-center gap-1">
                <CornerDownRight className="size-3.5 shrink-0 text-muted" />
                <div className="flex min-w-0 flex-1 items-center gap-1 rounded-md border border-border bg-surface-3 px-2 py-1">
                  <input
                    value={replacement}
                    onChange={(e) => setReplacement(e.target.value)}
                    placeholder={t('apps.editor.search.replacePlaceholder')}
                    spellCheck={false}
                    className="min-w-0 flex-1 bg-transparent font-mono text-[12px] text-foreground placeholder:text-muted focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void doReplace()}
                    disabled={busy || !data?.results.length}
                    aria-label={t('apps.editor.search.replaceAll')}
                    title={t('apps.editor.search.replaceAll')}
                    className="grid size-5 place-items-center rounded text-muted transition hover:bg-foreground/10 hover:text-foreground disabled:opacity-40"
                  >
                    <ReplaceAll className="size-3.5" />
                  </button>
                </div>
              </div>
            )}
            <button
              type="button"
              onClick={() => setShowScope((v) => !v)}
              className="self-start font-mono text-[10px] uppercase tracking-wide text-muted transition hover:text-foreground"
            >
              {showScope ? t('apps.editor.search.hideScope') : t('apps.editor.search.showScope')}
            </button>
            {showScope && (
              <div className="flex flex-col gap-1.5">
                <input
                  value={include}
                  onChange={(e) => setInclude(e.target.value)}
                  placeholder={t('apps.editor.search.include')}
                  spellCheck={false}
                  className="rounded-md border border-border bg-surface-3 px-2 py-1 font-mono text-[11px] text-foreground placeholder:text-muted focus:border-cyan focus:outline-none"
                />
                <input
                  value={exclude}
                  onChange={(e) => setExclude(e.target.value)}
                  placeholder={t('apps.editor.search.exclude')}
                  spellCheck={false}
                  className="rounded-md border border-border bg-surface-3 px-2 py-1 font-mono text-[11px] text-foreground placeholder:text-muted focus:border-cyan focus:outline-none"
                />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Summary / status strip */}
      <div className="flex min-h-[22px] items-center gap-2 px-3 text-[11px] text-muted">
        {loading ? (
          <span className="flex items-center gap-1.5">
            <Loader2 className="size-3 animate-spin" /> {t('apps.editor.search.searching')}
          </span>
        ) : error ? (
          <span className="text-destructive">{error}</span>
        ) : data ? (
          <span>{t('apps.editor.search.summary', { matches: data.total_matches, files: data.total_files })}</span>
        ) : query ? null : (
          <span>{t('apps.editor.search.hint')}</span>
        )}
      </div>

      {data?.truncated && !loading && (
        <div className="mx-3 mb-1 rounded border border-gold/30 bg-gold/[0.08] px-2 py-1 text-[11px] text-gold">
          {t(data.truncated_reason === 'files' ? 'apps.editor.search.truncatedFiles' : 'apps.editor.search.truncatedMatches')}
        </div>
      )}

      {undo && (
        <div className="mx-3 mb-1 flex items-center gap-2 rounded border border-mint/30 bg-mint/[0.08] px-2 py-1 text-[11px] text-mint">
          <span className="flex-1">{t('apps.editor.search.replaced', { total: undo.total, files: undo.files })}</span>
          <button type="button" onClick={() => void doUndo()} disabled={busy} className="flex items-center gap-1 font-semibold transition hover:underline disabled:opacity-40">
            <Undo2 className="size-3" /> {t('apps.editor.search.undo')}
          </button>
        </div>
      )}

      {/* Results tree */}
      <div className="min-h-0 flex-1 overflow-y-auto px-1 pb-2">
        {data?.results.map((file) => (
          <FileGroup
            key={file.path}
            file={file}
            collapsed={collapsed.has(file.path)}
            replacePreview={showReplace && replacement ? replacement : null}
            onToggle={() => toggleCollapse(file.path)}
            onJump={onJump}
          />
        ))}
      </div>
    </div>
  );
};

const FileGroup: React.FC<{
  file: SearchFileResult;
  collapsed: boolean;
  replacePreview: string | null;
  onToggle: () => void;
  onJump: (path: string, line: number, col: number, endCol: number) => void;
}> = ({ file, collapsed, replacePreview, onToggle, onJump }) => {
  const dir = file.rel.includes('/') ? file.rel.slice(0, file.rel.lastIndexOf('/')) : '';
  const name = file.rel.slice(file.rel.lastIndexOf('/') + 1);
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={onToggle}
        className="flex items-center gap-1.5 rounded px-1.5 py-1 text-left transition hover:bg-foreground/[0.05]"
      >
        {collapsed ? <ChevronRight className="size-3 shrink-0 text-muted" /> : <ChevronDown className="size-3 shrink-0 text-muted" />}
        <CodeXml className="size-3.5 shrink-0 text-cyan" />
        <span className="shrink-0 text-[12.5px] font-semibold text-foreground">{name}</span>
        {dir && <span className="min-w-0 flex-1 truncate text-[11px] text-muted">{dir}</span>}
        <span className="ml-auto shrink-0 rounded-full bg-surface-3 px-1.5 font-mono text-[10px] text-muted">{file.match_count}</span>
      </button>
      {!collapsed && (
        <div className="flex flex-col gap-px pl-2.5">
          {file.matches.map((m, i) => (
            <MatchRow key={`${m.line}:${m.col}:${i}`} match={m} replacePreview={replacePreview} onClick={() => onJump(file.path, m.line, m.col, m.end)} />
          ))}
        </div>
      )}
    </div>
  );
};

const MatchRow: React.FC<{ match: SearchMatch; replacePreview: string | null; onClick: () => void }> = ({ match, replacePreview, onClick }) => {
  const pre = match.text.slice(0, match.col);
  const hit = match.text.slice(match.col, match.end);
  const post = match.text.slice(match.end);
  return (
    <button type="button" onClick={onClick} className="flex items-center gap-2 rounded px-1.5 py-0.5 text-left transition hover:bg-cyan-soft">
      <span className="w-7 shrink-0 text-right font-mono text-[11px] tabular-nums text-muted">{match.line}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">
        {pre}
        {replacePreview != null ? (
          <>
            <span className="rounded-sm bg-destructive/20 text-destructive line-through">{hit}</span>
            <span className="rounded-sm bg-mint/20 text-mint">{replacePreview}</span>
          </>
        ) : (
          <span className="rounded-sm bg-gold/25 font-semibold text-foreground">{hit}</span>
        )}
        {post}
      </span>
    </button>
  );
};
