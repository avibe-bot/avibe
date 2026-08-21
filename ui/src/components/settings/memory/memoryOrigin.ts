import type { MemoryItem } from '../../../context/ApiContext';

export const memoryOriginLabelKey = (origin: MemoryItem['origin']): string | null =>
  origin ? `memory.origin.${origin}` : null;
