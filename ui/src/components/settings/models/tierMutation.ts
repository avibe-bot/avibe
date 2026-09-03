import type { ReasoningEffortsSource, Source } from './types';

/**
 * The rungs of the provenance ladder the SERVER owns: it re-applies them on
 * every refresh and refuses `PATCH .../models/{id}` tier edits against them
 * with `source_model_tiers_managed`. Everything else — `user`, an explicit
 * `null`, an absent field on an older server, or a rung a newer server names
 * that this build has never heard of — is editable.
 *
 * Unknown degrades to editable on purpose. Locking on an unrecognized value
 * would put the row behind a badge this build cannot label and a rule it cannot
 * state, for a claim it never verified; offering the edit and rendering the
 * server's refusal (`fail.tierManaged`) is the same outcome arrived at honestly.
 * The server is the authority either way — this reader only decides what the UI
 * proposes on its own initiative.
 */
export const MANAGED_TIER_SOURCES = ['upstream', 'catalog'] as const;

export type ManagedTierSource = (typeof MANAGED_TIER_SOURCES)[number];

const MANAGED = new Set<string>(MANAGED_TIER_SOURCES);

/**
 * The rung locking a model's tiers, or null when the user may edit them.
 *
 * Takes the field rather than the model so every call site spells
 * `model.reasoning_efforts_source` out loud: this is the one wire field whose
 * absence changes what the UI offers, and a rename that misses a reader would
 * otherwise unlock every row silently.
 */
export const managedTierSource = (
  source: ReasoningEffortsSource | null | undefined,
): ManagedTierSource | null => (source && MANAGED.has(source) ? (source as ManagedTierSource) : null);

export type TierMutationIntent = Readonly<{
  kind: 'add' | 'remove';
  tier: string;
}>;

export type TierMutationPayload = Readonly<{
  previous: string[];
  next: string[];
}>;

export const tierMutationPayload = (
  source: Source,
  modelId: string,
  intent: TierMutationIntent,
): TierMutationPayload | null => {
  const model = source.models.find((candidate) => candidate.id === modelId);
  if (!model) return null;
  // Read off the source the write is about to be sent against, which is the
  // freshest copy this client holds. That makes the lock structural rather than
  // a rule every affordance has to remember to hide, and closes the window
  // where a refresh landed the provenance after the editor was already open.
  if (managedTierSource(model.reasoning_efforts_source)) return null;
  const previous = [...model.reasoning_efforts];
  const next = intent.kind === 'add'
    ? previous.includes(intent.tier) ? previous : [...previous, intent.tier]
    : previous.filter((tier) => tier !== intent.tier);
  return { previous, next };
};
