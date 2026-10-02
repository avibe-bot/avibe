import type { TranslationKey } from '@/i18n/types';
import type { SourceProtocol } from './types';

export const PROTOCOL_COPY_KEYS: Record<SourceProtocol, TranslationKey> = {
  google: 'settings.models.addKey.protocol.google',
  anthropic: 'settings.models.addKey.protocol.anthropicMessages',
  openai_responses: 'settings.models.addKey.protocol.openaiResponses',
  openai_chat: 'settings.models.addKey.protocol.openaiChatCompletions',
};
