import { describe, expect, it } from 'vitest';

import { sessionActivityInboxAction } from './inboxActivity';

describe('sessionActivityInboxAction (contract A6)', () => {
  it('drops the card when the session moved to background', () => {
    expect(sessionActivityInboxAction({ event: 'updated', visibility: 'background' })).toBe('drop');
  });

  it('reconciles the feed when the session moved to foreground', () => {
    expect(sessionActivityInboxAction({ event: 'updated', visibility: 'foreground' })).toBe('reconcile');
  });

  it('still drops on an explicit archive event', () => {
    expect(sessionActivityInboxAction({ event: 'archived' })).toBe('drop');
  });

  it('no-ops for an ordinary activity event without visibility (defensive pre-M1)', () => {
    expect(sessionActivityInboxAction({ event: 'updated' })).toBe('ignore');
    expect(sessionActivityInboxAction({ event: 'activity', visibility: undefined })).toBe('ignore');
  });
});
