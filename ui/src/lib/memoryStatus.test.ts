import { describe, expect, it } from 'vitest';

import { memoryStatusBuckets } from './memoryStatus';


describe('memoryStatusBuckets', () => {
  it('derives all six display buckets without treating unknown as success', () => {
    expect(memoryStatusBuckets({
      pending: 1,
      processing: 2,
      awaiting_receipt: 3,
      succeeded: 4,
      receipt_unknown: 5,
      distill_failed: 6,
      dead: 7,
      missed: 8,
    })).toEqual({
      syncing: 6,
      succeeded: 4,
      unknown: 5,
      failed: 6,
      dead: 7,
      missed: 8,
    });
  });
});
