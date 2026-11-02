# Vault Taproot signing: change contract and acceptance

## Failure and intended behavior

The listed Taproot address uses Q = lift_x(P.x) + TapTweak(P.x)G, but the raw
BIP340 scheme signs under P. A 64-byte signature can therefore look successful
while Bitcoin rejects the spend. Preserve the existing key, envelope and receive
addresses; use the explicit `schnorr-secp256k1-bip341-keypath` scheme for BIP86
(empty script tree) instead. The caller still owns transaction construction,
sighash selection and broadcast authorization. No transaction is broadcast by
these tests.

## Boundaries and invariants

- **avault** owns private-key normalization, tweaking, signing and zeroization.
  Avibe only routes ciphertext and verifies public signatures. The associated
  dependency is `avibe-bot/avault#35`; no managed version pin changes here.
- **Standard signing:** both headless and approved delivery verify the returned
  signature against the persisted public key (Q for key-path, P for raw BIP340).
  A bad result is not delivered or audited as success; claimed requests fail.
  Key-path signing requires a valid compressed pinned secp256k1 P **before**
  creating an approval or using the key. The same preflight applies to protected
  signing, so invalid metadata leaves no stuck pending request. Legacy
  unpinned records keep only old schemes, with strict encoding/range/low-S checks.
  Top-level and delivery parameters use allowlists: unsupported signing options,
  including explicit empty/null tree inputs, must not be silently dropped before
  the Rust RPC.
- **Protected signing:** the actual signer is `vault-sandbox`, not an Avibe UI
  crypto helper. `avibe-bot/vault-sandbox#25` binds the signed `release` to name,
  scheme, digest, compressed publicKey and the complete signingContext, checks
  it before approval and immediately before key use, and retains fresh UV,
  expiry and replay checks. The daemon builds it from persisted request/key
  metadata, never from a caller-supplied release object. Approval-card metadata
  is projected to the exact sandbox sign wire shape (`material: {name, envelope}`)
  at the shared UI hook; display-only `kind`/`field` never enter that RPC. The
  service owner rejects protected EVM/EIP-712 contexts with non-ECDSA schemes
  before creating an approval. Raw Schnorr with an agent-operation context stays
  available as a separate primitive.
- **Domain separation:** agent-operation preimages start with the literal ASCII
  `avault-agent-operation:v1` followed by one LF and non-whitespace operation
  text. UTF-8 bytes are SHA256-hashed by the sandbox before signing. The API
  validates structure/domain and matches the supplied digest to the request;
  the sandbox additionally recomputes the context digest. Literal backslash-n,
  CRLF, unprefixed input and extra top-level context fields are rejected.
- **Canonical JSON:** signing contexts admit only safe integers (absolute value
  at most 2^53-1), strings without lone surrogates, booleans, null, arrays and
  string-keyed objects. No floats, NaN or infinity. Python sorts object keys by
  UTF-16 code units to match JavaScript, including astral/BMP ordering.
- **Address UX:** raw BIP340 is never labeled as signing the listed Taproot
  address. Protected BTC addresses remain visible for existing-fund
  identification, with a warning in creation, list and approval surfaces. The
  approval warning does not imply that BTC addresses are displayed on that card.
- **Errors:** new signing validation failures carry backend catalog keys. The
  Web response renders the configured language; CLI diagnostics remain English.
  Only the specific new validation failures get these keys: existing lifecycle
  errors (denied, expired, mismatched requests) retain their actual reason rather
  than being relabeled as a context/scheme failure.

## Known by design and rollout

Protected Bitcoin transaction signing is **not supported**: there is no
verifiable Bitcoin transaction context in the protected workflow. New key-path
requests are rejected at the service owner/API before a request or passkey prompt
is created. Neither an arbitrary digest nor an agent-operation context is a
substitute. Raw signing remains an explicitly separate primitive, not a promise
that protected BTC deposits can be spent. Hardware passkey testing is separate
from the sandbox's simulated authorization-window/fresh-passkey RPC suite.

Install the new Avibe context **producer before** deploying the hardened shared
sandbox. The sandbox rejects legacy unbound requests; recreate them after the
upgrade. Do not weaken sandbox origin/integrity pins to test a branch. A released
supporting avault build is separately required for standard Taproot; an old build
fails closed and must never trigger raw-BIP340 fallback. No release, deployment,
real wallet operation, data migration or credential change is part of this PR.

## Regression ownership and evidence

