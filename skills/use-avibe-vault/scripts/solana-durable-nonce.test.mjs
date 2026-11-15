import assert from 'node:assert/strict';
import { createPrivateKey, createPublicKey, sign } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { Message, NONCE_ACCOUNT_LENGTH, PublicKey, SystemInstruction, SystemProgram, Transaction } from '@solana/web3.js';
import { assemble, buildBootstrap, buildTransfer, signingContext } from './solana-durable-nonce.mjs';

// Public RFC 8032 test seed, never a user's key. The recipe does not sign.
const privateKey = createPrivateKey({
  key: Buffer.from('302e020100300506032b6570042204209d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60', 'hex'),
  format: 'der', type: 'pkcs8',
});
const signer = new PublicKey(createPublicKey(privateKey).export({ format: 'der', type: 'spki' }).subarray(-32));
const nonceAccount = new PublicKey(Buffer.alloc(32, 2));
const nonce = new PublicKey(Buffer.alloc(32, 3)).toBase58();
const destination = new PublicKey(Buffer.alloc(32, 4));
const args = { signer, nonceAccount, nonce, destination, lamports: 123456789n };
const signature = hex => sign(null, Buffer.from(hex, 'hex'), privateKey).toString('hex');

test('nonce is the lifetime and AdvanceNonceAccount is first; exact signer, accounts and amount', () => {
  const hex = buildTransfer(args);
  const fixture = JSON.parse(readFileSync(new URL('../../../tests/fixtures/vault_signing/solana-durable-nonce.json', import.meta.url)));
  assert.equal(hex, fixture.messageHex);
  const message = Message.from(Buffer.from(hex, 'hex'));
  assert.equal(message.recentBlockhash, nonce);
  assert.equal(message.header.numRequiredSignatures, 1);
  assert.ok(message.accountKeys[0].equals(signer));
  const tx = Transaction.populate(message);
  assert.equal(tx.instructions.length, 2);
  const advance = tx.instructions[0];
  assert.ok(advance.programId.equals(SystemProgram.programId));
  assert.deepEqual([...advance.data], [4, 0, 0, 0]);
  assert.ok(advance.keys[0].pubkey.equals(nonceAccount));
  assert.equal(advance.keys[0].isWritable, true);
  assert.ok(advance.keys[2].pubkey.equals(signer));
  assert.equal(advance.keys[2].isSigner, true);
  const transfer = SystemInstruction.decodeTransfer(tx.instructions[1]);
  assert.equal(transfer.lamports, 123456789n);
  assert.ok(transfer.toPubkey.equals(destination));
  assert.deepEqual(signingContext(hex), { kind: 'solana-transaction-v1', messageHex: hex });
});

test('bootstrap uses seeded account and initialization, no additional account keypair', async () => {
  const { nonceAccount: address, messageHex } = await buildBootstrap({ signer, seed: 'test-nonce-1', rentLamports: 1500000, recentBlockhash: nonce });
  assert.ok(address.equals(await PublicKey.createWithSeed(signer, 'test-nonce-1', SystemProgram.programId)));
  const message = Message.from(Buffer.from(messageHex, 'hex'));
  assert.equal(message.header.numRequiredSignatures, 1);
  const [create, initialize] = Transaction.populate(message).instructions;
  const decoded = SystemInstruction.decodeCreateWithSeed(create);
  assert.equal(decoded.space, NONCE_ACCOUNT_LENGTH);
  assert.equal(decoded.lamports, 1500000);
  assert.ok(decoded.newAccountPubkey.equals(address));
  assert.ok(decoded.basePubkey.equals(signer));
  assert.ok(SystemInstruction.decodeNonceInitialize(initialize).authorizedPubkey.equals(signer));
  assert.deepEqual(Transaction.from(assemble(messageHex, signer, signature(messageHex))).serializeMessage(), Buffer.from(messageHex, 'hex'));
});

test('approval delay never changes bytes; fresh nonce cannot reuse an old signature', () => {
  const hex = buildTransfer(args);
  const signed = signature(hex);
  const wire = assemble(hex, signer, signed);
  assert.deepEqual(Transaction.from(wire).serializeMessage(), Buffer.from(hex, 'hex'));
  const changed = buildTransfer({ ...args, nonce: new PublicKey(Buffer.alloc(32, 5)).toBase58() });
  assert.throws(() => assemble(changed, signer, signed), /Signature verification failed/);
  assert.throws(() => assemble(hex, destination, signed), /only signer/);
  assert.throws(() => assemble(hex, signer, '00'.repeat(64)), /Signature verification failed/);
});

test('reject imprecise funding/amount and invalid signature encodings', async () => {
  await assert.rejects(buildBootstrap({ signer, seed: 'x', rentLamports: NaN, recentBlockhash: nonce }), /rentLamports/);
  for (const lamports of [1, 0n, -1n, 2n ** 64n]) assert.throws(() => buildTransfer({ ...args, lamports }), /u64 bigint/);
  assert.throws(() => assemble(buildTransfer(args), signer, 'ff'), /64-byte signature/);
});
