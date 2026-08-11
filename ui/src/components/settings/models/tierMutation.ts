import type { Source } from './types';

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
  const previous = [...model.reasoning_efforts];
  const next = intent.kind === 'add'
    ? previous.includes(intent.tier) ? previous : [...previous, intent.tier]
    : previous.filter((tier) => tier !== intent.tier);
  return { previous, next };
};
