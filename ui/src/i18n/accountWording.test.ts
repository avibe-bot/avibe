import { describe, expect, it } from 'vitest';

import zh from './zh.json';

// The signed-in avibe.bot account is 「账号」 (product decision, 2026-11-15). 「账户」 belongs to Vault
// wallet accounts and provider balances, which live outside the App's account and shell copy.
const strings = (value: unknown, path: string): [string, string][] => {
  if (typeof value === 'string') return [[path, value]];
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, entry]) => strings(entry, `${path}.${key}`));
  }
  return [];
};

describe('sign-in account wording', () => {
  it('calls the signed-in account 账号 in the App and shell copy', () => {
    const offending = [
      ...strings(zh.mobileApp, 'mobileApp'),
      ...strings(zh.appShell, 'appShell'),
    ].filter(([, text]) => text.includes('账户'));
    expect(offending).toEqual([]);
  });
});
