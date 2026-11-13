// Public data for the seed-phrase wallet proposal (docs: _tmp/vault-mnemonic/design-v2.md).
// Both phrases are published BIP39 test vectors, never a wallet: `legal winner …` is a Trezor
// vector, `abandon … about` the BIP86 one. Every address below was derived from them with
// @scure/bip32 + SLIP-0010 and Avibe's own storage/vault_addresses.py (empty passphrase):
// EVM m/44'/60'/0'/0/i, Bitcoin Taproot m/86'/0'/0'/0/i, SegWit m/84'/0'/0'/0/i, Legacy m/44'/0'/0'/0/i,
// Solana m/44'/501'/i'/0' (SegWit/Legacy with @scure/bip32: the BIP84/BIP44 vectors for `abandon … about`).
import type { SigningAddresses } from '@/context/ApiContext';

export type MnemonicChain = 'evm' | 'bitcoin' | 'solana';

export const CREATED_WORDS = 'legal winner thank year wave sausage worth useful legal winner thank yellow'.split(' ');
export const IMPORTED_WORDS = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'.split(' ');

type Triple = Required<Pick<SigningAddresses, 'eth' | 'btc_taproot' | 'btc_segwit' | 'btc_legacy' | 'solana'>>;

export const CREATED_ADDRESSES: Triple[] = [
  { eth: '0x58A57ed9d8d624cBD12e2C467D34787555bB1b25', btc_taproot: 'bc1pgp80xvc97ssyphukzd5w6gmz2z45dzawl8sn3g6r9e95l90yvdfqa9879z', btc_segwit: 'bc1qgkju4yvvtuz0s8vqn837q396jezu2h8ex7gk98', btc_legacy: '1EBuf21icKTE5m3HWVndKx2bTxvqrWCqV6', solana: 'BLeUXTx9thHGT7VJUtF9vHEmfMDgW1nnKZ9UVer2CoLX' },
  { eth: '0x0D3eB21b6b21833A4939Cfff4810E9AE0758e12C', btc_taproot: 'bc1p8t3tw3jvt5urqxjxp8jaex0ecca48l02qm0a85nlaanq2p9lqdvsgglqxq', btc_segwit: 'bc1q0uez0durhkezwzmhfyj5fkjcmqcm56jdhxkq6k', btc_legacy: '1Dchp5ZQ21JajX9oBVQioi5b7Lahg2uEsq', solana: 'EdjcxP8MmXP4yRHguEVoH75kbXVfZNFXPgNfL9NqcXXK' },
  { eth: '0xe42f4612e154153B68e241e8FDe337e0c4dD6bBD', btc_taproot: 'bc1pkhtuh934jgr2k94pfhc2590jn4nk30svhcj2htta0l4hjx7qudpqdkhm7z', btc_segwit: 'bc1qkkvv72p65g0rlca8scwqpsr8yrgcty05vxaa7q', btc_legacy: '1C7QAWYZXjbBb8RJXVtqEyjr2RAYVsmW2x', solana: 'AFG4eoTGSCdNemYFQhhJBqT7tJwo3WdC4Raeq9SkXx7p' },
  { eth: '0x2F07c220dC62CE9531bC44B695A6D93578806d8d', btc_taproot: 'bc1pa8rgpzfjr4r0q6vkyxwjnegd969lr64k9adustnme7ckzdv6r8cqe2zemq', btc_segwit: 'bc1q6awlg7krz9wx8gqaddsgax809nl7859rjtlcu6', btc_legacy: '1PjVBsdL4sYrUL9ksdYUvTVnBNTyb3dE82', solana: 'gBXdHgqD3g2ChGjHveWiPXSGMRXRTNwbieiBWtKVtzJ' },
];

export const IMPORTED_ADDRESSES: Triple[] = [
  { eth: '0x9858EfFD232B4033E47d90003D41EC34EcaEda94', btc_taproot: 'bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr', btc_segwit: 'bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu', btc_legacy: '1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA', solana: 'HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk' },
  { eth: '0x6Fac4D18c912343BF86fa7049364Dd4E424Ab9C0', btc_taproot: 'bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh', btc_segwit: 'bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g', btc_legacy: '1Ak8PffB2meyfYnbXZR9EGfLfFZVpzJvQP', solana: 'Hh8QwFUA6MtVu1qAoq12ucvFHNwCcVTV7hpWjeY1Hztb' },
  { eth: '0xb6716976A3ebe8D39aCEB04372f22Ff8e6802D7A', btc_taproot: 'bc1p0d0rhyynq0awa9m8cqrcr8f5nxqx3aw29w4ru5u9my3h0sfygnzs9khxz8', btc_segwit: 'bc1qp59yckz4ae5c4efgw2s5wfyvrz0ala7rgvuz8z', btc_legacy: '1MNF5RSaabFwcbtJirJwKnDytsXXEsVsNb', solana: '7WktogJEd2wQ9eH2oWusmcoFTgeYi6rS632UviTBJ2jm' },
  { eth: '0xF3f50213C1d2e255e4B2bAD430F8A38EEF8D718E', btc_taproot: 'bc1py0vryk8aqusz65yzuudypggvswzkcpwtau8q0sjm0stctwup0xlqkkxler', btc_segwit: 'bc1qgl5vlg0zdl7yvprgxj9fevsc6q6x5dmcyk3cn3', btc_legacy: '1MVGa13XFvvpKGZdX389iU8b3qwtmAyrsJ', solana: '3YqEpfo3c818GhvbQ1UmVY1nJxw16vtu4JB9peJXT94k' },
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

/** Account #0 of the `abandon … about` phrase with the BIP39 passphrase `TREZOR` (derived once). */
export const IMPORTED_PASSPHRASE_ADDRESS_0: Triple = {
  eth: '0x9c32F71D4DB8Fb9e1A58B0a80dF79935e7256FA6',
  btc_taproot: 'bc1p3ryfth56dp058avv97ppn065ctsk263puvwp4rcka3wpg6cudp9qd3jsuu',
  btc_segwit: 'bc1qv5rmq0kt9yz3pm36wvzct7p3x6mtgehjul0feu',
  btc_legacy: '1PEha8dk5Me5J1rZWpgqSt5F4BroTBLS5y',
  solana: '7zSmbu6gKkb6HB7UDPtHYjwCWuBHU1D4TpNZFm4sndQe',
};
