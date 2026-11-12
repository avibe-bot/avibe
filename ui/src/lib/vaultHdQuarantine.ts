// Seed-phrase wallets whose post-save address check failed (the stored #0 is not the address the
// words give). The dialog deletes such a wallet at once; if that delete cannot be confirmed, the
// wallet's name is kept here so this browser keeps it blocked (no addresses, no accounts dialog,
// a "do not fund" warning) across closing the dialog and reloading, until the wallet is gone.
const KEY = 'avibe.vault.hdAddressCheckFailed';

const read = (): string[] => {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(KEY) ?? '[]');
    return Array.isArray(value) ? value.filter((name): name is string => typeof name === 'string') : [];
  } catch {
    return [];
  }
};
const write = (names: string[]) => {
  try {
    if (names.length) globalThis.localStorage?.setItem(KEY, JSON.stringify(names));
    else globalThis.localStorage?.removeItem(KEY);
  } catch { /* storage unavailable: the in-dialog block still holds */ }
};

export const isHdWalletBlocked = (name: string) => read().includes(name);
export const blockHdWallet = (name: string) => { if (!read().includes(name)) write([...read(), name]); };
export const unblockHdWallet = (name: string) => write(read().filter((entry) => entry !== name));
/** Forget names no longer listed: a deleted wallet's name may be reused by a new one. */
export const pruneHdWalletBlocks = (listed: readonly string[]) => write(read().filter((name) => listed.includes(name)));
