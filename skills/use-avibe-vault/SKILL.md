---
name: use-avibe-vault
slug: use-avibe-vault
description: Use Avibe Vault for API keys, tokens, passwords, protected credentials, authenticated HTTP requests, or signing digests and Solana messages without exposing secret values to the agent.
version: 0.11.0
---

# Use Avibe Vault

When a task needs API keys, access tokens, passwords, wallet private keys, or other sensitive credentials, prefer Avibe Vault: agents reference secrets by name, tag, or skill tag, and users do not need to paste plaintext into chat.

Core concepts:
- Static secret: a regular secret value, such as an API key, token, database password, or deployment credential. Use it with `vibe vault run` for environment injection or `vibe vault fetch` for authenticated HTTP egress.
- A static secret can contain up to 32 named fields. `NAME` selects its default field (existing secrets call it `value`); `NAME.field` selects that field, including an explicit default label such as `GITHUB_LOGIN.password`. Field names match `^[a-z][a-z0-9_]{0,63}$`. The default label may be renamed; extra labels and the choice of default cannot change after creation.
- Keypair secret: a signing key for digests or transactions, such as a wallet key or deployment signer. It cannot be exported as an environment variable and cannot be used with `run` / `fetch`; use `vibe vault sign`.
- HD seed wallet: a standard-tier signing key whose row has `signer_kind: hd-seed` and an `accounts` list. One seed supplies EVM, Bitcoin and Solana addresses at each shared account index. Prepared standard wallets offer Legacy (P2PKH), Native SegWit (P2WPKH) and Taproot (P2TR) under separate purpose-derived keys; an older unprepared wallet keeps its original Taproot address. It is signing-only, never a value to reveal/export/inject. Only the user creates/imports it, enables accounts, names them and changes ask-before-signing in the browser; never ask for words, a passphrase or a seed in chat.
- Standard: for lower-risk routine automation. Agents can usually use it without interrupting the user unless it is configured to ask first.
- Protected: for high-risk secrets, such as production databases or wallet/funds keys. Because protected secrets are end-to-end encrypted, use requires browser approval and passkey unlock.

