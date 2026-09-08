import type { SuppliedModel } from './types';

// Ordered preferences, not invented inventory. These exact IDs are already
// present in the bundled backend catalog; only upstream/manual rows may win.
// Keep tuning this small list separate from provider/model capability metadata.
export const PREFERRED_TEST_MODEL_IDS = [
  'gpt-5.6-luna',
  'claude-sonnet-5',
  'gpt-5.4-mini',
  'claude-sonnet-4-6',
] as const;

export const selectTestModel = (
  models: readonly SuppliedModel[],
  selected = '',
): string => {
  const ids = models.filter((model) => !model.retired).map((model) => model.id);
  if (selected && ids.includes(selected)) return selected;
  return PREFERRED_TEST_MODEL_IDS.find((id) => ids.includes(id)) ?? ids[0] ?? '';
};
