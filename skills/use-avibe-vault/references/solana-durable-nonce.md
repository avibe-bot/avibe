# Solana: approval-safe transaction lifetime

Use a durable nonce when a Solana transaction needs approval: standard keys with
`always_ask`, the selected standard HD account with `always_ask`, and protected
keys. Inspect `vibe vault list` / `find`; protection and account policy determine
approval, not whether an Agent may call `sign`. If delay is uncertain, prepare a
nonce transaction before requesting signatures. This recipe is for a simple SOL
transfer; never silently rebuild a third-party swap or remove its instructions.

Avibe signs; it does not hold SOL, create/fund accounts, run RPC, or broadcast.
The Agent uses caller-owned tools and explicitly authorized funds. Account rent,
transaction fees, and the payment amount are separate costs. Never disable
ask-before-signing or export a key to make this work.

## Caller-side example (legacy message, one Vault signer)

The adjacent `../scripts/solana-durable-nonce.mjs` is a small **offline builder**
using pinned `@solana/web3.js` 1.x. It neither signs nor submits anything. Copy
that script and its `package.json` / `package-lock.json` to a task-owned directory;
run `npm ci --ignore-scripts` there. Import `buildBootstrap`, `buildTransfer`,
`signingContext`, and `assemble` from the copied module. Use Node.js with ESM.
Use an explicit RPC endpoint for the intended cluster, never an implicit default.

### 1. Provision once, or reuse an initialized account

Use the existing Vault Solana public address as payer, base and nonce authority
(`signer = new PublicKey(address)`). A seeded System account requires no extra
private key. Choose a stable public seed (at most 32 UTF-8 bytes); derive its
address with `PublicKey.createWithSeed(signer, seed, SystemProgram.programId)`.
Check that address on the chosen cluster before creating it. If it already exists,
validate its owner/state/authority as below; never overwrite or recreate it blindly.

For a new account, get `rentLamports` from
`connection.getMinimumBalanceForRentExemption(NONCE_ACCOUNT_LENGTH)`, check the
payer's balance includes rent and fees, and get a fresh blockhash. Call:

```js
const bootstrap = await buildBootstrap({
  signer, seed, rentLamports, recentBlockhash: latest.blockhash,
});
// bootstrap.nonceAccount is the public account address.
// bootstrap.messageHex contains CreateAccountWithSeed + InitializeNonceAccount.
```

Sign/assemble it as in step 3 and submit promptly; confirm with its original
blockhash/lastValidBlockHeight. **Bootstrap itself can expire while approval is
pending.** Arrange prompt owner approval, use an existing nonce in a separately
constructed bootstrap, or ask the owner to provision the account. This recipe's
bootstrap uses a recent blockhash, not a recursive nonce workaround. If it expires,
reconcile its status/account state before requesting approval for rebuilt bytes.

### 2. Fetch and reserve the nonce, then build the payment

Fetch the account with `connection.getAccountInfo(nonceAccount, 'confirmed')`.
Require a non-executable System Program-owned account with exactly
`NONCE_ACCOUNT_LENGTH` bytes, current version 1 and initialized state 1 (little-endian
u32 at offsets 0 and 4). Decode using `NonceAccount.fromAccountData(info.data)`;
require `authorizedPubkey.equals(signer)`. Use its stored `nonce`, **not** a new
RPC blockhash. Serialize pending operations per nonce account; do not build two
payments against the same value.

```js
const messageHex = buildTransfer({
  signer, nonceAccount, nonce: decoded.nonce, destination,
  lamports: 1000000n, // Example only: supply the authorized amount in lamports.
});
const context = signingContext(messageHex);
```

The builder places exact System `AdvanceNonceAccount` at instruction **0** and the
stored nonce in `recent_blockhash`, followed by the transfer. Keep advance first,
even if adding compute-budget instructions. Other builders may require more
signers: all must sign the same exact bytes. This example intentionally has only
one signer and refuses other signer layouts during assembly.

### 3. Request approval once, then use the returned signature

Save the exact message hex, nonce account/value, cluster, payer/authority and
payment intent in task-owned state. Set `MESSAGE_HEX` to that saved hex, not a hash
or a full transaction packet. For an ordinary key:

```bash
vibe vault sign SOLANA_KEY --scheme ed25519 --message-hex "$MESSAGE_HEX" \
  --command "sign the prepared durable-nonce payment"
```

For a protected key add the exact matching context:

```bash
--signing-context-json "{\"kind\":\"solana-transaction-v1\",\"messageHex\":\"$MESSAGE_HEX\"}"
```

For standard HD use `--chain solana --account N` instead of `--scheme`. A pending
result carries `request.solana_lifetime_hint`: conditional advice, not proof that
the input has a recent blockhash or a valid nonce. End the turn for the existing
callback. On resumption follow its result-retrieval instruction; with
`--no-callback`, read `vibe vault await REQUEST_ID` yourself. Do not rerun `sign`.
After approval, use the returned 64-byte signature hex:

```js
const wire = assemble(messageHex, signer, signatureHex);
// Only after the checks below, submit with connection.sendRawTransaction(wire).
```

`assemble` verifies the signature and requires the message bytes to remain
identical. Before submission re-fetch nonce/authority/account state and check
balances/intent; a recognized advance in the sandbox proves only encoded facts,
not on-chain validity. Never replace a nonce/blockhash under an old approval.

### 4. Settle before reuse

Use nonce-aware confirmation (the nonce account/value and signature), not a
recent-blockhash `lastValidBlockHeight` strategy for the nonce payment. Record the
signature before sending; after a timeout, reconcile signature status and nonce
state before creating any replacement payment. A nonce can be consumed even when
a later instruction fails, with fees charged. Refetch before the next operation.
Account closure, authority changes or nonce consumption invalidate old bytes.
Changed bytes require fresh signatures/approval. Durable nonces do not extend
Avibe's request expiry or guarantee execution; never automatically repay after an
ambiguous submission.

Primary reference: Solana official documentation, **Durable Nonces**
(`solana.com/docs/core/transactions/durable-nonces`), checked November 14, 2026.
