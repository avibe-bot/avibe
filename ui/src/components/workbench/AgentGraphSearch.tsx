// Run-graph node search (M8): a toolbar combobox that finds a session node or
// trigger chip by id/title/name and, on select, asks the canvas to pan+zoom to
// it. The matcher is the pure `searchGraph` (unit-tested); this component owns
// only the input + dropdown + keyboard nav. The search corpus is the broad
// window payload (incl. filter-hidden rows), so a hit can sit "outside current
// filters" — those rows are badged and selecting one widens the filters first
// (handled by the parent).
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Search } from 'lucide-react';
import clsx from 'clsx';

import { nodeDisplayTitle, statusMeta } from '../../lib/agentGraph';
import type { GraphSearchResult } from '../../lib/graphSearch';

// Keep the dropdown bounded; a prefix/substring query narrows fast, and an
// unbounded list would overflow the toolbar. Truncation is surfaced, not silent.
const MAX_RESULTS = 40;

const shortId = (id: string) => (id.length > 6 ? `…${id.slice(-6)}` : id);

interface AgentGraphSearchProps {
  query: string;
  onQueryChange: (q: string) => void;
  results: GraphSearchResult[];
  // Lazy-load hook: the parent fetches the broad search index on first focus.
  onFocus: () => void;
  onSelect: (result: GraphSearchResult) => void;
  // True while the search index is being fetched (first focus / after refresh).
  loading: boolean;
  // Whether a result currently sits outside the visible graph filters.
  isOutsideFilters: (result: GraphSearchResult) => boolean;
}

export const AgentGraphSearch: React.FC<AgentGraphSearchProps> = ({
  query,
  onQueryChange,
  results,
  onFocus,
  onSelect,
  loading,
  isOutsideFilters,
}) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const trimmed = query.trim();
  const shown = useMemo(() => results.slice(0, MAX_RESULTS), [results]);
  const overflow = results.length - shown.length;
  // The panel opens on focus; content shows once there's a query to answer.
  const panelOpen = open && trimmed.length > 0;
  // Clamp the active row to the current results at read time rather than syncing
  // it back through an effect (avoids a cascading re-render when results shrink).
  const activeRow = shown.length ? Math.min(activeIndex, shown.length - 1) : 0;

  // Scroll the active row into view for keyboard nav past the visible slice.
  useEffect(() => {
    if (!panelOpen) return;
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeRow, panelOpen]);

  // Close on an outside click (mousedown so it beats a row's onMouseDown-select).
  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [open]);

  const choose = (r: GraphSearchResult | undefined) => {
    if (!r) return;
    onSelect(r);
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (!panelOpen || shown.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex(Math.min(activeRow + 1, shown.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex(Math.max(activeRow - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      choose(shown[activeRow]);
    }
  };

  return (
    <div ref={containerRef} className="relative w-full sm:w-[300px]">
      <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
        <Search className="size-3.5 shrink-0 text-muted" />
        <input
          value={query}
          onChange={(e) => {
            onQueryChange(e.target.value);
            setOpen(true);
            setActiveIndex(0);
          }}
          onFocus={() => {
            setOpen(true);
            onFocus();
          }}
          onKeyDown={onKeyDown}
          placeholder={t('agents.graph.search.placeholder')}
          aria-label={t('agents.graph.search.placeholder')}
          role="combobox"
          aria-expanded={panelOpen}
          aria-controls="agent-graph-search-list"
          className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
        />
        {loading && <Loader2 className="size-3.5 shrink-0 animate-spin text-muted" />}
      </div>

      {panelOpen && (
        <div
          id="agent-graph-search-list"
          ref={listRef}
          role="listbox"
          className="absolute z-30 mt-1.5 max-h-[320px] w-full min-w-[280px] overflow-y-auto rounded-lg border border-border-strong bg-surface p-1 shadow-lg"
        >
          {shown.length === 0 ? (
            <div className="px-3 py-6 text-center text-[12px] text-muted">
              {loading ? t('agents.graph.search.loading') : t('agents.graph.search.empty')}
            </div>
          ) : (
            <>
              {shown.map((r, i) => (
                <ResultRow
                  key={r.kind === 'node' ? `n:${r.id}` : `t:${r.id}`}
                  result={r}
                  active={i === activeRow}
                  outside={isOutsideFilters(r)}
                  onHover={() => setActiveIndex(i)}
                  onChoose={() => choose(r)}
                />
              ))}
              {overflow > 0 && (
                <div className="px-3 pb-1.5 pt-1 text-center text-[11px] text-muted/80">
                  {t('agents.graph.search.more', { count: overflow })}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
};

const ResultRow: React.FC<{
  result: GraphSearchResult;
  active: boolean;
  outside: boolean;
  onHover: () => void;
  onChoose: () => void;
}> = ({ result, active, outside, onHover, onChoose }) => {
  const { t } = useTranslation();
  // onMouseDown (not onClick) so selecting fires before the input's blur closes
  // the panel; preventDefault keeps focus on the input.
  const select = (e: React.MouseEvent) => {
    e.preventDefault();
    onChoose();
  };
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      data-active={active}
      onMouseEnter={onHover}
      onMouseDown={select}
      className={clsx(
        'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition',
        active ? 'bg-foreground/[0.06]' : 'hover:bg-foreground/[0.04]',
        // Trigger rows get a violet left accent so they read distinctly from
        // session rows (spec: "行样式区分").
        result.kind === 'trigger' && 'border-l-2 border-violet/70 pl-1.5',
      )}
    >
      {result.kind === 'node' ? (
        <>
          <span className={clsx('size-1.5 shrink-0 rounded-full', statusMeta(result.node.status).dotClass)} />
          <span className="min-w-0 flex-1 truncate font-medium text-foreground">
            {nodeDisplayTitle(result.node)}
          </span>
          <span className="shrink-0 font-mono text-[10.5px] text-muted">{shortId(result.id)}</span>
          <span className="shrink-0 text-[10.5px] text-muted">
            {t(statusMeta(result.node.status).labelKey)}
          </span>
        </>
      ) : (
        <>
          <span className="shrink-0 rounded bg-violet/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-violet">
            {t('agents.graph.search.triggerTag')}
          </span>
          <span className="min-w-0 flex-1 truncate font-medium text-foreground">
            {result.trigger.name?.trim() || shortId(result.id)}
          </span>
          <span className="shrink-0 font-mono text-[10.5px] text-muted">{shortId(result.id)}</span>
        </>
      )}
      {outside && (
        <span className="shrink-0 rounded bg-gold/15 px-1.5 py-0.5 text-[10px] font-medium text-gold">
          {t('agents.graph.search.outsideFilters')}
        </span>
      )}
    </button>
  );
};
