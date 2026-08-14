import {
  SOURCE_PROTOCOLS,
  type SourceObservation,
  type SourceProtocol,
} from './types';

export type AddApiKeyOrigin = 'add' | 'pull';
export type AddApiKeyFailure = 'auth' | 'network' | 'interface' | 'unclassified' | 'engineDown';

export type ObservationVerdict =
  | { kind: 'ready'; observation: SourceObservation }
  | { kind: 'undetermined'; observation: SourceObservation }
  | { kind: 'inventory'; observation: SourceObservation }
  | { kind: 'failure'; cause: AddApiKeyFailure };

export const PROTOCOL_COPY_KEYS: Record<SourceProtocol, string> = {
  anthropic: 'settings.models.addKey.protocol.anthropicMessages',
  openai_responses: 'settings.models.addKey.protocol.openaiResponses',
  openai_chat: 'settings.models.addKey.protocol.openaiChatCompletions',
};

export function protocolOrderWithHint(hint: SourceProtocol | null): SourceProtocol[] | undefined {
  if (!hint) return undefined;
  return [hint, ...SOURCE_PROTOCOLS.filter((protocol) => protocol !== hint)];
}

export function classifyObservation(observation: SourceObservation): ObservationVerdict {
  switch (observation.outcome) {
    case 'observed':
      if (observation.protocol && observation.discovery === 'succeeded') {
        return { kind: 'ready', observation };
      }
      if (observation.protocol && observation.discovery === 'failed') {
        return { kind: 'inventory', observation };
      }
      return { kind: 'failure', cause: 'unclassified' };
    case 'ambiguous':
      return { kind: 'undetermined', observation };
    case 'authentication_failed':
      return { kind: 'failure', cause: 'auth' };
    case 'unreachable':
    case 'timeout':
      return { kind: 'failure', cause: 'network' };
    case 'adapter_error':
      return { kind: 'failure', cause: observation.reachable ? 'interface' : 'unclassified' };
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
