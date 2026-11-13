// Fixtures for the BTC address-type switch proposal. Seed wallet: the `abandon … about` BIP39 test
// vector (published, never a wallet). Taproot m/86'/0'/0'/0/i (BIP86 vector), SegWit
// m/84'/0'/0'/0/i (BIP84 vector), Legacy m/44'/0'/0'/0/i (BIP44): derived with @scure/bip32. The
// seed wallet's SegWit/Legacy paths follow the backend contract once it is set.
export type BtcType = 'taproot' | 'segwit' | 'legacy';
export type BtcAddresses = Partial<Record<BtcType, string>>;

export const SEED_ACCOUNT0 = {
  eth: '0x9858EfFD232B4033E47d90003D41EC34EcaEda94',
  solana: 'HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk',
  btc: {
    taproot: 'bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr',
    segwit: 'bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu',
    legacy: '1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA',
  } satisfies BtcAddresses,
};

export const SEED_ACCOUNT1 = {
  eth: '0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0',
  solana: 'Hh8QwFUA6MtVu1qAoq12ucvFHNwCcVTV7hpWjeY1Hztb',
  btc: {
    taproot: 'bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh',
    segwit: 'bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g',
    legacy: '1Ak8PffB2meyfYnbXZR9EGfLfFZVpzJvQP',
  } satisfies BtcAddresses,
};

/** A standard secp256k1 signing key (ETH_HOT_KEY in the vault fixtures): all three types sign. */
export { syntheticAddresses as KEY_ADDRESSES } from '../../frames/vault/fixtures';

/** Only Taproot can sign (a protected key today: typed Taproot only): one address, no switch. */
export const ONLY_TAPROOT: BtcAddresses = { taproot: SEED_ACCOUNT0.btc.taproot };
