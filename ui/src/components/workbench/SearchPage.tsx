import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { AlertCircle, ChevronLeft, Loader2, Search, X } from 'lucide-react';

import { useMessageSearch } from '../../lib/useMessageSearch';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { SearchResultGroup } from './search/SearchResultGroup';

// Mobile full-screen message-content search (design.pen K7Bytg "M · Search
// results"). A focused surface with its OWN header (back chevron + an active
// search field with mint glyph + clear ✕) over the grouped results — reached
// from the Inbox search field (InboxPage) / the bottom-nav is hidden on
// /search (AppShell). Results are SERVER-driven via useMessageSearch, the same
// hook the desktop ⌘K palette uses; selecting a hit routes to
// /chat/<session>?msg=<message> (the ?msg contract consumed by ChatPage P5).
// Touch surface — no keyboard arrow navigation (that is palette-only).
export const SearchPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  // The query lives in the URL (?q=…), not just component state, so navigating
  // INTO a result (push /chat/…?msg=…) and back (navigate(-1) → /search?q=…)
  // restores it — otherwise Back would land on a blank search page. Seed the
  // input from the param on mount and mirror every keystroke back into it.
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState(() => searchParams.get('q') ?? '');
  const { results, loading, error } = useMessageSearch(query);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Drive the query (state + URL together). ``replace: true`` so each keystroke
  // updates ?q= in place instead of pushing a history entry per character (Back
  // would otherwise step through every intermediate query). Preserve any other
  // params already on the URL.
  const updateQuery = (next: string) => {
    setQuery(next);
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next) params.set('q', next);
        else params.delete('q');
        return params;
      },
      { replace: true },
    );
  };

  // Autofocus the field on mount so typing flows straight in (the search page
  // exists only to search). iOS sometimes needs the focus deferred a tick past
  // the route transition for the soft keyboard to come up.
  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  // Back returns to where the user came from (usually the inbox field); a deep
  // link / refresh has nothing to pop, so fall back to /inbox. Mirrors
  // ChatPage's goBack.
  const goBack = () => {
    if (location.key !== 'default') navigate(-1);
    else navigate('/inbox');
  };

  // The clear ✕ empties a non-empty query (keeping focus to keep typing); on an
  // already-empty field it doubles as a back affordance.
  const onClear = () => {
    if (query) {
      updateQuery('');
      inputRef.current?.focus();
    } else {
      goBack();
    }
  };

  const trimmed = query.trim();
  const sessions = results?.sessions ?? [];
  const hasResults = sessions.length > 0;
  const showHint = trimmed.length === 0;
  const showError = trimmed.length > 0 && !loading && !!error;
  const showEmpty = trimmed.length > 0 && !loading && results !== null && !hasResults;

  return (
    // Full-screen focused surface (like ChatPage): fixed over the shell, its own
    // header is the top of the screen, and it scrolls internally. Desktop never
    // routes here (sidebar field + ⌘K own search), but keep it sane if hit.
    <div className="fixed inset-0 z-40 flex flex-col bg-background pt-[env(safe-area-inset-top)] md:absolute">
      {/* Header — back chevron + active search field (design.pen P6Nsz/KmsNV). */}
      <header className="flex shrink-0 items-center gap-2.5 border-b border-border bg-background/92 px-4 py-3 backdrop-blur">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={goBack}
          aria-label={t('common.back')}
          className="size-9 shrink-0 rounded-lg border border-border-strong bg-foreground/[0.04] hover:bg-foreground/[0.08]"
        >
          <ChevronLeft className="size-5" />
        </Button>
        <div className="flex min-w-0 flex-1 items-center gap-2.5 rounded-xl border border-border-strong bg-foreground/[0.04] px-3 py-2.5">
          <Search className="size-4 shrink-0 text-mint" />
          <Input
            ref={inputRef}
            variant="bare"
            value={query}
            onChange={(e) => updateQuery(e.target.value)}
            placeholder={t('workbench.search.placeholder')}
            aria-label={t('workbench.search.placeholder')}
            className="min-w-0 flex-1 text-[15px]"
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            enterKeyHint="search"
          />
          {loading && <Loader2 className="size-4 shrink-0 animate-spin text-muted" />}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={onClear}
            aria-label={t('common.clear')}
            className="size-5 shrink-0 rounded-md text-muted hover:bg-transparent hover:text-foreground [&_svg]:size-4"
          >
            <X className="size-4" />
          </Button>
        </div>
      </header>

      {/* Body — own scroll surface. Empty-query hint / no-results / grouped
          results, mirroring the desktop palette's states. */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-[calc(1.5rem+env(safe-area-inset-bottom))] pt-3">
        {showHint && (
          <div className="px-2.5 py-12 text-center text-[13px] text-muted">
            {t('workbench.search.hint')}
          </div>
        )}
        {showEmpty && (
          <div className="px-2.5 py-12 text-center text-[13px] text-muted">
            {t('workbench.search.empty')}
          </div>
        )}
        {showError && (
          <div className="px-2.5 py-12">
            <div className="mx-auto flex max-w-sm items-center gap-2 rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[13px] text-destructive">
              <AlertCircle className="size-4 shrink-0" />
              <span>{t('workbench.search.error')}</span>
            </div>
          </div>
        )}
        {hasResults && (
          <div className="flex flex-col gap-1.5">
            {sessions.map((session) => (
              <SearchResultGroup
                key={session.session_id}
                session={session}
                onSelect={(match) =>
                  navigate(
                    `/chat/${encodeURIComponent(session.session_id)}?msg=${encodeURIComponent(match.id)}`,
                  )
                }
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
