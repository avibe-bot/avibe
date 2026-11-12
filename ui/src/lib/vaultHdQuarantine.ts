// Seed-phrase wallets whose post-save address check failed (the stored #0 is not the address the
// words give). The dialog deletes such a wallet at once; if that delete cannot be confirmed, the
// wallet's name is kept here so this browser keeps it blocked (no addresses, no accounts dialog,
// a "do not fund" warning) across closing the dialog and reloading, until the wallet is gone.
// Defense in depth only: the server-side pre-commit check is the real boundary.
const KEY = 'avibe.vault.hdAddressCheckFailed';

type Block = { name: string; at: number };

// Kept in memory as well, so a browser that refuses storage still blocks for this page's life.
let memory: Block[] = [];

const read = (): Block[] => {
  let stored: Block[] = [];
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(KEY) ?? '[]');
    if (Array.isArray(value)) {
      stored = value.flatMap((entry): Block[] => (
        typeof entry === 'string' ? [{ name: entry, at: 0 }]
          : entry && typeof entry.name === 'string' && typeof entry.at === 'number' ? [{ name: entry.name, at: entry.at }] : []
      ));
    }
  } catch { /* unavailable: memory still holds */ }
  const merged = new Map<string, Block>();
  for (const block of [...stored, ...memory]) merged.set(block.name, block);
  return [...merged.values()];
};
const write = (blocks: Block[]) => {
  memory = blocks;
  try {
    if (blocks.length) globalThis.localStorage?.setItem(KEY, JSON.stringify(blocks));
    else globalThis.localStorage?.removeItem(KEY);
  } catch { /* storage unavailable: the in-memory copy still blocks */ }
};

export const isHdWalletBlocked = (name: string) => read().some((block) => block.name === name);
export const blockHdWallet = (name: string, now = Date.now()) => {
  write([...read().filter((block) => block.name !== name), { name, at: now }]);
};
export const unblockHdWallet = (name: string) => write(read().filter((block) => block.name !== name));
/** Forget wallets a list no longer shows, but only blocks made before that list was requested: a
 *  list begun earlier says nothing about a wallet created (and blocked) after it started. */
export const pruneHdWalletBlocks = (listed: readonly string[], requestedAt: number) => write(
  read().filter((block) => listed.includes(block.name) || block.at >= requestedAt),
);
