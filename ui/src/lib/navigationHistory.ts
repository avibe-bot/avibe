export function hasInAppBackEntry(historyState: unknown): boolean {
  if (!historyState || typeof historyState !== 'object') return false;

  const index = (historyState as { idx?: unknown }).idx;
  return typeof index === 'number' && Number.isFinite(index) && index > 0;
}
