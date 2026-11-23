import { expect, it } from 'vitest';

import { SUPPORT_EMAIL } from './support';

// Fails until the owner has chosen the support address: a "contact us" must name a real one.
it('names a support address before anything ships that asks people to write to it', () => {
  expect(SUPPORT_EMAIL).toMatch(/^[^\s@]+@[^\s@]+\.[^\s@]+$/);
});
