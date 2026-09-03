import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { AlertCircle, Loader2, Search } from 'lucide-react';

import { appTabHref, tabModifierLabel, type LaunchModifiers } from '../../../apps/appLaunch';
import { isComposingKey } from '@/lib/imeComposition';
import { useMessageSearch } from '../../../lib/useMessageSearch';
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from '../../ui/dialog';
import { Input } from '../../ui/input';
import { Switch } from '../../ui/switch';
import { AppSearchResultSection } from './AppSearchResults';
import { SearchResultGroup } from './SearchResultGroup';
import { useAppSearchResults, useOpenSearchApp } from './useAppSearch';

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

// Desktop ⌘K command palette for global app + message-content search
// (design.pen sUCZo).
// A centered 720px modal: a query row (mint search glyph + text input + an Esc
// pill), a scrollable results area grouped by session via the shared
// <SearchResultGroup>, and a footer of keyboard hints. Message hits stay
// server-driven; the Apps section filters the built-in registry + Show Pages
// inventory client-side. Selecting a message routes to /chat/<session>?msg=<message>.
export const SearchPalette: React.FC<SearchPaletteProps> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  // Opt-in only, and reset on every open (see onOpenAutoFocus) — the palette
  // already resets the query there, and a sticky archived filter would quietly
  // change what ⌘K returns by default.
  const [includeArchived, setIncludeArchived] = useState(false);
  const { results, loading, error } = useMessageSearch(query, { includeArchived });
  const { results: appResults, loading: appsLoading } = useAppSearchResults(query, open);
  const openSearchApp = useOpenSearchApp();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // One flat navigation order: Apps first, then the existing message groups.
  const flatTargets = useMemo(
    () => [
      ...appResults.map((result) => ({ kind: 'app' as const, key: `app:${result.key}`, result })),
      ...(results?.sessions ?? []).flatMap((session) =>
        session.matches.map((match) => ({
          kind: 'message' as const,
          key: `message:${session.session_id}:${match.id}`,
          sessionId: session.session_id,
          messageId: match.id,
        })),
      ),
    ],
    [appResults, results],
  );

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // A changed result set falls back to its first row without a state-sync
  // effect. Arrow navigation only stores an explicit key while it stays valid.
  const selectedTarget = flatTargets.find((target) => target.key === selectedKey) ?? flatTargets[0];
  // Whether the highlighted row actually HAS a browser-tab surface: the ⌘↵ hint below
  // must not promise a tab for a message hit, nor for a window-only app such as the
  // Library, where `openSearchApp` falls back to a workbench window (§7.1m).
  const selectedTabHref =
    selectedTarget?.kind === 'app'
      ? appTabHref({
          appId: selectedTarget.result.appId,
          sessionId: selectedTarget.result.kind === 'showpage' ? selectedTarget.result.sessionId : null,
        })
      : null;

  const handleSelect = (sessionId: string, messageId: string) => {
    navigate(`/chat/${encodeURIComponent(sessionId)}?msg=${encodeURIComponent(messageId)}`);
    onClose();
  };

  // `launch` carries the activating event's modifiers, so ⌘/Ctrl+click AND
  // ⌘/Ctrl+Enter open the app in a browser tab (§7.1m).
  const handleAppSelect = (result: (typeof appResults)[number], launch?: LaunchModifiers) => {
    openSearchApp(result, launch);
    onClose();
  };

  const moveSelection = (delta: number) => {
    if (flatTargets.length === 0) return;
    const idx = flatTargets.findIndex((target) => target.key === selectedTarget?.key);
    const next = idx < 0 ? 0 : (idx + delta + flatTargets.length) % flatTargets.length;
    setSelectedKey(flatTargets[next].key);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    // While an IME composition is active (Chinese/Japanese/Korean candidate
    // selection), ArrowUp/Down/Enter belong to the IME — navigating candidates
    // and committing the chosen one. Intercepting them would steal those keys
    // from the input. The two signals a browser may announce this through are
    // read by the shared guard.
    if (isComposingKey(e)) return;
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
      if (selectedTarget?.kind === 'app') handleAppSelect(selectedTarget.result, e);
      else if (selectedTarget) handleSelect(selectedTarget.sessionId, selectedTarget.messageId);
    }
  };

  // Scroll the highlighted row into view as the selection walks past the fold.
  // SearchResultRow marks the active row with aria-current="true"; lean on that
  // rather than threading a new data attribute through the shared P2 component.
  useEffect(() => {
    if (!selectedTarget || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>('[aria-current="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedTarget]);

  const trimmed = query.trim();
  const hasMessageResults = (results?.sessions.length ?? 0) > 0;
  const hasResults = appResults.length > 0 || hasMessageResults;
  const showError = trimmed.length > 0 && !loading && !!error;
  const showEmpty = trimmed.length > 0 && !loading && !appsLoading && results !== null && !hasResults;
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
            setQuery('');
            setSelectedKey(null);
            setIncludeArchived(false);
            inputRef.current?.focus();
          }}
          aria-describedby={undefined}
          className="fixed left-1/2 top-[120px] z-50 flex max-h-[min(640px,calc(100dvh-160px))] w-[720px] max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col overflow-hidden rounded-2xl border border-border-strong bg-surface-2 shadow-[0_32px_80px_-16px_rgba(0,0,0,0.75)] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95"
        >
          <DialogTitle className="sr-only">{t('workbench.search.title')}</DialogTitle>

          {/* Query row — mint search glyph + input + Esc pill. */}
          <div className="flex shrink-0 items-center gap-3 border-b border-border px-[18px] py-4">
            <Search className="size-[18px] shrink-0 text-mint-ink" />
            <Input
              ref={inputRef}
              variant="bare"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelectedKey(null);
              }}
              placeholder={t('workbench.search.placeholder')}
              className="min-w-0 flex-1 text-[17px]"
              aria-label={t('workbench.search.placeholder')}
              spellCheck={false}
              autoComplete="off"
            />
            {(loading || appsLoading) && <Loader2 className="size-4 shrink-0 animate-spin text-muted" />}
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
                <div className="mx-auto flex max-w-sm items-center gap-2 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[13px] text-destructive-ink">
                  <AlertCircle className="size-4 shrink-0" />
                  <span>{t('workbench.search.error')}</span>
                </div>
              </div>
            )}
            {hasResults && (
              <div className="flex flex-col gap-1.5">
                <AppSearchResultSection
                  results={appResults}
                  selectedKey={selectedTarget?.kind === 'app' ? selectedTarget.result.key : undefined}
                  onSelect={handleAppSelect}
                />
                {(results?.sessions ?? []).map((session) => (
                  <SearchResultGroup
                    key={session.session_id}
                    session={session}
                    selectedId={
                      selectedTarget?.kind === 'message' && selectedTarget.sessionId === session.session_id
                        ? selectedTarget.messageId
                        : undefined
                    }
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
            {/* Only while an app WITH a tab surface is highlighted: ⌘↵ opens it as a
                browser tab (§7.1m), which a message hit has no equivalent of — so the
                hint appears with the target it applies to instead of always sitting in
                the footer. */}
            {selectedTabHref && (
              <FooterHint
                keyLabel={`${tabModifierLabel()}↵`}
                label={t('workbench.search.kbdNewTab')}
              />
            )}
            <FooterHint keyLabel={t('workbench.search.kbdEsc')} label={t('workbench.search.kbdClose')} />
            {/* Archived opt-in — off on every open; archived hits open read-only. */}
            <label className="flex items-center gap-1.5 text-[11px] text-muted">
              <Switch
                checked={includeArchived}
                onCheckedChange={setIncludeArchived}
                label={t('workbench.search.includeArchived')}
                size="sm"
              />
              {t('workbench.search.includeArchived')}
            </label>
            <span className="flex-1" />
            <span className="truncate text-[11px] text-muted">{t('workbench.search.footerNote')}</span>
          </div>
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
};
