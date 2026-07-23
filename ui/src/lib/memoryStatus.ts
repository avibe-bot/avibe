export type MemoryStatusCounts = Record<string, unknown>;

const count = (status: MemoryStatusCounts, name: string): number =>
  typeof status[name] === 'number' ? status[name] : 0;

export const memoryStatusBuckets = (status: MemoryStatusCounts) => ({
  syncing: count(status, 'pending') + count(status, 'processing') + count(status, 'awaiting_receipt'),
  succeeded: count(status, 'succeeded'),
  unknown: count(status, 'receipt_unknown'),
  failed: count(status, 'distill_failed'),
  dead: count(status, 'dead'),
  missed: count(status, 'missed'),
});
