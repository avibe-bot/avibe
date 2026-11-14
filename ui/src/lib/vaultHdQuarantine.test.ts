import { afterEach, describe, expect, it, vi } from 'vitest';

import { blockHdWallet, isHdWalletBlocked, pruneHdWalletBlocks, unblockHdWallet } from './vaultHdQuarantine';

afterEach(() => { unblockHdWallet('W'); vi.unstubAllGlobals(); });

describe('address-check block (defense in depth)', () => {
  it('still blocks when the browser refuses storage', () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('denied'); }, removeItem: () => { throw new Error('denied'); } });
    blockHdWallet('W');
    expect(isHdWalletBlocked('W')).toBe(true);
  });

  it('is not erased by a list that was requested before the block was made', () => {
    const listStarted = 1_000;
    blockHdWallet('W', 2_000);
    pruneHdWalletBlocks([], listStarted);
    expect(isHdWalletBlocked('W')).toBe(true);
    // A list requested after the block that no longer shows the wallet: it is gone.
    pruneHdWalletBlocks([], 3_000);
    expect(isHdWalletBlocked('W')).toBe(false);
  });

  it('keeps blocking while a fresh list still shows the wallet', () => {
    blockHdWallet('W', 2_000);
    pruneHdWalletBlocks(['W'], 3_000);
    expect(isHdWalletBlocked('W')).toBe(true);
  });
});

it('keeps a pending block through pruning until its save resolves, then prunes it like any other', () => {
  blockHdWallet('PENDING_WALLET', 1_000, true);
  // A list requested later that does not show it yet: the save may still be committing.
  pruneHdWalletBlocks([], 2_000);
  expect(isHdWalletBlocked('PENDING_WALLET')).toBe(true);
  // Resolved as unconfirmed: an ordinary block from now on.
  blockHdWallet('PENDING_WALLET', 3_000);
  pruneHdWalletBlocks(['PENDING_WALLET'], 4_000);
  expect(isHdWalletBlocked('PENDING_WALLET')).toBe(true);
  pruneHdWalletBlocks([], 5_000);
  expect(isHdWalletBlocked('PENDING_WALLET')).toBe(false);
});

it('treats a pending block left by a closed page as ordinary once no save could still be running', () => {
  blockHdWallet('LEFT_PENDING', 0, true);
  pruneHdWalletBlocks(['LEFT_PENDING'], 20 * 60_000);
  expect(isHdWalletBlocked('LEFT_PENDING')).toBe(true);
  pruneHdWalletBlocks([], 20 * 60_000);
  expect(isHdWalletBlocked('LEFT_PENDING')).toBe(false);
});
