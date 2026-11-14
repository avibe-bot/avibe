// Caller-side recipe only: no private keys, RPC, Vault calls, funding or broadcast.
// These builders deliberately use one existing Vault signer as payer/base/authority.
import { Message, NONCE_ACCOUNT_LENGTH, PublicKey, SystemProgram, Transaction } from '@solana/web3.js';

export async function buildBootstrap({ signer, seed, rentLamports, recentBlockhash }) {
  if (!Number.isSafeInteger(rentLamports) || rentLamports <= 0) {
    throw new Error('rentLamports must be the positive RPC-reported rent-exempt minimum');
  }
  const nonceAccount = await PublicKey.createWithSeed(signer, seed, SystemProgram.programId);
  const transaction = new Transaction({ feePayer: signer, recentBlockhash }).add(
    SystemProgram.createAccountWithSeed({
      fromPubkey: signer, basePubkey: signer, seed, newAccountPubkey: nonceAccount,
      lamports: rentLamports, space: NONCE_ACCOUNT_LENGTH, programId: SystemProgram.programId,
    }),
    SystemProgram.nonceInitialize({ noncePubkey: nonceAccount, authorizedPubkey: signer }),
  );
  return { nonceAccount, messageHex: transaction.serializeMessage().toString('hex') };
}

export function buildTransfer({ signer, nonceAccount, nonce, destination, lamports }) {
  if (typeof lamports !== 'bigint' || lamports <= 0n || lamports > 0xffffffffffffffffn) {
    throw new Error('lamports must be a positive u64 bigint');
  }
  const transaction = new Transaction({
    feePayer: signer,
    nonceInfo: {
      nonce,
      nonceInstruction: SystemProgram.nonceAdvance({ noncePubkey: nonceAccount, authorizedPubkey: signer }),
    },
  }).add(SystemProgram.transfer({ fromPubkey: signer, toPubkey: destination, lamports }));
  return transaction.serializeMessage().toString('hex');
}

export function signingContext(messageHex) {
  return { kind: 'solana-transaction-v1', messageHex };
}

export function assemble(messageHex, signer, signatureHex) {
  if (!/^(?:[0-9a-f]{2})+$/.test(messageHex) || !/^[0-9a-f]{128}$/.test(signatureHex)) {
    throw new Error('expected exact lowercase message hex and a 64-byte signature');
  }
  const bytes = Buffer.from(messageHex, 'hex');
  const message = Message.from(bytes);
  if (message.header.numRequiredSignatures !== 1 || !message.accountKeys[0].equals(signer)) {
    throw new Error('this recipe requires the Vault signer to be the only signer and payer');
  }
  const transaction = Transaction.populate(message);
  if (!transaction.serializeMessage().equals(bytes)) throw new Error('message bytes changed');
  transaction.addSignature(signer, Buffer.from(signatureHex, 'hex'));
  // serialize requires every signature and verifies it over the original message.
  return transaction.serialize({ requireAllSignatures: true, verifySignatures: true });
}
