# Solana Vault and protected-signing parity

**Decision: 2026-11-11.** Standard Solana **and** protected Solana/EVM raw signing
must ship in **3.2.6**; stable 3.2.6 waits for both. This replaces the earlier
protected-transfer policy in this plan. Implementation is authorized; every
sandbox/signing PR still requires exact-head owner approval before merge.
**Related:** `avault/docs/DESIGN.md`, `docs/plans/avault-custody-core.md`,
`docs/plans/vault-protected-bitcoin.md`.

## 1. Standard Solana contract (unchanged)

Standard keys are generated, imported, encrypted and signed inside avault.
Avibe receives ciphertext, public metadata and signatures, never the seed.
The existing browser-to-avault blind-box boundary remains: Python relays the
opaque import box, whose context binds operation, name and curve.

- Metadata: `signing_public_key = {curve: "ed25519", chain: "solana",
  public_key: "<32-byte lowercase hex>"}`. Public address: base58 of those bytes,
  exposed as `{solana: "<address>"}`. No network prefix. Existing secp256k1
  records, ETH/BTC addresses and schemes remain unchanged.
- Generation creates a fresh 32-byte seed inside avault and returns its sealed
  envelope, curve and public key. No private-key export/decrypt operation.
- Import accepts only a Solana CLI JSON array of exactly 64 integers in
  `0..255`, or base58 decoding to exactly 64 bytes. Bytes 32–63 must equal the
  public key derived from the first 32 seed bytes. No mnemonics, derivation
  paths, arbitrary 32-byte seed input, PEM or cross-curve imports.
- `ed25519` signs exact supplied message bytes using pure Ed25519, not
  Ed25519ph/ctx or a digest. The CLI/core bound is 1 MiB; empty-message
  Ed25519 remains valid at this standard cryptographic boundary. Solana
  transaction callers supply the serialized unsigned message, not a signed
  wire packet or hash.
- API/request `message_hex` and CLI `--message-hex` are distinct from `digest`
  / `--digest`. Ed25519 rejects digest input; digest schemes reject message
  input and retain their 32-byte digest requirement.
- Result: 64-byte lowercase-hex signature, without a recovery id. Avibe verifies
  it against the stored public key and exact message before settlement/audit.
  Curve/scheme mismatch, malformed input or a bad signature fails closed.
- Headless and `always_ask` standard flows remain available. An always-ask card
  shows the Solana address and exact message, not supposedly decoded effects.

```text
vibe vault sign NAME --scheme ed25519 --message-hex MESSAGE_HEX
# MESSAGE_HEX is the exact message, not a digest.
```

avault v0.1.10 is published and verified. Its managed pin has a separate owner;
Avibe's standard integration proceeds against that release. Standard UI adds
the chain picker, generation/import, truthful address/approval/list/detail/search
surfaces and EN/ZH strings. Switching chains clears stale import material.
Protected controls remain unavailable until their compatible sandbox pin and
acceptance are delivered; no silent standard fallback. Standard work proceeds
in its own lane, not behind protected implementation.

## 2. Protected contract

**Decode only plain transfers; otherwise show raw and allow approval.** The
sandbox independently binds and signs the exact content. On Solana, decode
native System Transfer and SPL Transfer/TransferChecked to the addresses and
raw integer quantities actually present. TransferChecked adds its mint and
encoded decimals; its destination is a **token account**, not a proven wallet
owner. Plain Transfer has no mint/decimals: do not invent them. Keep existing
EVM/typed-data decoders and add a raw path for unrecognized calldata.

Everything else, including swaps, unknown programs, Token-2022 extensions,
compute-budget/memo instructions and unresolved lookups, remains raw and
approvable. **No token names/symbols/list, swap-specific display, outside RPC,
attestor, lookup resolution, simulation or network token metadata.** Never
promote caller labels or resolved addresses into verified facts. Decoding does
not prove deployed behavior, chain state, execution success or receipt of funds.

One notice whenever any content is undecoded/unresolved, in the selected locale:

- **EN:** Some content is not decoded; approving it is blind signing.
- **ZH:** 部分内容未解析，批准即为盲签。

No extra mode, toggle, second confirmation or second passkey ceremony. Removed:
forced create-then-transfer, receiving-token-account ownership proof, fee/memo
policy, one-signer/fee-payer restriction and blanket unknown/Token-2022/ALT/DeFi
refusals. Unknown semantics are not malformed serialization.

### Structural and custody invariants

- Canonical bounded parsing, full input consumption, valid counts/header/indices,
  no silent field dropping, and exactly the original bytes signed. Invalid
  encoding and unsupported outer formats fail before approval; valid outer
  messages with undecodable instruction data use raw fallback.
- Solana: **legacy and v0**, including ALT-bearing messages. Enforce the
  1,232-byte transaction bound including required signature slots. Validate
  compact-u16, lengths, indices and account ordering without resolving tables.
  The Vault key must be a required static signer; other signers and a different
  fee payer are allowed. Return only this key's signature. Fields that need
  unavailable lookup addresses stay unresolved/raw, not fabricated or refused.
