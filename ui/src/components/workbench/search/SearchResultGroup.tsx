import { Folder } from 'lucide-react';

import type { MessageSearchMatch, MessageSearchSession } from '../../../context/ApiContext';
import { SearchResultRow } from './SearchResultRow';

type SearchResultGroupProps = {
  session: MessageSearchSession;
  // ``id`` of the currently keyboard-highlighted match (palette navigation).
  selectedId?: string;
  onSelect?: (match: MessageSearchMatch) => void;
};

// A session group: a muted header (folder glyph + "project · session" label +
// match count) followed by its matching rows. Presentational — selection state
// and navigation are passed through from the consumer.
export const SearchResultGroup: React.FC<SearchResultGroupProps> = ({
  session,
  selectedId,
  onSelect,
}) => {
  const projectLabel = session.project_name || session.project_id || '—';
  const sessionLabel = session.title || session.session_id;

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-1.5 px-2.5 py-1.5">
        <Folder className="size-[13px] shrink-0 text-muted" />
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] font-bold text-muted">
          {projectLabel} · {sessionLabel}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-muted">{session.matches.length}</span>
      </div>
      <div className="flex flex-col">
        {session.matches.map((match) => (
          <SearchResultRow
            key={match.id}
            match={match}
            selected={selectedId === match.id}
            onSelect={onSelect ? () => onSelect(match) : undefined}
          />
        ))}
      </div>
    </div>
  );
};
