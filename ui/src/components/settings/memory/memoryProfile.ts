import type { MemoryItem, MemoryProfile } from '../../../context/ApiContext';

/** Return the first recognized provider profile, leaving legacy raw items untouched. */
export const structuredProfileFromItems = (items: readonly MemoryItem[] | null): MemoryProfile | null =>
  items?.find((item) => item.kind === 'profile' && item.profile)?.profile ?? null;
