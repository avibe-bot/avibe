import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { AlertCircle, Loader2, Search } from 'lucide-react';

import { useMessageSearch } from '../../../lib/useMessageSearch';
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from '../../ui/dialog';
import { Input } from '../../ui/input';
import { SearchResultGroup } from './SearchResultGroup';

type SearchPaletteProps = {
  open: boolean;
  onClose: () => void;
};

// One footer keyboard hint: a key-cap pill + its label (↑↓ Navigate, etc.).
// Mirrors design.pen UM9dm → Hint (Key cornerRadius 5 / #FFFFFF0F / border,
// mono key + Inter muted label).
const FooterHint: React.FC<{ keyLabel: string; label: string }> = ({ keyLabel, label }) => (
  <span className="flex items-center gap-1.5">
    <kbd className="flex items-center justify-center rounded-[5px] border border-border bg-foreground/[0.06] px-1.5 py-0.5 font-mono text-[10px] font-bold text-muted">
      {keyLabel}
    </kbd>
    <span className="text-[11px] text-muted">{label}</span>
  </span>
);

// Desktop ⌘K command palette for message-content search (design.pen sUCZo).
// A centered 720px modal: a query row (mint search glyph + text input + an Esc
// pill), a scrollable results area grouped by session via the shared
// <SearchResultGroup>, and a footer of keyboard hints. Results are SERVER-driven
// (useMessageSearch) — there is no client-side filtering. Selecting a hit routes
// to /chat/<session>?msg=<message> (the ?msg contract is consumed by P5).
export const SearchPalette: React.FC<SearchPaletteProps> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const { results, loading, error } = useMessageSearch(query);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // A flat, ordered list of (sessionId, messageId) — the navigation order the
  // arrow keys walk, reading sessions top-to-bottom and matches within each.
  const flatMatches = useMemo(
    () =>
      (results?.sessions ?? []).flatMap((session) =>
        session.matches.map((match) => ({ sessionId: session.session_id, messageId: match.id })),
      ),
    [results],
  );

  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Reset the query (and the in-flight search via the hook) every time the
  // palette reopens — the parent keeps it mounted, so without this a stale
  // query/result set would flash on the next open.
  useEffect(() => {
    if (open) {
      setQuery('');
      setSelectedId(null);
    }
  }, [open]);

  // Keep a valid highlight as results change: default to the first hit, and snap
  // back to it if the previously selected message dropped out of the new set.
  useEffect(() => {
    if (flatMatches.length === 0) {
      setSelectedId(null);
      return;
    }
    setSelectedId((prev) =>
      prev && flatMatches.some((m) => m.messageId === prev) ? prev : flatMatches[0].messageId,
    );
  }, [flatMatches]);

  const handleSelect = (sessionId: string, messageId: string) => {
    navigate(`/chat/${encodeURIComponent(sessionId)}?msg=${encodeURIComponent(messageId)}`);
    onClose();
  };

  const moveSelection = (delta: number) => {
    if (flatMatches.length === 0) return;
    const idx = flatMatches.findIndex((m) => m.messageId === selectedId);
    const next = idx < 0 ? 0 : (idx + delta + flatMatches.length) % flatMatches.length;
    setSelectedId(flatMatches[next].messageId);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    // While an IME composition is active (Chinese/Japanese/Korean candidate
    // selection), ArrowUp/Down/Enter belong to the IME — navigating candidates
    // and committing the chosen one. Intercepting them would steal those keys
    // from the input. ``keyCode === 229`` is the legacy IME-in-progress signal
    // for browsers that don't set ``isComposing`` on the synthetic event.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveSelection(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveSelection(-1);
    } else if (e.key === 'Enter') {
      // Result rows are real <Button>s now (focusable via Tab). Only treat Enter
      // as "open the arrow-selected hit" while the SEARCH INPUT holds focus; when
      // a row Button is focused, let its native Enter→click fire so the FOCUSED
      // row activates instead of the (possibly different) arrow-selected one.
      if (document.activeElement !== inputRef.current) return;
      e.preventDefault();
      const target = flatMatches.find((m) => m.messageId === selectedId);
      if (target) handleSelect(target.sessionId, target.messageId);
    }
  };

  // Scroll the highlighted row into view as the selection walks past the fold.
  // SearchResultRow marks the active row with aria-current="true"; lean on that
  // rather than threading a new data attribute through the shared P2 component.
  useEffect(() => {
    if (!selectedId || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>('[aria-current="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedId]);

  const trimmed = query.trim();
  const hasResults = (results?.sessions.length ?? 0) > 0;
  const showError = trimmed.length > 0 && !loading && !!error;
  const showEmpty = trimmed.length > 0 && !loading && results !== null && !hasResults;
  const showHint = trimmed.length === 0;

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogPortal>
        <DialogOverlay className="bg-[#05050B]/85" />
        <DialogPrimitive.Content
          onKeyDown={onKeyDown}
          onOpenAutoFocus={(e) => {
            // Focus the query field rather than the first row, so typing flows
            // straight into the search input.
            e.preventDefault();
            inputRef.current?.focus();
          }}
          aria-describedby={undefined}
          className="fixed left-1/2 top-[120px] z-50 flex max-h-[min(640px,calc(100dvh-160px))] w-[720px] max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col overflow-hidden rounded-2xl border border-border-strong bg-surface-2 shadow-[0_32px_80px_-16px_rgba(0,0,0,0.75)] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95"
        >
          <DialogTitle className="sr-only">{t('workbench.search.title')}</DialogTitle>

          {/* Query row — mint search glyph + input + Esc pill. */}
          <div className="flex shrink-0 items-center gap-3 border-b border-border px-[18px] py-4">
            <Search className="size-[18px] shrink-0 text-mint" />
            <Input
              ref={inputRef}
              variant="bare"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('workbench.search.placeholder')}
              className="min-w-0 flex-1 text-[17px]"
              aria-label={t('workbench.search.placeholder')}
              spellCheck={false}
              autoComplete="off"
            />
            {loading && <Loader2 className="size-4 shrink-0 animate-spin text-muted" />}
            <kbd className="shrink-0 rounded-md border border-border bg-foreground/[0.06] px-2 py-[3px] font-mono text-[11px] text-muted">
              {t('workbench.search.kbdEsc')}
            </kbd>
          </div>

          {/* Results / states. */}
          <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
            {showHint && (
              <div className="px-2.5 py-10 text-center text-[13px] text-muted">
                {t('workbench.search.hint')}
              </div>
            )}
            {showEmpty && (
              <div className="px-2.5 py-10 text-center text-[13px] text-muted">
                {t('workbench.search.empty')}
              </div>
            )}
            {showError && (
              <div className="px-2.5 py-10">
                <div className="mx-auto flex max-w-sm items-center gap-2 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[13px] text-destructive">
                  <AlertCircle className="size-4 shrink-0" />
                  <span>{t('workbench.search.error')}</span>
                </div>
              </div>
            )}
            {hasResults && (
              <div className="flex flex-col gap-1.5">
                {results!.sessions.map((session) => (
                  <SearchResultGroup
                    key={session.session_id}
                    session={session}
                    selectedId={selectedId ?? undefined}
                    onSelect={(match) => handleSelect(session.session_id, match.id)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Footer keyboard hints. */}
          <div className="flex shrink-0 items-center gap-4 border-t border-border bg-foreground/[0.02] px-4 py-[11px]">
            <FooterHint keyLabel="↑↓" label={t('workbench.search.kbdNavigate')} />
            <FooterHint keyLabel="↵" label={t('workbench.search.kbdOpen')} />
            <FooterHint keyLabel={t('workbench.search.kbdEsc')} label={t('workbench.search.kbdClose')} />
            <span className="flex-1" />
            <span className="truncate text-[11px] text-muted">{t('workbench.search.footerNote')}</span>
          </div>
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
};