Rules:
- Vault approval auto-resume and Vault's `--no-callback` are unchanged by the retirement of Agent Run callbacks. Agent Run `--no-reply` does not disable Vault continuations.
- Refer to secrets only by secret name, record ref (`NAME.field`), tag, or skill tag.
- Static secrets can be used with `run` / `fetch`; keypair secrets can only be used with `vibe vault sign`. Keypairs do not accept field refs.
- HD signing requires `--chain evm|bitcoin|solana`; `--account N` defaults to 0 and must appear in the listed enabled accounts (0–19). EVM uses recoverable ECDSA; Solana uses Ed25519. HD Bitcoin adds optional `--address-type p2pkh|p2wpkh|p2tr`: Legacy/Native SegWit use low-S `ecdsa-secp256k1-der`, Taproot uses `schnorr-secp256k1-bip341-keypath`. Omission remains the original Taproot default. Omit `--scheme`, or supply the matching scheme. `--address-type` requires `--chain bitcoin`; all three HD selectors (`--chain`, `--account`, `--address-type`) are errors on ordinary keypairs, whose existing scheme-based commands remain unchanged.
- Use only the server-listed `bitcoin_address_types` for that account/key. The Web switch selects a displayed receiving address, not the signing default, and never moves funds. If Legacy/Native addresses are missing on an existing seed wallet, ask the user to open its Accounts dialog to prepare them from the stored seed; never ask for words or re-import, re-encode the BIP86 key as BIP84, or substitute another path. Paths are `m/44'/0'/0'/0/i`, `m/84'/0'/0'/0/i`, `m/86'/0'/0'/0/i` for P2PKH/P2WPKH/P2TR: i is the receiving-address index under account 0, not a hardened account index. Existing Taproot addresses never change; do not promise reference-wallet hardened-account-i parity beyond #0.
- HD EVM/Bitcoin take the independently computed 32-byte `--digest`; HD Solana takes the exact serialized `--message-hex`, never its hash. Signing's original HD surface needs avault >=0.1.11; new imports/public preparation and any explicit Bitcoin `--address-type` need avault >=0.1.12. On `avault_upgrade_required`, stop and ask for the approved avault update rather than changing accounts, address types, schemes or protection. If the managed version is below the required floor, wait for the supported release; do not install an unofficial binary or silently fall back to a legacy import.
- HD ask-before-signing is per account and applies across that account's three chains. A pending sign request is bound to its original account and input; do not rerun `sign` or switch accounts to bypass approval. On auto-resume, read the completed request's signature and derivation and continue once. Mnemonic words cannot be viewed again; do not attempt to extract them or reconstruct them from the seed.
- Authorization is per secret: approving a protected secret unlocks **all its fields** for the grant's lifetime. The CLI cannot create, replace, or delete fields; the user does that in the browser.
- `--env NAME.field` delivers one value to `NAME_FIELD` (only the field is uppercased); `--env LOCAL=NAME.field` chooses the variable. `--tag` and `--skill` deliver every field of each selected secret, using `NAME` for the default and `NAME_FIELD` for extras. Conflicting variable mappings fail before delivery.
- `list` and `find` print the same value-free row: a static secret lists its `fields`, and a multi-field secret also names its `default_field`, the field `NAME` selects. If a ref fails with `field_not_found`, use the listed field names rather than guessing.
- `export` remains deprecated and has no delivery. Prefer `run`; advanced `inject` writes a restricted-permission file, so use it only for tools that require file-based configuration.
- With `vibe vault run`, the child process receives static secrets as environment variables, so never run commands that may print env vars, debug config, or secret-bearing errors.
- When protected `run` / `fetch` needs approval, Avibe automatically asks the user to decrypt and authorize access, and the command waits for that approval (540 seconds by default; `--approval-wait SECONDS` or `--no-approval-wait` to change it). If the command then succeeds, it ran once: do not run it again. If it returns `approval_required`, `approval_wait_timeout`, or `approval_handed_off`, the command did not run; Avibe resumes this session once the request is decided, and after approval you run the same command once. Stopping a still-pending waiter leaves continuation to that callback. A fetch response lost after approval completion is different: follow its `execution_unknown` guidance rather than assuming the callback will fire.
- If a Vault command fails with `avault_authorization_required`, avault needs a one-time macOS Keychain authorization on the Mac running Avibe. Do not retry, and do not run `avault` or `vibe vault key authorize` yourself: ask the user to choose Authorize avault on the Vault page (or to run `vibe vault key authorize` on that Mac), then run the command once after they confirm.
- `vault fetch` requires a running, compatible Avibe service. `service_unavailable` with `execution: not_executed` means no fetch ran; start/update the service only with the appropriate authorization. There is no offline SQLite fallback or automatic service start. `execution_unknown` means the result or approval continuation could not be confirmed: do not replay automatically. In Show `api/*.ts` handlers, use the server-only adapter documented by `use-show-pages` instead of spawning a CLI per fetch.
- When protected `sign` needs approval, Avibe creates a browser signing request and returns immediately. Do not rerun `sign`; when Avibe resumes this session, follow the callback instruction to read the completed request result and continue with the returned signature.
- For the listed standard-tier BTC Taproot addresses, use `schnorr-secp256k1-bip341-keypath` (BIP86, empty script tree). It requires an avault build supporting that scheme. Older builds fail closed: never retry with raw BIP340, rotate the key, or change the receiving address as a workaround. No Merkle-root/script-tree option is supported.
- `schnorr-secp256k1-bip340` is raw signing under the internal public key, NOT signing for the listed tweaked Taproot address. `ecdsa-secp256k1-der` produces low-S DER for Bitcoin ECDSA; the caller constructs and validates the transaction sighash and appends any transaction-required sighash byte.
- For Legacy or Native SegWit, build and independently validate the transaction's correct 32-byte sighash FIRST. The signing API returns low-S DER ONLY, with no transaction sighash byte. For an explicit HD Bitcoin request, a successful result also returns `signing_public_key:{curve:"secp256k1",public_key:"<compressed SEC1 hex>"}` for the selected purpose key; for explicit Taproot this is the internal BIP86 point, not the tweaked output key. Use that frozen returned point to verify the DER and confirm HASH160/address before assembly. For the smallest supported caller example use SIGHASH_ALL (`0x01`), append `01` outside DER and assemble P2PKH scriptSig or P2WPKH witness `[DER+01, compressedPublicKey]` (Native scriptSig is empty). BIP143 must bind the exact previous-output amount in satoshis and the selected key's P2PKH scriptCode `76a914<20-byte HASH160>88ac`, NOT the `0014...` witness program. Check previous-output script/key equality, transaction inputs/outputs/fee and returned signature before broadcast. Legacy sighash does not commit to the previous-output amount. Raw-digest signing proves no transaction semantics, UTXO value or intent; no typed Legacy/Native context or protected-tier support is added.
- A protected BTC key signs only one bounded BIP86 key-path payment per request: `--scheme schnorr-secp256k1-bip341-keypath --digest DIGEST` with a `bitcoin-taproot-keypath-v1` `--signing-context-json`. The context has exactly `kind`, `network` (`bitcoin`, `testnet`, `signet` or `regtest`; a requester-selected label, not proof), `unsignedTransaction` (lowercase non-witness hex of at most 137 bytes: version 2, exactly one input, one or two outputs, locktime 0), `prevout` with exactly `amountSats` and `scriptPubKey` (`5120` plus this key's BIP86 output key), `digestAlgorithm: "bip341-sighash-default-keypath"`, and `digest`, the SIGHASH_DEFAULT digest equal to `--digest`. Avibe recomputes that digest and rejects a mismatch, a non-positive fee, or a fee above 10,000 sat/vB. Legacy and SegWit-v0 inputs, multiple inputs, script paths and PSBTs are unsupported. Never substitute raw BIP340, disguise a Bitcoin sighash as an agent operation, or downgrade a protected key. Approval needs the user's browser passkey and a compatible Vault sandbox; signing does not broadcast.
- A Solana key (its row lists `signing_addresses.solana`) signs only with `--scheme ed25519 --message-hex HEX`. `--message-hex` is the exact serialized Solana message bytes, signed as given, at most 1 MiB: it is not a digest, so do not hash the message first. `--digest` is refused with `ed25519`, and `--message-hex` is refused with every secp256k1 scheme. The result is a 64-byte signature with no recovery id. Standard Solana signing needs avault 0.1.10 or later; on `avault_upgrade_required`, ask the user to update avault from the Vault page rather than retrying.
- A protected Solana key signs the same exact message with `--scheme ed25519 --message-hex HEX` plus `--signing-context-json '{"kind":"solana-transaction-v1","messageHex":"HEX"}'`, where `messageHex` is the same nonempty lowercase hex and the context has no other fields. Never hash, shorten or replace the message with a digest. In the browser approval, plain System and SPL token transfers and exact System `AdvanceNonceAccount` instructions are decoded. A nonce advance shows only the nonce account and authority encoded in the message, not the stored nonce value, current on-chain authority, execution success or expiry. Other valid instructions stay raw with a blind-signing notice, so never tell the user what undecoded content does or that its effects were verified. Approval needs the user's browser passkey and a compatible Vault sandbox.
- For Solana **transactions** requiring approval (standard `always_ask`, the selected HD account's `always_ask`, or protected), build with a durable nonce before calling `sign`: a recent blockhash may expire while waiting. Follow [the durable-nonce recipe](references/solana-durable-nonce.md) and its tested caller-side builder in `scripts/solana-durable-nonce.mjs`. The Agent creates/initializes/funds its nonce account using authorized caller funds; Avibe never holds SOL or provisions accounts. Fetch the stored nonce on the intended cluster; put it in `recent_blockhash` and exact System `AdvanceNonceAccount` at instruction 0, before all other instructions. Bootstrap also needs signing: arrange prompt approval or an existing owner-provisioned nonce, never lower policy. Keep one outstanding operation per nonce account; after approval use the saved exact bytes and existing result, not another `sign`. Recheck account/authority/nonce before submission, reconcile uncertain broadcast before replacement, and refetch after use (execution failure can consume nonce and fees). Changed bytes require fresh approval; Avibe request expiry still applies. Pending lifetime hints are conditional advice, not nonce detection or validity proof.
- Protected signing needs `--signing-context-json` with a supported verifiable context: `evm-transaction`, `eip-712-typed-data`, `avault-agent-operation`, `bitcoin-taproot-keypath-v1` or `solana-transaction-v1`. An `avault-agent-operation` preimage must begin with `avault-agent-operation:v1` followed by a real LF newline and nonempty operation text; its digest is SHA256 of those UTF-8 bytes. JSON encodes that newline as `\n`, not `\\n`. This domain is for agent operations only, not transaction authorization. A legacy unbound request must be recreated after both producer and sandbox are upgraded.

Common commands:

Request that the user add a missing static secret. `spec-json` may contain only non-secret prefill metadata; the actual secret value is entered by the user in the browser:
`vibe vault request OPENAI_API_KEY --reason "Need OpenAI API access" --spec-json '{"kind":"static","protection":"protected","description":"OpenAI API key","tags":["openai","prod","skill:model-work"],"policy":{"allowed_hosts":["api.openai.com"],"auth":{"type":"bearer"}}}'`

A request may suggest a generator preset for an application that accepts a user-chosen secret. This is only a hint: the user chooses whether to generate and save, for either tier. Passwords default to 24 characters (12–128); hex and standard padded Base64 default to 32 random bytes (16–64). Do not use a generator to invent a third-party API key:
`vibe vault request APP_SECRET --reason "Configure the app signing secret" --spec-json '{"generate":{"type":"hex","bytes":32}}'`

Request a multi-field login item with a password default. These are field names only, never values:
`vibe vault request GITHUB_LOGIN --reason "Log in to GitHub" --spec-json '{"protection":"protected","fields":["username","password","totp"],"default_field":"password"}'`

A `totp` field is ordinary stored secret material in this version, not a generated one-time code.

For a missing keypair/signing key, ask the user to create a keypair secret in the Vault UI; do not request or store private-key material as a static secret.

On Avibe Web chat, a lighter manual prompt can mention the missing secret as a clickable placeholder in your reply, for example `$<OPENAI_API_KEY>`. The user can click it and fill the secret from Web chat. This has no reason or structured prefill metadata; use `vibe vault request` when those are needed.

List or find existing Vault entries:
`vibe vault list`
`vibe vault list --tag prod`
`vibe vault find --kind static --protection protected`
`vibe vault find openai --tag prod`
`vibe vault tags`

Run a command with selected static secrets injected as environment variables:
`vibe vault run --env OPENAI_API_KEY,GITHUB_TOKEN -- python script.py`
`vibe vault run --env GITHUB_LOGIN.username -- python script.py`
`vibe vault run --env PASSWORD=GITHUB_LOGIN.password -- python script.py`
`vibe vault run --tag deploy -- ./deploy.sh`
`vibe vault run --skill github-release -- ./release.sh`

Make an authenticated HTTP request. The credential is attached only at egress, and the agent never sees the secret:
`vibe vault fetch --auth GITHUB_API.token --url https://api.github.com/user`

The secret's allowed hosts and auth scheme apply to every field; this attaches one field, not a username/password pair.

For tools that require a file, `inject` uses the same selectors and explicit output keys:
`vibe vault inject --env USERNAME=GITHUB_LOGIN.username --env PASSWORD=GITHUB_LOGIN.password --out login.env --format dotenv`
`vibe vault inject --tag deploy --out deploy.json --format json`

Request approval before a protected `run` with an existing static secret:
`vibe vault access GITHUB_LOGIN.password --skill deploy --command "Log in to GitHub" --egress "github.com"`

For protected `fetch`, run `vibe vault fetch`; it creates the correct fetch approval request when needed.

Sign a 32-byte digest with a standard keypair secret. Protected keys additionally require a verifiable signing context and browser approval:
`vibe vault sign WALLET_KEY --digest 0000000000000000000000000000000000000000000000000000000000000001 --scheme ecdsa-secp256k1-recoverable --command "sign deployment transaction"`

Standard-tier BIP86 Taproot scheme example (replace the example digest with the independently verified BIP341 transaction sighash):
`vibe vault sign BTC_KEY --digest 0000000000000000000000000000000000000000000000000000000000000001 --scheme schnorr-secp256k1-bip341-keypath --command "sign verified Bitcoin key-path spend"`

Sign a Solana transaction message with a standard Solana keypair secret. If signing requires approval, build it using the durable-nonce recipe above first. `$MESSAGE_HEX` is the complete serialized transaction message your code built (header, account keys, recent blockhash and instructions), as hex. It is the bytes every signer signs: it does not include the transaction's signature count or signatures, and it is not a hash. Never pass a partial or placeholder message:
`vibe vault sign SOLANA_KEY --scheme ed25519 --message-hex "$MESSAGE_HEX" --command "sign Solana transaction message"`

For a protected Solana key, add the matching context; the command returns a pending browser signing request:
`vibe vault sign SOLANA_KEY --scheme ed25519 --message-hex "$MESSAGE_HEX" --signing-context-json "{\"kind\":\"solana-transaction-v1\",\"messageHex\":\"$MESSAGE_HEX\"}" --command "sign Solana transaction message"`

Sign with an enabled HD wallet account. Account indices are shared across chains; the list identifies the receiving address for each chain. `$EVM_DIGEST_HEX` and `$BITCOIN_SIGHASH_HEX` are independently validated 32-byte transaction digests; `$MESSAGE_HEX` is the complete serialized Solana message:
`vibe vault sign AGENT_WALLET --chain evm --account 0 --digest "$EVM_DIGEST_HEX"`
`vibe vault sign AGENT_WALLET --chain bitcoin --account 1 --digest "$BITCOIN_SIGHASH_HEX"`

For a prepared standard HD account's Legacy or Native SegWit address, compute the corresponding legacy/BIP143 SIGHASH_ALL digest independently; do not reuse the Taproot digest:
`vibe vault sign AGENT_WALLET --chain bitcoin --account 1 --address-type p2pkh --digest "$LEGACY_SIGHASH_HEX"`
`vibe vault sign AGENT_WALLET --chain bitcoin --account 1 --address-type p2wpkh --digest "$BIP143_SIGHASH_HEX"`

Solana still signs the exact message:
`vibe vault sign AGENT_WALLET --chain solana --account 1 --message-hex "$MESSAGE_HEX"`

Ask the user to delete a Vault secret. Every agent-initiated delete needs one-click user approval, including standard, protected, static, and keypair secrets:
`vibe vault remove OLD_TOKEN --reason "Rotated credential"`
This returns a pending request ID without deleting anything. With the normal callback, Avibe resumes this Session after the user decides; do not rerun `rm`. With `--no-callback`, or when the request has no resolvable Session, check explicitly with `vibe vault await <request_id>`; do not rerun `rm`. If a repeated `rm` reuses a pending request created by another Session, use the returned request ID with `vibe vault await <request_id>` rather than expecting a callback for the current Session.

`vibe vault remove` always requests whole-secret deletion. It rejects `NAME.field`; remove a single field from the Vaults page instead.

For more details, run `vibe vault --help`.
