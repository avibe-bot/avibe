import { pickerGroups } from '../settings/models/backendCatalog';
import type { BackendModelCandidates, ModelCandidate } from '../settings/models/types';

/** The candidates read behind a built-in card that has no model yet. */
export type BuiltinModelOffer =
  | { kind: 'loading' }
  | { kind: 'failed' }
  | { kind: 'ready'; read: BackendModelCandidates };

/** How many candidates the card offers inline; the rest are behind "All models…". */
export const INLINE_CANDIDATES = 3;

/**
 * The candidates worth offering on the card, in the picker's own order: the ones
 * already in the backend's list, then its built-in and provider candidates. A candidate
 * nothing supplies would start an empty route, so only the dialog offers those.
 */
export const inlineCandidates = (read: BackendModelCandidates): ModelCandidate[] => {
  const groups = pickerGroups(read, new Set(read.in_list.map((candidate) => candidate.id)));
  return [...groups.listed, ...groups.builtin, ...groups.providers]
    .filter((candidate) => candidate.suppliers.length > 0)
    .slice(0, INLINE_CANDIDATES);
};
