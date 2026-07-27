import type { MessageSearchMatch } from '../../../context/ApiContext';
import { specFor } from '../../../lib/messageTypes';

export type MessageSearchRole = 'you' | 'automated' | 'agent';

export const messageSearchRole = (
  match: Pick<MessageSearchMatch, 'author' | 'source' | 'type'>,
): MessageSearchRole => {
  // The type test is the catalog's input-turn identity: a type accepted as harness
  // input (``inputAuthors`` contains ``harness``), not a bare name comparison.
  if (
    match.author === 'harness' ||
    match.source === 'harness' ||
    specFor(match.type).inputAuthors.includes('harness')
  ) {
    return 'automated';
  }
  return match.author === 'user' ? 'you' : 'agent';
};
