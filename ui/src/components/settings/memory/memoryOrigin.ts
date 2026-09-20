import type { TranslationKey } from '@/i18n/types';
import type { MemoryItem } from '../../../context/ApiContext';

export const memoryOriginLabelKey = (origin: MemoryItem['origin']): TranslationKey | null =>
  origin ? `memory.origin.${origin}` : null;
