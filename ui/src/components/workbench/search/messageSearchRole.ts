import type { MessageSearchMatch } from '../../../context/ApiContext';
import { specFor } from '../../../lib/messageTypes';

export type MessageSearchRole = 'you' | 'automated' | 'agent';

export const messageSearchRole = (
  match: Pick<MessageSearchMatch, 'author' | 'source' | 'type'>,
): MessageSearchRole => {
  if (match.author === 'harness' || match.source === 'harness') return 'automated';
  // ``inputAuthors`` is a permission — which authors may submit this type as
  // input — not a claim about who wrote this row. A type can accept harness
  // input and still carry rows nobody submitted: an annotation is one type in
  // two directions, and the reverse one is written by the agent. So a row that
  // names a known author is attributed to that author, and the catalog's
  // input-turn identity only decides rows that do not.
  if (match.author === 'user') return 'you';
  if (match.author === 'agent') return 'agent';
  return specFor(match.type).inputAuthors.includes('harness') ? 'automated' : 'agent';
};
