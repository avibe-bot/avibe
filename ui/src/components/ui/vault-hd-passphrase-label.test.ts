// The seed-wallet dialog names the passphrase one way when creating and when importing a wallet
// (3.2.8): the import section is titled with the create field's own label, never "BIP39 passphrase"
// or "BIP39 密码", so a person bringing a wallet over and one making a new one read the same word.
import { expect, it } from 'vitest';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

it.each([['en', en], ['zh', zh]] as const)('%s: create and import name the passphrase the same way', (_lang, catalog) => {
  const hd = catalog.vaults.hd;
  expect(hd.import.passphrase.startsWith(hd.passphrase.label)).toBe(true);
  for (const text of [hd.import.passphrase, catalog.vaults.hd.importInfo]) expect(text).not.toMatch(/BIP39/);
});