- v1/unknown versions: **Unsupported transaction version / 不支持的交易版本**.
  Current Jupiter APIs default to v0; Kit/web3.js require explicit version
  choice, and Phantom supports caller-selected v1 but its in-wallet default is
  undocumented. Legacy+v0 is the first-slice scope, not a claim v1 doesn't exist.
- EVM: keep supported legacy/EIP-1559 envelopes, existing EIP-712 support and
  independent hash/chain checks. Unsupported envelope fields/types still fail;
  this does not add `personal_sign`, access/blob/authorization lists or new ABIs.
- Protected Ed25519 generation, sealing and signing stay inside the existing
  cross-origin sandbox; protected import is out. Only new Ed25519 records add
  curve metadata. Existing protected secp256k1 AAD remains byte-identical.
- Kind/scheme/record curve/public key/signer membership agree. No Ed25519
  through generic agent-operation contexts, digest aliases or raw-context bypass.
- Authenticate the whole immutable operation: name/key, scheme, exact payload,
  complete signing context, request identity, expiry and surrounding details.
  Preserve replay protection, fresh R3 passkey and sign-time revalidation after
  asynchronous approval. A message hash alone is not this binding. Request
  expiry is not a claim about blockhash freshness or durable-nonce validity.

### Exact-message wire

```json
{"kind":"solana-transaction-v1","messageHex":"<exact unsigned message hex>"}
```

The context has exactly those fields. `v1` versions the Avibe contract, **not**
the Solana message. Protected `messageHex` is nonempty, bounded, lowercase and
even-length. CLI/API uses `message_hex`; sandbox RPC and daemon-signed release
use `messageHex`. The authenticated release is
`{name, scheme:"ed25519", messageHex, publicKey, signingContext}`, with a 32-byte
lowercase-hex public key and no digest/digestAlgorithm placeholder. Outer message
and context bytes must match. Existing secp release bytes stay unchanged; the
passkey challenge still binds the complete signed-operation context. The sandbox
parses, displays and signs the same message without reserialization.

## 3. Approval: short summary, one Details section

Summary: transaction kind, signing key, decoded destination/raw amount where
available, and the notice whenever anything remains raw. Everything else lives
in **Details / 详情**, inside the same trusted popup, complete and copyable.
The notice is never hidden there. The parent card cannot substitute for this
trusted view. All labels have EN/ZH strings; addresses, hex and program names
stay unchanged. No first-version ALT UI beyond the original raw bytes.

Common summary labels: **Signing key / 签名密钥**, **To / 收款地址**,
**Amount / 数量**, **Mint / Mint 地址**, **Token account / 代币账户**,
**Contract / 合约**, **Value (wei) / 转账金额（wei）**.
Details retain the authenticated operation's existing surrounding fields, full
key/public key, version, and **Exact message (hex) / 完整消息（十六进制）** for
Solana; for EVM, the accepted envelope, all existing decoded fields, **Selector /
函数选择器**, **Calldata (hex) / 调用数据（十六进制）**, **Exact unsigned transaction
(hex) / 完整未签名交易（十六进制）**, and **Signing digest / 签名摘要**. The original
raw content remains available even when transfer fields are decoded.

The following are line-by-line content examples; `<…>` means a complete value,
not production truncation. The notice is rendered once in the chosen locale.

| Scenario | Summary lines, in order | Details additions |
| --- | --- | --- |
| SOL transfer | `Solana transfer / Solana 转账` → `Signing key / 签名密钥: <key>` → `To / 收款地址: <destination>` → `Amount (lamports) / 数量（lamports）: 1000000000` | `From / 转出地址: <source>`; complete message. No notice if all content is decoded. |
| USDC TransferChecked (scenario name only) | `Token transfer / 代币转账: TransferChecked` → signing key → `Mint / Mint 地址: <encoded mint>` → `Destination token account / 收款代币账户: <address>` → `Amount (base units) / 数量（基础单位）: 1000000` | `Source token account / 转出代币账户: <address>`; `Authority / 授权地址: <address>`; `Encoded decimals / 编码精度: 6`; full message. No “USDC” label or wallet-owner claim. |
| Jupiter swap with ALT | `Solana transaction / Solana 交易` → signing key → `Not decoded / 未解析` → blind-signing notice | Full original message, including all instruction and ALT bytes. No Jupiter/route/output interpretation or resolved address. |
| ETH transfer | `EVM transfer / EVM 转账` → signing key → `To / 收款地址: <address>` → `Value (wei) / 转账金额（wei）: 1000000000000000000` | Chain ID 1, type, nonce, all gas/fee fields, empty calldata, full unsigned payload and digest. No notice for this empty-calldata example. |
| ERC-20 transfer | `Decoded calldata / 已解析调用数据: transfer(address,uint256)` → signing key → `Contract / 合约: <address>` → `Recipient argument / 收款地址参数: <address>` → `Amount argument / 数量参数: 1000000` | Existing envelope, selector `0xa9059cbb`, full calldata/payload/digest. No symbol, decimals lookup or verified-execution claim. |
| Uniswap swap | `EVM contract call / EVM 合约调用` → signing key → `Contract / 合约: <transaction destination>` → `Value (wei) / 转账金额（wei）: <integer>` → blind-signing notice | Complete envelope, selector, raw calldata, unsigned payload and digest; no swap-specific presentation. |

