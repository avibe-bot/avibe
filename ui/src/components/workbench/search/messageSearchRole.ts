import type { MessageSearchMatch } from '../../../context/ApiContext';

export type MessageSearchRole = 'you' | 'automated' | 'agent';

export const messageSearchRole = (
  match: Pick<MessageSearchMatch, 'author' | 'source' | 'type'>,
): MessageSearchRole => {
  if (match.author === 'harness' || match.source === 'harness' || match.type === 'harness') {
    return 'automated';
  }
  return match.author === 'user' ? 'you' : 'agent';
};
