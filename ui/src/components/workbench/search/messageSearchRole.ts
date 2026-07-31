import type { MessageSearchMatch } from '../../../context/ApiContext';
import { specFor } from '../../../lib/messageTypes';

export type MessageSearchRole = 'you' | 'automated' | 'agent';

export const messageSearchRole = (
  match: Pick<MessageSearchMatch, 'author' | 'source' | 'type'>,
): MessageSearchRole => {
  // An annotation is one type in two directions, and ``direction`` — the field
  // that separates them — lives in ``content``, which a search match does not
  // carry. Inside this type the frozen contract makes ``author`` stand in for it
  // exactly: a forward annotation is the user's own, submitted as turn input and
  // therefore recorded as harness-authored; a reverse mark is written by the
  // agent. So here a harness author does NOT mean automated, which is why the
  // type is settled before the author rules below rather than inside them.
  if (match.type === 'annotation') return match.author === 'agent' ? 'agent' : 'you';

  if (match.author === 'harness' || match.source === 'harness') return 'automated';
  if (match.author === 'user') return 'you';
  if (match.author === 'agent') return 'agent';
  // ``inputAuthors`` is a permission — which authors may submit this type as
  // input — not a claim about who wrote this row, so it only decides rows whose
  // author names nobody above. Keeping it means a future harness-input type
  // inherits the automated treatment without another edit here.
  return specFor(match.type).inputAuthors.includes('harness') ? 'automated' : 'agent';
};
