export const isNotifyMessageType = (type: string): boolean =>
  type === 'notify' || type === 'error';

type TerminalMessageCandidate = {
  author: string;
  type: string;
  metadata?: Record<string, unknown> | null;
};

export const isTerminalAgentMessage = (message: TerminalMessageCandidate): boolean =>
  message.author === 'agent' &&
  (message.type === 'result' ||
    message.type === 'error' ||
    (message.type === 'notify' && message.metadata?.event === 'backend_failure'));
