import { useTranslation } from 'react-i18next';
import { Folder } from 'lucide-react';

import type { MessageSearchMatch, MessageSearchSession } from '../../../context/ApiContext';
import { Badge } from '../../ui/badge';
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
// An archived session (only reachable with the "include archived" opt-in) gets a
// badge in the header and a dimmed group, so a read-only hit is recognizable
// before it is opened.
export const SearchResultGroup: React.FC<SearchResultGroupProps> = ({
  session,
  selectedId,
  onSelect,
}) => {
  const { t } = useTranslation();
  const projectLabel = session.project_name || session.project_id || '—';
  const sessionLabel = session.title || session.session_id;

  return (
    <div className={`flex flex-col${session.archived ? ' opacity-70' : ''}`}>
      <div className="flex items-center gap-1.5 px-2.5 py-1.5">
        <Folder className="size-[13px] shrink-0 text-muted" />
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] font-bold text-muted">
          {projectLabel} · {sessionLabel}
        </span>
        {session.archived && (
          <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-[10px] font-bold">
            {t('common.archived')}
          </Badge>
        )}
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
