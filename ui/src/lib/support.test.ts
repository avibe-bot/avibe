import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, it } from 'vitest';

import { SUPPORT_EMAIL } from './support';

// Fails until the owner has chosen the support address: a "contact us" must name a real one.
it('names a support address before anything ships that asks people to write to it', () => {
  expect(SUPPORT_EMAIL).toMatch(/^[^\s@]+@[^\s@]+\.[^\s@]+$/);
});

// vibe/support-email.txt is the one source (#4210). Until it exists on this branch there is nothing
// to compare with; once it does, the UI must name the same address.
it('names the same address as vibe/support-email.txt', () => {
  const source = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'vibe', 'support-email.txt');
  if (!existsSync(source)) return;
  expect(SUPPORT_EMAIL).toBe(readFileSync(source, 'utf8').trim());
});
