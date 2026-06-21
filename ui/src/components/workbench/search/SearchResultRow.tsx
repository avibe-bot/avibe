import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import type { MessageSearchMatch } from '../../../context/ApiContext';
import { formatRelativeTime } from '../../../lib/relativeTime';
import { Button } from '../../ui/button';
import { Snippet } from './Snippet';

type SearchResultRowProps = {
  match: MessageSearchMatch;
  // Keyboard-highlighted row (palette arrow navigation in P3); paints a
  // mint-soft background + mint ring so the active hit reads clearly.
  selected?: boolean;
  onSelect?: () => void;
};

// One matching message: a role chip (YOU / AGENT), the highlighted snippet, and
// a muted relative timestamp. Presentational + reusable by both the desktop
// palette and the mobile page — navigation is wired by the consumer via
// ``onSelect``.
//
// Built on the shared ``Button`` primitive (variant="ghost") per AGENTS.md —
// the row is a button. Button's size variants impose a fixed height and centered
// layout, so the className overrides them back to this row's auto-height,
// full-width, left-aligned shape (twMerge lets className win over the variant
// utilities). The selected state is marked with ``aria-current="true"`` — the
// palette relies on that attribute to scroll the active row into view.
export const SearchResultRow: React.FC<SearchResultRowProps> = ({ match, selected, onSelect }) => {
  const { t } = useTranslation();
  const isUser = match.author === 'user';

  return (
    <Button
      type="button"
      variant="ghost"
      onClick={onSelect}
      aria-current={selected ? 'true' : undefined}
      className={clsx(
        'h-auto w-full justify-start gap-2.5 px-2.5 py-2 text-left font-normal',
        selected
          ? 'bg-mint-soft ring-1 ring-inset ring-mint/40 hover:bg-mint-soft'
          : 'hover:bg-foreground/[0.04]',
      )}
    >
      <span
        className={clsx(
          'shrink-0 rounded-md px-2 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider',
          isUser
            ? 'bg-cyan-soft text-cyan'
            : 'bg-mint-soft text-mint',
        )}
      >
        {isUser ? t('workbench.search.roleYou') : t('workbench.search.roleAgent')}
      </span>
      <span className="min-w-0 flex-1">
        <Snippet snippet={match.snippet} />
      </span>
      <span className="shrink-0 font-mono text-[10px] text-muted">
        {formatRelativeTime(match.created_at, t)}
      </span>
    </Button>
  );
};
