// Seed-phrase (BIP39) wallets for the Vault, in the browser (docs/plans/vault-hd-wallets.md).
//
// The browser is the only place the words exist: it turns them into the 64-byte BIP39 seed, which
// is sealed for avault and never reaches Python. It also derives the public addresses of an
// account so the user can check them before saving, and so the dialog can compare them with the
// addresses avault reports after saving (the shown address must be the spendable one).
//
// Paths (one account index shared by every chain, as MetaMask and Phantom number accounts):
//   EVM      m/44'/60'/0'/0/i   secp256k1, EIP-55 address
//   Bitcoin  m/86'/0'/0'/0/i    BIP86 Taproot key path, bech32m bc1p (tb1p on test networks)
//   Solana   m/44'/501'/i'/0'   SLIP-0010 ed25519, base58 public key
//
// Vetted libraries only: @scure/bip39 (wordlist, checksum, PBKDF2), @scure/bip32, @noble/curves,
// @noble/hashes, @scure/base. Callers zero the seed bytes once they are sealed.
import { HDKey } from '@scure/bip32';
import { entropyToMnemonic, generateMnemonic, mnemonicToSeedSync, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { base58, bech32, bech32m, createBase58check } from '@scure/base';
import { ed25519 } from '@noble/curves/ed25519.js';
import { schnorr, secp256k1 } from '@noble/curves/secp256k1.js';
import { hmac } from '@noble/hashes/hmac.js';
import { ripemd160 } from '@noble/hashes/legacy.js';
import { sha256, sha512 } from '@noble/hashes/sha2.js';
import { keccak_256 } from '@noble/hashes/sha3.js';
import { bytesToHex, utf8ToBytes } from '@noble/hashes/utils.js';

export const HD_ACCOUNT_LIMIT = 20;
export const HD_WORD_COUNTS = [12, 15, 18, 21, 24] as const;

export type HdChain = 'evm' | 'bitcoin' | 'solana';
export type HdAccountAddresses = {
  account: number;
  eth: string;
  btc_taproot: string;
  /** Native SegWit (P2WPKH, m/84'/0'/0'/0/i) and Legacy (P2PKH, m/44'/0'/0'/0/i), bitcoin-address-types-v1. */
  btc_segwit: string;
  btc_legacy: string;
  solana: string;
  paths: Record<HdChain, string>;
};

export const hdPaths = (account: number): Record<HdChain, string> => ({
  evm: `m/44'/60'/0'/0/${account}`,
  bitcoin: `m/86'/0'/0'/0/${account}`,
  solana: `m/44'/501'/${account}'/0'`,
});

/** The words as BIP39 reads them: NFKD, lower case, single spaces. */
export function normalizeWords(input: string): string[] {
  return input.normalize('NFKD').toLowerCase().trim().split(/\s+/u).filter(Boolean);
}

export type PhraseCheck =
  | { ok: true; words: string[] }
  | { ok: false; reason: 'empty' }
  | { ok: false; reason: 'count'; count: number }
  | { ok: false; reason: 'unknown-word'; position: number; word: string; suggestions: string[] }
  | { ok: false; reason: 'checksum' };

const WORDS = new Set(wordlist);

/** Words of the list that share the typed word's first letters, the closest first. */
export function suggestWords(word: string, limit = 3): string[] {
  for (let prefix = Math.min(4, word.length); prefix >= 1; prefix -= 1) {
    const head = word.slice(0, prefix);
    const found = wordlist.filter((candidate) => candidate.startsWith(head));
    if (found.length > 0) return found.slice(0, limit);
  }
  return [];
}

/** Checks a pasted phrase word by word, then its checksum. The first unknown word is named. */
export function checkPhrase(input: string): PhraseCheck {
  const words = normalizeWords(input);
  if (words.length === 0) return { ok: false, reason: 'empty' };
  const unknown = words.findIndex((word) => !WORDS.has(word));
  if (unknown >= 0) {
    return { ok: false, reason: 'unknown-word', position: unknown + 1, word: words[unknown], suggestions: suggestWords(words[unknown]) };
  }
  if (!(HD_WORD_COUNTS as readonly number[]).includes(words.length)) return { ok: false, reason: 'count', count: words.length };
  if (!validateMnemonic(words.join(' '), wordlist)) return { ok: false, reason: 'checksum' };
  return { ok: true, words };
}

/** Twelve new words (128 bits from the platform CSPRNG). */
export function newPhrase(): string[] {
  return generateMnemonic(wordlist, 128).split(' ');
}

/** The words for given entropy; for tests and fixtures only. */
export function phraseFromEntropy(entropy: Uint8Array): string[] {
  return entropyToMnemonic(entropy, wordlist).split(' ');
}

/** The 64-byte BIP39 seed. The passphrase is NFKD-normalised by BIP39 itself, never trimmed. */
export function wordsToSeed(words: string[], passphrase = ''): Uint8Array {
  return mnemonicToSeedSync(words.join(' '), passphrase);
}

const keccakAddress = (uncompressed: Uint8Array): string => {
  const hex = bytesToHex(keccak_256(uncompressed.slice(1)).slice(-20));
  const hash = bytesToHex(keccak_256(utf8ToBytes(hex)));
  let out = '0x';
  for (let i = 0; i < hex.length; i += 1) out += parseInt(hash[i], 16) >= 8 ? hex[i].toUpperCase() : hex[i];
  return out;
};

const taggedHash = (tag: string, ...parts: Uint8Array[]): Uint8Array => {
  const t = sha256(utf8ToBytes(tag));
  const total = parts.reduce((n, p) => n + p.length, 0);
  const msg = new Uint8Array(64 + total);
  msg.set(t, 0);
  msg.set(t, 32);
  let offset = 64;
  for (const p of parts) { msg.set(p, offset); offset += p.length; }
  return sha256(msg);
};

/** BIP86: the output key is the internal key tweaked by its own TapTweak with no script tree. */
export function taprootOutputKey(compressedInternal: Uint8Array): Uint8Array {
  const point = secp256k1.Point.fromBytes(compressedInternal);
  const xOnly = point.toBytes(true).slice(1);
  // x is a field element (mod p), not a scalar: a valid key may have n <= x < p.
  const even = schnorr.utils.lift_x(secp256k1.Point.Fp.fromBytes(xOnly));
  const tweak = secp256k1.Point.Fn.fromBytes(taggedHash('TapTweak', xOnly));
  return even.add(secp256k1.Point.BASE.multiply(tweak)).toBytes(true).slice(1);
}

export function taprootAddress(outputKey: Uint8Array, network: 'mainnet' | 'testnet' = 'mainnet'): string {
  return bech32m.encode(network === 'mainnet' ? 'bc' : 'tb', [1, ...bech32m.toWords(outputKey)]);
}

/** SLIP-0010 ed25519: hardened-only derivation from the seed. */
function slip10Ed25519(seed: Uint8Array, path: string): Uint8Array {
  let I = hmac(sha512, utf8ToBytes('ed25519 seed'), seed);
  let key = I.slice(0, 32);
  let chain = I.slice(32);
  for (const segment of path.replace(/^m\//, '').split('/')) {
    if (!segment.endsWith("'")) throw new Error('ed25519 derivation is hardened only');
    const index = (Number(segment.slice(0, -1)) + 0x80000000) >>> 0;
    const data = new Uint8Array(37);
    data.set(key, 1);
    new DataView(data.buffer).setUint32(33, index, false);
    I.fill(0);
    I = hmac(sha512, chain, data);
    data.fill(0);
    key.fill(0);
    key = I.slice(0, 32);
    chain = I.slice(32);
  }
  I.fill(0);
  chain.fill(0);
  return key;
}

const base58check = createBase58check(sha256);
const hash160 = (publicKey: Uint8Array) => ripemd160(sha256(publicKey));
/** Mainnet P2WPKH (bc1q…) of a compressed public key. */
export const segwitAddress = (publicKey: Uint8Array) => bech32.encode('bc', [0, ...bech32.toWords(hash160(publicKey))]);
/** Mainnet P2PKH (1…) of a compressed public key. */
export const legacyAddress = (publicKey: Uint8Array) => base58check.encode(Uint8Array.from([0, ...hash160(publicKey)]));

/** The public addresses of accounts `from`..`to - 1`. Private keys are dropped as soon as used. */
export function deriveAccounts(seed: Uint8Array, count = 1, from = 0): HdAccountAddresses[] {
  if (seed.length !== 64) throw new Error('a BIP39 seed is 64 bytes');
  const root = HDKey.fromMasterSeed(seed);
  const out: HdAccountAddresses[] = [];
  try {
    for (let account = from; account < from + count; account += 1) {
      const paths = hdPaths(account);
      const evm = root.derive(paths.evm);
      const btc = root.derive(paths.bitcoin);
      const segwit = root.derive(`m/84'/0'/0'/0/${account}`);
      const legacy = root.derive(`m/44'/0'/0'/0/${account}`);
      const evmPub = secp256k1.Point.fromBytes(evm.publicKey!).toBytes(false);
      const solPriv = slip10Ed25519(seed, paths.solana);
      const solPub = ed25519.getPublicKey(solPriv);
      solPriv.fill(0);
      out.push({
        account,
        eth: keccakAddress(evmPub),
        btc_taproot: taprootAddress(taprootOutputKey(btc.publicKey!)),
        btc_segwit: segwitAddress(segwit.publicKey!),
        btc_legacy: legacyAddress(legacy.publicKey!),
        solana: base58.encode(solPub),
        paths,
      });
      evm.wipePrivateData();
      btc.wipePrivateData();
      segwit.wipePrivateData();
      legacy.wipePrivateData();
    }
  } finally {
    root.wipePrivateData();
  }
  return out;
}

/** One question of the backup check: which word sits at this (1-based) position. */
export type HdQuestion = { position: number; options: string[]; answer: string };

/** Three positions, each with the right word and two others from the phrase, in random order. */
export function makeQuiz(words: string[], random: () => number = Math.random): HdQuestion[] {
  const positions = new Set<number>();
  while (positions.size < 3) positions.add(1 + Math.floor(random() * words.length));
  return [...positions].sort((a, b) => a - b).map((position) => {
    const answer = words[position - 1];
    const others = [...new Set(words.filter((w) => w !== answer))];
    const decoys: string[] = [];
    while (decoys.length < 2 && others.length > 0) decoys.push(others.splice(Math.floor(random() * others.length), 1)[0]);
    const options = [answer, ...decoys];
    for (let i = options.length - 1; i > 0; i -= 1) {
      const j = Math.floor(random() * (i + 1));
      [options[i], options[j]] = [options[j], options[i]];
    }
    return { position, options, answer };
  });
}


/** Whether every question has its answer picked. */
export const quizPassed = (quiz: HdQuestion[], chosen: Record<number, string>) => quiz.every((q) => chosen[q.position] === q.answer);
