import { describe, expect, it } from 'vitest';

import { createWorkbenchSessionReadOwnership } from './workbenchSessionReadOwnership';

describe('Workbench session read ownership', () => {
  it('accepts only the newest read in the current mutation epoch', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const older = ownership.beginRead();
    const newer = ownership.beginRead();

    expect(ownership.isCurrent(older)).toBe(false);
    expect(ownership.isCurrent(newer)).toBe(true);
  });

  it('invalidates reads issued before an accepted session mutation', () => {
    const ownership = createWorkbenchSessionReadOwnership();
    const read = ownership.beginRead();

    expect(ownership.epoch()).toBe(0);
    expect(ownership.acceptMutation()).toBe(1);
    expect(ownership.isCurrent(read)).toBe(false);
    expect(ownership.isLatestRead(read)).toBe(true);
  });
});
