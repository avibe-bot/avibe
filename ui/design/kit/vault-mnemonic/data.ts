// Public data for the seed-phrase wallet proposal (docs: _tmp/vault-mnemonic/design-v2.md).
// Both phrases are published BIP39 test vectors, never a wallet: `legal winner …` is a Trezor
// vector, `abandon … about` the BIP86 one. Every address below was derived from them with
// @scure/bip32 + SLIP-0010 and Avibe's own storage/vault_addresses.py (empty passphrase):
// EVM m/44'/60'/0'/0/i, Bitcoin Taproot m/86'/0'/0'/0/i, Solana m/44'/501'/i'/0'.
import type { SigningAddresses } from '@/context/ApiContext';

export type MnemonicChain = 'evm' | 'bitcoin' | 'solana';

export const CREATED_WORDS = 'legal winner thank year wave sausage worth useful legal winner thank yellow'.split(' ');
export const IMPORTED_WORDS = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'.split(' ');

type Triple = Required<Pick<SigningAddresses, 'eth' | 'btc_taproot' | 'solana'>>;

export const CREATED_ADDRESSES: Triple[] = [
  { eth: '0x58A57ed9d8d624cBD12e2C467D34787555bB1b25', btc_taproot: 'bc1pgp80xvc97ssyphukzd5w6gmz2z45dzawl8sn3g6r9e95l90yvdfqa9879z', solana: 'BLeUXTx9thHGT7VJUtF9vHEmfMDgW1nnKZ9UVer2CoLX' },
  { eth: '0x0D3eB21b6b21833A4939Cfff4810E9AE0758e12C', btc_taproot: 'bc1p8t3tw3jvt5urqxjxp8jaex0ecca48l02qm0a85nlaanq2p9lqdvsgglqxq', solana: 'EdjcxP8MmXP4yRHguEVoH75kbXVfZNFXPgNfL9NqcXXK' },
  { eth: '0xe42f4612e154153B68e241e8FDe337e0c4dD6bBD', btc_taproot: 'bc1pkhtuh934jgr2k94pfhc2590jn4nk30svhcj2htta0l4hjx7qudpqdkhm7z', solana: 'AFG4eoTGSCdNemYFQhhJBqT7tJwo3WdC4Raeq9SkXx7p' },
  { eth: '0x2F07c220dC62CE9531bC44B695A6D93578806d8d', btc_taproot: 'bc1pa8rgpzfjr4r0q6vkyxwjnegd969lr64k9adustnme7ckzdv6r8cqe2zemq', solana: 'gBXdHgqD3g2ChGjHveWiPXSGMRXRTNwbieiBWtKVtzJ' },
];

export const IMPORTED_ADDRESSES: Triple[] = [
  { eth: '0x9858EfFD232B4033E47d90003D41EC34EcaEda94', btc_taproot: 'bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr', solana: 'HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk' },
  { eth: '0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0', btc_taproot: 'bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh', solana: 'Hh8QwFUA6MtVu1qAoq12ucvFHNwCcVTV7hpWjeY1Hztb' },
  { eth: '0xb6716976A3ebe8D39aCEB04372f22Ff8e6802D7A', btc_taproot: 'bc1p0d0rhyynq0awa9m8cqrcr8f5nxqx3aw29w4ru5u9my3h0sfygnzs9khxz8', solana: '7WktogJEd2wQ9eH2oWusmcoFTgeYi6rS632UviTBJ2jm' },
  { eth: '0xF3f50213C1d2e255e4B2bAD430F8A38EEF8D718E', btc_taproot: 'bc1py0vryk8aqusz65yzuudypggvswzkcpwtau8q0sjm0stctwup0xlqkkxler', solana: '3YqEpfo3c818GhvbQ1UmVY1nJxw16vtu4JB9peJXT94k' },
];

export const path = (chain: MnemonicChain, index: number) =>
  chain === 'evm' ? `m/44'/60'/0'/0/${index}` : chain === 'bitcoin' ? `m/86'/0'/0'/0/${index}` : `m/44'/501'/${index}'/0'`;

/** One account of a wallet, as the wallet view lists it. Names and activity are draft fixtures. */
export type WalletAccount = {
  index: number;
  name: string;
  nameEn: string;
  addresses: Triple;
  /** Asks before every signature with this account. */
  ask?: boolean;
  /** Last signature: chain, minutes ago, and the Session that asked for it. */
  last?: { chain: MnemonicChain; minutes: number; session: string; sessionEn: string };
};

export const WALLET_ACCOUNTS: WalletAccount[] = [
  { index: 0, name: '主账户', nameEn: 'Main', addresses: IMPORTED_ADDRESSES[0], ask: true, last: { chain: 'evm', minutes: 60 * 26, session: '测试网部署合约', sessionEn: 'Deploy testnet contract' } },
  { index: 1, name: '交易机器人', nameEn: 'Trading bot', addresses: IMPORTED_ADDRESSES[1], last: { chain: 'solana', minutes: 120, session: '网格交易', sessionEn: 'Grid trading' } },
  { index: 2, name: '空投测试', nameEn: 'Airdrop tests', addresses: IMPORTED_ADDRESSES[2] },
];

/** The 150-byte Solana legacy message the shipped approval frames sign (vault/solana): 0.01 SOL. */
export const SOLANA_TRANSFER_BYTES = 150;
