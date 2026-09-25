import { afterEach, describe, expect, it, vi } from 'vitest';

import { modelsApi } from '../settings/models/modelsApi';
import type { TurnProvenance } from '../settings/models/types';
import { RECORDS_LIMIT, readRecord, resetFailureDetailsCache } from './failureDetailsReads';

describe('failureDetailsReads', () => {
  afterEach(() => {
    resetFailureDetailsCache();
    vi.restoreAllMocks();
  });

  it('keeps only the most recently read Turn records', async () => {
    const read = vi.spyOn(modelsApi, 'getTurnProvenance')
      .mockImplementation(async (turnId) => ({ turn_id: turnId } as unknown as TurnProvenance));
    for (let index = 0; index <= RECORDS_LIMIT; index += 1) await readRecord(`turn_${index}`);
    expect(read).toHaveBeenCalledTimes(RECORDS_LIMIT + 1);

    await readRecord(`turn_${RECORDS_LIMIT}`);
    expect(read).toHaveBeenCalledTimes(RECORDS_LIMIT + 1);
    await readRecord('turn_0');
    expect(read).toHaveBeenCalledTimes(RECORDS_LIMIT + 2);
  });
});