| Boundary | Observable protection | Why old coverage missed it |
| --- | --- | --- |
| `tests/test_vault_signing_contract.py` | Official positive/negative BIP340 and empty-tree BIP341 signatures; pinned standard success/failure and request settlement; range/DER rejection; unsupported parameters; protected owner refusal | Length-only mocks did not verify P vs Q or exercise refusal before request creation |
| Same suite, sandbox fixtures | Real input validator → persisted request → signed release; full hydrated material shape; UTF-16 interoperability; domain/type/number validation | Helper-only fixtures bypassed input validation and copied a wrong literal `\\n` |
| Same suite, preflight and Web routes | Invalid compressed pins and incompatible scheme/context pairs leave no request or signing side effect across service/API/CLI; localized REST errors and English CLI errors | Post-signature checks failed closed too late; helper tests never exercised Web rendering |
| `ui/src/lib/useProtectedVault.signing.test.tsx` | Real hook → client → captured full `postMessage` payload, with optional actual sandbox parser acceptance | Card tests mocked the hook, so they missed UI `material.kind` being rejected by the hardened consumer |
| `tests/test_vault_addresses.py` | BIP341 plus three BIP86 receive/change vectors, both SEC1 parities, zero/order/infinity tweak boundaries | One even-Y vector cannot detect all parity/edge failures |
| CLI and approval-card tests | Parser-backed skill example, warning visibility, scheme-specific address labels | Primitive tests cannot verify what users/agents are told |
| Optional Core runner below | Actual `python -m vibe vault sign` → API → avault → Core acceptance for even/odd P, rawtr compatibility, DER SegWit, tamper rejection | Independent node acceptance detects unusable but well-formed signatures |

The Rust and sandbox PRs own their private-key primitive and real sandbox RPC
regressions respectively. Canonical JSON expected bytes and their UTF-8 hash are
regenerated with the actual JavaScript consumer, not the Python producer under
test; the fixture README records the independent reproduction command. No test
claims a JS primitive is a deployed protected Bitcoin workflow.

## Optional actual sandbox wire acceptance

The hook/client test always asserts the full wire contract. To additionally pass
that **captured production payload** to the companion sandbox's actual strict
`parseSignRequest`, bundle that checkout's source and select it explicitly:

```bash
# In the companion sandbox worktree, with its own dependencies installed:
./node_modules/.bin/esbuild src/signRequest.ts --bundle --platform=node \
  --format=esm --outfile=/absolute/path/to/scratch/sandbox-sign-parser.mjs

# In this worktree's ui/ directory:
AVIBE_TEST_SANDBOX_SIGN_PARSER=/absolute/path/to/scratch/sandbox-sign-parser.mjs \
  npx vitest run src/lib/useProtectedVault.signing.test.tsx
```

The test uses the real UI hook and client, simulates only transport setup and
responses, and checks that the actual consumer accepts the emitted payload and
rejects the previous `material.kind` shape. This is parser/serialization
acceptance, **not** hardware passkey or private-key signing acceptance. No
sandbox pins, live services, wallets or credentials are changed.

## Optional offline Bitcoin Core acceptance

Prerequisites: independently SHA256-verified Bitcoin Core binaries with wallet
support, an explicitly selected avault binary with the new scheme, and this
worktree's Python dependencies. The runner downloads nothing. Use the worktree's
own Python environment, not a shared environment or the installed service.

```bash
.venv/bin/python scripts/vault_bitcoin_regtest.py \
  --bitcoin-dir /absolute/path/to/bitcoin/bin \
  --avault /absolute/path/to/test/avault \
  --output /absolute/path/to/scratch/regtest-avibe.json
```

It creates fresh HOME/AVIBE_HOME/AVAULT_HOME/XDG roots, forces the avault file
store, strips inherited credential/runtime environment, and starts its own
loopback-only regtest node with peer networking disabled. Only publicly known
synthetic scalars 1 and 6 are sealed; it never opens Keychain or a real wallet.
The node derives independent descriptor addresses, matures synthetic coinbases,
and runs `testmempoolaccept` (not `sendrawtransaction`). Child teardown uses only
the recorded PID. The temporary state is removed even on failure.

Expected: key-path signatures accepted for `tr(P)`, raw signatures accepted only
for `rawtr(P)`, DER accepted for `wpkh(P)`, crossed schemes and tampering rejected,
and listed address witness programs equal Core's independent derivation. The
JSON report records each result and Core's version. The pre-fix mismatch can be
reproduced against a released old Rust binary with `--avault-only --baseline`;
that mode does **not** claim to test the new Avibe CLI. The full path was checked
locally with Bitcoin Core 31.1; the external binary prerequisite means CI's normal
unit suite does not run it automatically.
