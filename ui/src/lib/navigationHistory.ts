export function inAppHistoryIndex(historyState: unknown): number | null {
  if (!historyState || typeof historyState !== 'object') return null;

  const index = (historyState as { idx?: unknown }).idx;
  return typeof index === 'number' && Number.isSafeInteger(index) && index >= 0 ? index : null;
}

export function hasInAppBackEntry(historyState: unknown): boolean {
  const index = inAppHistoryIndex(historyState);
  return index !== null && index > 0;
}
