// The protocol-family suggestion lists, read from the JSON `tierSuggestions.test.ts`
// holds equal to the product table. This file exists because the e2e tsconfig
// cannot import `@/` aliases; the vitest is what keeps the copy from drifting.
import { readFileSync } from 'node:fs';

export const PROTOCOL_TIER_SUGGESTIONS = JSON.parse(
  readFileSync(new URL('./protocol-tiers.json', import.meta.url), 'utf8'),
) as Record<string, readonly string[]>;