For SPL Transfer, omit mint/decimals because they aren't present. If required
addresses are lookup-backed, show only facts that are actually known and mark
remaining content raw/unresolved with the notice. A known program's unsupported
instruction shape is raw, not silently reduced to a recognized prefix. EVM
noncanonical ABI arguments/trailing data likewise remain visible raw rather
than being dropped by a decoder.

## 4. Delivery and estimate

All lanes are active. Independent PRs start from current default branches;
merge shared contracts before starting consuming PRs, never stack them. Any
foundation route stays unavailable until a real structural verifier, trusted
view, whole-operation binding and compatible release pin are all wired in.

| Lane / independent PR | Model | Dependency / follow-on |
| --- | --- | --- |
| Sandbox Ed25519 custody, exact-message primitive, wire/binding foundation | GPT-6.1 Sol, xhigh | Current sandbox; production Solana gated until integration |
| Avibe standard generation/import/sign/verify + shared CLI/message/binding | Claude Fable 5.2, high | Published avault v0.1.10; separate managed-pin writer |
| Solana canonical parser + minimal transfer facts | Claude Fable 5.2, xhigh | Independent parser contract, no custody/UI imports |
| EVM unknown-calldata raw path | Claude Opus 5.5, high | Existing verifier and existing ABI only |
| Trusted bilingual summary/Details + frames | Claude Fable 5.2, high | Frames reviewed first; integrate verified facts after contracts land |
| Independent fixture corpus and acceptance | GPT-6.1 Sol, xhigh | SDK/primary-source fixtures now; real consumer tests after integration |
| Standard Avibe UI | Claude Fable 5.2, high | Standard backend on default; not blocked by protected work |

Then deliver independently based integration PRs: assembled sandbox verifier/
signer/view and immutable release, followed by the exact Avibe sandbox pin and
protected request/completion/UI path. Do not enable one without its dependencies.

**Planning estimate:** 9–13 engineer-days across parallel lanes; **3–4 calendar
days from November 11 to RC-ready**, targeting November 14 with November 15 as
review/acceptance buffer. November 11: first foundation/parser/raw/frame heads
and vectors; November 12: approved contracts and integration; November 13:
release/pin and isolated acceptance; November 14: fixes and full RC gates.
This is a working estimate, not a guarantee or permission to skip a check.
The critical path is sandbox contracts → integrated immutable release →
owner-approved deployment/pin → end-to-end acceptance. Refresh it from actual
review results; standard progresses in parallel.

### Acceptance and owner checkpoints

- Use independent public/unfunded fixtures with exact serialized messages,
  known signatures and provenance, not only self-generated roundtrips. Cover
  native/SPL transfers, ALT raw fallback, multi-signer/different-fee-payer,
  unknown programs and EVM calldata; canonical bounds, unsupported versions,
  all accepted fields and complete raw copy; EN/ZH and notice placement.
- Verify actual consumer boundaries: unchanged secp AAD; message/key/curve/
  context mutation; fresh passkey, replay/expiry and sign-time revalidation;
  returned signature verification and no downgrade/external lookup. Preserve
  standard import/blind-box/CLI/headless/always-ask regression coverage.
- Hermetic tests and local Incus for user-facing acceptance; no funded keys,
  live user Vault operations, broadcasts or remote regression. Keep real
  passkey/manual gaps explicit; mocks are not proof of a real ceremony.
- Owner sees frames first, then **each sandbox/signing PR's exact Merge-ready
  head** via the product owner. Each sandbox main merge auto-deploys Production:
  approval must include that consequence. Local append-only snapshot staging is
  not permission to deploy; retained published bytes are never overwritten.
- Owner separately approves the immutable sandbox release and exact Avibe pin.
  List every commit added after an approved head, including tests; distinguish
  mechanical rebases from content changes. RC readiness alone does not authorize
  a stable tag, rollout, install or restart.

No open product-scope choice remains: transfers at the minimal depth above,
no token names, raw fallback for everything else, legacy/v0, and 3.2.6 are fixed.
Owner checkpoints are frame and exact-head security/release approvals.

Later work may add deeper instruction/contract decoding, token identity labels
or v1 format support. None is a prerequisite for this first protected release.

## 5. Primary references

- Solana [versioned transactions](https://solana.com/docs/core/transactions/versioned-transactions),
  [transaction structure](https://solana.com/docs/core/transactions/transaction-structure),
  [token transfers](https://solana.com/docs/tokens/basics/transfer-tokens).
- Jupiter [transaction versions](https://developers.jup.ag/docs/swap/advanced/transaction-versions)
  and Phantom [v1 signing recipe](https://docs.phantom.com/recipes/transactions/send-solana-v1-transaction)
  (format/default documentation checked 2026-11-11).
- `vault-sandbox/src/signingContext.ts`, `src/signRequest.ts`,
  `src/operationContext.ts`, `src/vaultCrypto.ts`, `docs/integrity.md`;
  `avault/docs/DESIGN.md` owns standard Ed25519/blind-box semantics.
