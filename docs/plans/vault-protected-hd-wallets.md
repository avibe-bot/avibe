# Protected HD wallets — v1 contract

Status: CONTRACT revision 0.7, 2026-11-14. Target: 3.2.7.
Revision 0.4 added all THREE BTC public-key precommitments using the common
versioned flat table and existing address-index paths, keeping protected
signing/display Taproot-only. Revision 0.5 changed DOCUMENT ONLY: exact validated
stored projection, separate unchanged-standard/protected release unions, and
account-label versus secret-record-name rules. Revision 0.6 only removes private
coordination identifiers and replaces a scratch fixture locator with its committed
repository path. Wire, storage, custody and acceptance requirements are unchanged;
both public fixture bytes stay unchanged. Revision 0.7 adds the value-free exact-name
reconciliation GET and identical transactional create snapshot below. Its access
floor reuses protected-HD create authority plus the saved record's resource ACL;
it is not list/view authority. The rev0.6 fingerprint algorithm and both vector
files remain byte-for-byte unchanged.

Freeze requires the exact document and BOTH public vectors to be acknowledged by
the sandbox producer, backend consumer and an independent reviewer. An older
revision or vector-only ACK does not acknowledge changed document bytes. No
non-Taproot protected display/signing grammar is authorised by this contract.

## 1. Scope and outcomes

One row remains `kind: keypair`, `signer_kind: hd-seed`; `protection: protected`
selects browser custody, never avault HD custody. Create and import are both v1.
There are 20 shared indices (0..19), initially only #0 enabled. Existing standard
HD, static secrets, ordinary keypairs, their AAD and their calls remain unchanged.
No non-Taproot protected address display/spend, arbitrary path, seed export,
phrase re-view RPC, network lookup, simulation or expanded Bitcoin grammar is
introduced. Product's Nov13 14:01 decision requires the BIP44/BIP84 public points
precommitted now so future address-type enabling needs no reseal or migration.

The fixed profile is `bitcoin-address-types-v1`. Chain/type maps to these paths;
BTC uses hardened account0 and public receiving address-index i, preserving the
shipped BIP86 #1+ path, NOT m/purpose'/0'/i'/0/0. “Account #i” is the product's
shared index, not a promise that every reference wallet's hardened account matches.

| chain / address_type | public-key path for i | current protected scheme/context |
| --- | --- | --- |
| evm / absent | m/44'/60'/0'/0/i | ecdsa-secp256k1-recoverable; evm-transaction or eip-712-typed-data |
| bitcoin / p2pkh | m/44'/0'/0'/0/i | PUBLIC PRECOMMIT ONLY; no current display/signing |
| bitcoin / p2wpkh | m/84'/0'/0'/0/i | PUBLIC PRECOMMIT ONLY; no current display/signing |
| bitcoin / p2tr | m/86'/0'/0'/0/i | schnorr-secp256k1-bip341-keypath; bitcoin-taproot-keypath-v1 |
| solana / absent | m/44'/501'/i'/0' | ed25519; solana-transaction-v1 |

Tokens are exactly `p2pkh|p2wpkh|p2tr`, legal only for Bitcoin. Public projection
field names stay `btc_legacy|btc_segwit|btc_taproot`. On protected signing only,
omitted API/CLI address_type normalizes ONCE at protected request intake to p2tr,
before persistence/signing-context construction; the signed release and protected
completion always carry explicit p2tr. There is no completion-time normalization.
Other types refuse as unsupported even though their public keys are precommitted.
Do not change old
standard omission/binding bytes. Future type enabling requires its own complete
signing-context/verification/approval contract, not merely relaxing this gate.

Chain, account and scheme are validated before key use. No raw/unbound protected
fallback and no disguise as an agent-operation context. Existing recognised
transactions are decoded; other valid contents stay raw with the existing notice.
Existing context parsers, digest/message semantics and Bitcoin limits are reused.
Solana signs original message bytes, never their digest. BTC verifies against the
BIP86 tweaked output key/script, not merely the internal key.

## 2. Secret payload: entropy AND seed, never a stored passphrase

Product decision, Nov13 12:57: preserve future phrase re-view without providing it
in v1. In one AEAD plaintext store the BIP39 entropy and the 64-byte seed. Signing
uses only the seed. No word list, entropy, words, passphrase, seed or derived secret
crosses into the parent page, postMessage/RPC, Python, logs, Show Pages or avault.
Only public/synthetic fixtures are used in agent tests.

Proposed binary codec (all lengths exact; reject trailing/truncated data):

```
UTF8("avibe:protected-hd-wallet:v1\n")
|| uint8(entropy_length)
|| entropy[entropy_length]
|| seed[64]
|| uint8(passphrase_required)
```

`entropy_length` is one of 16,20,24,28,32. The fixed v1 wordlist is BIP39 English.
`passphrase_required` is 0 or 1, indicating a nonempty NFKD-normalised passphrase;
it is not the passphrase and is encrypted. This one-bit derivation hint preserves
honest future recovery wording, not passphrase recovery, a second factor or a
new v1 viewing feature. Create generates 128-bit entropy
(12 words); import accepts the standard flow's valid 12/15/18/21/24-word phrases.
Words use the standard normalisation/checksum rules. Passphrase uses BIP39 NFKD,
never trimming/case-folding; whitespace is significant. Create confirms the
passphrase twice and runs the same three-word backup quiz as the standard flow.

The marker is exactly 29 bytes. Plaintext lengths are 111/115/119/123/127 bytes
for those entropy lengths; AES-GCM ciphertext lengths including the tag are
127/131/135/139/143 bytes. The nonce and wrapped-DEK nonce are each 12 bytes;
wrapped DEK is 48 bytes.

The final words + final passphrase produce both payload seed and all shown/public
keys. Before return, reopen the new ciphertext in the sandbox and compare BOTH
reopened entropy and seed to the final confirmed entropy and final-passphrase-
derived seed, plus the public keys. A right seed with wrong entropy must fail.
Do not retain a seed derived before the last passphrase change. Later signing
cannot prove the entropy/seed relation without the never-stored passphrase;
creation/import consistency is tested independently from actual displayed words.
After creation, passphrase/word UI state is discarded and mutable byte buffers are
wiped on success, cancel and error. JS immutable strings/GC and dependency internal
copies are an honest residual, not guaranteed heap erasure.

Keeping entropy in this ciphertext adds no new decryption/export path. Estimate
only in hours: codec work is a small reusable slice, not an estimate for the full
trusted UI/window/signing feature; calibrate from actual first-slice implementation.
Passphrase is
a wallet derivation input, not a second encryption factor protecting a decrypted
seed. A later phrase-view feature needs its own approval/design and must remind
the user it cannot reveal a passphrase that was never stored.

## 3. Authenticated public identity, not mutable UI settings

Proposed new internal `wrap_meta.record_meta` discriminator:

```
{
  kind: "hd-seed",
  profile: "bitcoin-address-types-v1",
  accounts: [{ account: 0, evm: "<33-byte compressed hex>",
               bitcoin: "<33-byte BIP86 internal compressed hex>",
               solana: "<32-byte hex>",
               bitcoin_p2pkh: "<33-byte BIP44 compressed hex>",
               bitcoin_p2wpkh: "<33-byte BIP84 compressed hex>" }, ...19]
}
```

Exact keys only. Ordered 0..19, canonical lowercase hex; secp keys are valid
compressed curve points, and Solana keys use the shipped strict 32-byte encoding/
chain validation, not an invented subgroup-validation claim. Actual sandbox
key derivation/equality and completion signature verification still apply.
No duplicate/missing indices, custom paths, xpub, chain codes or enabled flags;
no extra cross-row public-key-uniqueness rule. Bitcoin public keys here are the
compressed BIP86 INTERNAL derived child points, not already tweaked output keys.
The flat field-to-type registry is exact: bitcoin→p2tr, bitcoin_p2pkh→p2pkh,
bitcoin_p2wpkh→p2wpkh; each has its distinct path above. Profile versions this
complete immutable table without a parallel nested-table schema. Unknown profile,
missing key or an extra row field refuses. Precommitting all100 public keys
(20×[EVM+threeBTC+SOL]) makes later type enablement metadata-only with respect
to this envelope; adding absent keys or changing paths/profile/AAD would NOT be
metadata-only and would require a separately designed custody migration.

`wrap_meta.v` stays integer 1 and its scheme stays `machine-aesgcm-v1`.
The current producer at sandbox `src/vaultCrypto.ts` (published base 6edd2e14 and
successor 49a24983) already uses typed v2 AAD, not the older concatenating helper
still present in the Avibe UI copy. Do not introduce a new scheme to fix a stale
source assumption. For the new typed HD branch AAD is UTF8(stableJson({
  domain:"avault:protected-record:aad:v2", name:NAME,
  scheme:"machine-aesgcm-v1", version:1, record:RECORD_META
})). The ENTIRE record metadata above is authenticated by both payload encryption
and DEK wrapping. New HD names use the secret-name validator (no dotted field
names), even though the internal kind is not `keypair`. The OUTER row, display
and material kind remain `keypair`; never insert internal `hd-seed` into the
existing signed display-kind union. Add a new typed branch rather than interpreting
the seed as a 32-byte signing key. Preserve every legacy AAD byte, including an
absent secp curve rather than adding a default, and every old root/RP/credential.
Old sandbox must refuse the new kind.

For this new branch `wrap_meta` is a raw string only, at most 65,536 UTF-8 bytes;
never accept an object and silently reserialize it. Preserve its exact string
for fingerprinting. Encrypted fields are canonical standard base64 (strict decode
and re-encode equality) at the exact sizes above; ciphertext length must be in
the five permitted lengths before opening, then match the decoded payload length.
Parsed JSON must be a bounded object with scalar-valid, finite values; malformed,
non-object, over-depth (>64 containers) and oversize forms refuse with a controlled
error. JSON duplicate members use the same ordinary last-member-wins semantics
in JS/Python; validation/AAD use that effective parsed object, fingerprinting the
ORIGINAL text. Preserve existing permitted base/root/copy fields and unknown
root extensions; exact key sets apply to the NEW HD record/table, not a new
legacy-breaking whitelist over the root. Public vectors pin these choices.

Backend copies only this validated immutable public key table into its HD public
metadata, creates #0 enabled, and preserves existing account field/CLI names.
Enabled state, labels, policy/audit fields are separate mutable server metadata,
not part of ciphertext AAD: account enable/rename must not re-encrypt the wallet.
Protected accounts always require per-signature approval; no false toggle/headless
escape. Add account enables the next index through the existing management ACL,
without seed/Passkey/custody activity. This does not claim hostile same-UID policy
isolation; the sandbox independently shows the actual selected account/path/key.

EVERY non-signing operation must permit only its intended kinds: generic reveal,
export/copy, `approveRelease`, DEK/agent delivery allow only explicit static or
the existing legacy-static case. Reject HD and unknown kinds BEFORE plaintext/
DEK use. A guard that rejects only `kind === keypair` is insufficient once the
HD parser exists. Ordinary-key sign helpers likewise reject HD. Add genuine HD
reveal/export/DEK refusal tests at the owning operation boundaries, not merely
old-parser failures. No phrase re-view/export RPC exists in v1.

## 4. Trusted create/import and atomic persistence

The additive operation/capability is exactly `sealHdWallet`; old `seal` is
unchanged. Client call: `sealHdWallet({name, mode, wrapMeta}, authorizationWindow)`.
Mode is exactly `create` or `import`. Payload has EXACTLY those three keys.
`wrapMeta` is a nonempty raw existing BASE/ROOT wrap string (with valid existing
passkey PRF copies), not a record envelope: `record_meta`, `dek_nonce` and
`wrapped_dek` are forbidden there. Preserve all existing allowed root/RP/copy
fields and extensions; do not replace the root or create/register another factor.

The Web form warms/verifies the pinned client/capability before its launch click,
then synchronously calls existing `openVaultAuthorizationWindow()` before any
await. A non-null handle is required for this NEW operation. Pass only its ID as
RPC ENVELOPE metadata `authorizationWindow`, never in payload. Missing ID is an
`invalid_payload` refusal. A blocked/null reservation is a retryable
`authorization_popup_blocked` failure: no parent secret form, old `seal`, tier
fallback or silent new seed. Parent closes the handle in `finally`.
Use the existing verified `about:blank` -> pinned same-release authorize route;
existing origin + exact build + ID + source-window gates surround the messages.
No secret or wrap string is put in a URL, browser persistence (local/session
storage or IndexedDB), BroadcastChannel or log.

The iframe holds a cancellable operation snapshot of the EXACT reusable root
identity, lifecycle epoch/operation generation and `currentFreshSetup()` value.
Root identity uses the existing full root projection (remove only scheme/record/
DEK fields, preserve unknown root extensions), not merely scope/user handle.
This is local state, NOT a parent-supplied boolean or new public flag/RPC.
Root changes, lock/auto-lock, reset, unload or superseding the operation invalidate
it; extend the existing lifecycle abort/epoch machinery to cover an initially
locked operation too. Comparing the fresh-setup boolean alone is insufficient
(false->false lock/root changes exist). Recheck after every awaited boundary
and immediately before emitting a successful result. Parent likewise verifies
its matching setup/root/registration snapshot before starting REST creation;
if an observed state change invalidated it, discard the result. Once REST has
started, use the persistence/response-loss rules below, not compensating DELETE.

The iframe creates one local `deadlineAt = Date.now() + 300_000` before pairing.
The 30-second pairing budget is INCLUDED in that 300-second operation deadline.
The NEW exact authorization-channel request is:

```ts
{ kind:"protected-hd-wallet", name, mode:"create"|"import", wrapMeta,
  deadlineAt: /* finite safe integer, epoch milliseconds */ number }
```

These FIVE fields are public context only. `deadlineAt` is generated by the
iframe, never supplied in the outer RPC payload. Popup requires a remaining
budget >0 and <=300_000 ms, and owns its abort controller/timer. No fresh-root
flag, registration, words, entropy, passphrase, seed, PRF output, VMK, DEK, private
key or public table is sent to it. Reject unknown fields/kinds.

The popup does the ENTIRE sensitive flow itself; it does not share the iframe's
VMK session or receive/copy a VMK. It validates the base root and available PRF
copy entries, owns generate/import/backup/quiz/final passphrase and #0 review,
then immediately before sealing uses existing `assertPasskeyPrf` + `unwrapVmk`
locally with the existing credential/RP. This independent assertion is required
even if the iframe is already unlocked; do not pre-unlock a locked iframe for
this create/import operation. Minimise VMK lifetime: unwrap -> seal -> reopen and
compare final entropy, seed and ALL 20x5 public keys/expected #0 -> cleanup.
No `commitUnlockedVmk`, popup session, credential registration, broadcast unlock
or broadcast lock is needed. Fresh setup's registration and this PRF assertion
are distinct ceremonies; frames must say so honestly.

After cleanup, the NEW exact channel success result is:

```ts
{ kind:"sealed-hd-wallet", envelope:{ciphertext,nonce,wrap_meta},
  accounts:[<pure canonical rows 0..19>],
  expectedAccount0:{eth,btc_legacy,btc_segwit,btc_taproot,solana} }
```

The iframe accepts only this kind/shape, matching name/base root in the returned
envelope, matching typed account table and #0 public addresses. It never accepts
`confirmed`/`unlock`/`assertion` as a creation result. It checks deadline and local
lifecycle snapshot again, then appends its SNAPSHOTTED `establishingVmk` value to
the outer result below. No secrets cross even the popup->iframe channel.

Cancel/Passkey cancellation -> retryable `authorization_cancelled`; popup close
or 30-second pairing failure -> existing retryable `authorization_window_closed`
(with the current close-message grace); deadline -> retryable
`authorization_window_timeout`. Existing `sandbox_reset` / operation-aborted
transport may describe a reset/supersede, but must never become success.
Popup pagehide, requesting-page unload, lock/root race, cancel and deadline abort
all async stages, discard mutable buffers and close listeners/window. The iframe
also closes the popup on its deadline, so an RPC-client timeout is not the only
cancellation mechanism. Use 305_000 ms for this outer RPC (old calls unchanged),
leaving a small terminal-reply grace; browser suspension can still delay errors,
so recheck absolute deadlines and refuse late success rather than promise timely
error delivery while suspended. No REST create call follows a failed operation.

Only the opaque completed envelope/public material may be kept in parent memory
while persistence is resolved. Backend/network failure does not bring secrets
back, replay setup or generate another seed; response loss follows the binding
and idempotency rules below. Cancel before submission discards that pending
operation without a backend write.

Reject parent-provided words, entropy, passphrase, seed/private key, public key
table or `confirmed` shortcuts. Sandbox owns the secret form, quiz, passphrase
confirmation and #0 address confirmation. The sandbox RPC result is exactly:

```
{ envelope: {ciphertext, nonce, wrap_meta},
  accounts: [<canonical public key table>],
  expectedAccount0: {eth, btc_legacy, btc_segwit, btc_taproot, solana},
  establishingVmk: boolean }
```

The raw RPC result has exactly these four keys; registration is NOT a popup or
raw RPC result. The parent hook preserves the existing lifecycle wrapper: attach optional
`authzFactorRegistration` from the SAME fresh setup session, map
`expectedAccount0` -> `expected_account0`,
`establishingVmk` -> `establishing_vmk` and registration ->
`authz_factor_registration` at the create API boundary. Setup alone on shipped
3.2.6 is not necessarily durable: first wallet/root/factor commit remains atomic.
After the separately landed shared durable setup contract from #3652, reconcile
current root/factor establishment and any already-consumed registration instead
of replaying it or making another root. An already-durable setup survives a
wallet failure. Do not add a hidden setup-ack RPC or reset/re-enroll a credential.
The exact hook result union must represent these existing public lifecycle extras.

Create uses the EXISTING create endpoint with:

```
{ name, kind:"keypair", signer_kind:"hd-seed", protection:"protected",
  sealed:{ciphertext, nonce, wrap_meta},
  accounts:[<pure canonical public key rows>],
  expected_account0:{eth, btc_legacy, btc_segwit, btc_taproot, solana},
  ...existing non-secret metadata/provision fields,
  ...applicable existing establishment fields above }
```

The top-level `accounts` table must equal the internal record_meta table by
typed projection; it is not another authority. For this NEW protected branch,
storage alone builds these REQUIRED versioned fields (not an alternate AAD):

```ts
public_meta.bitcoin_profile = "bitcoin-address-types-v1";
public_meta.hd_accounts = [ // exactly 20 rows, ordered account0..19
  { account:0, evm:"<compressed hex>", bitcoin:"<BIP86 internal compressed hex>",
    bitcoin_p2pkh:"<BIP44 compressed hex>", bitcoin_p2wpkh:"<BIP84 compressed hex>",
    solana:"<32-byte hex>", enabled:true, name:null, always_ask:true,
    last_signer:null }, // each row has EXACTLY these ten keys
  /* rows1..19: enabled:false, same mutable defaults, their validated public keys */
];
```

`bitcoin_profile` is the consumer-agreed internal projection name, aligned with
the separate standard3.2.6 BTC-type extension; do not add another `hd_profile`.
Copy it ONLY from validated `record_meta.profile` and copy
the six immutable row fields ONLY from validated `record_meta.accounts`. Both
cached profile and all immutable row projections must equal that authenticated
envelope metadata on create, protected-HD read/account-management, sign-request
intake and fresh completion reread. Missing/unknown profile, wrong/missing/extra
row fields, malformed typed values or any immutable mismatch refuses the item;
never auto-repair from caller input or fail the entire Vault startup/listing.
The envelope remains authority, not the cache. Read/create checks establish public
consistency, not ciphertext/seed authentication by the server.

The four other row fields are mutable server-owned settings/provenance, NOT AAD:
`enabled` is a true boolean using existing sequential account-enable rules;
`name` is null or the existing validated account-label string; `always_ask` starts
true for ALL20 and cannot enable a protected headless path even if corrupted;
`last_signer` uses the existing provenance type or null. Initially only #0 is
enabled; all names/last_signer are null. Label/settings changes cannot alter the
six immutable fields or cached profile and do not reseal the wallet. Protection,
not a mutable flag/cache, still enforces per-operation browser approval.

The new protected branch rejects caller `public_meta` ENTIRELY (standard
interfaces are unchanged), `account0_always_ask` even when true (standard-only knob),
client enabled/settings/signing_public_key,
`keypair` spec, `blind_box`, plaintext/seed/phrase/passphrase, fields/default_field,
row policy.always_ask, tier changes or metadata-based envelope replacements.
No new server feature flag or live-manifest-derived authority. The package pin
selects the cohort; an old/missing capability fails closed, never switches tier.

The create API accepts opaque protected HD material only, with existing ACL,
provision, Passkey setup/root-commit and same-name concurrency checks. It does NOT
call avault or decrypt material. It validates envelope structure, immutable key
metadata equality and public addresses. `expected_account0` must be supplied and
compared in the STORAGE SERVICE, not only the API wrapper, BEFORE INSERT/audit/
resource/provision fulfillment. Expected #0 has EXACTLY
eth/btc_legacy/btc_segwit/btc_taproot/solana: canonical EIP-55 ETH, mainnet BIP44
Base58Check P2PKH from bitcoin_p2pkh, BIP84 bc1q P2WPKH from bitcoin_p2wpkh,
BIP86 bc1p P2TR from bitcoin, and Solana base58 respectively. These are public
consistency outputs; UI must not display/offer non-Taproot protected types yet.
Compare all five exact strings against the validated stored-key projection,
never retry
without this check. Read back the inserted public projection inside
that same rollback boundary before commit; mismatch rolls back the whole new
wallet/root/factor establishment even if a caller catches the validation error.
Independently durable setup is existing state, not compensating-delete material.
No create-then-PATCH policy gap or browser-only quarantine substitute.
A normal response-loss retry may report existing state; it must not overwrite or
silently generate another seed or adopt a different same-name winner. UI verifies
saved record ID where known, tier/signer/profile, full immutable table, envelope
fingerprint and #0 against its completed operation before presenting ready-to-fund;
mismatch remains blocking, never silently retried without the check. Fault tests
cover stale response/name reuse and a committed response lost. DELETE as
compensation is not atomic rollback.

### Exact-name reconciliation (revision 0.7)

The protected-HD POST success is additive:

```ts
{ ok: true, secret: /* existing enabled-account public cards */,
  reconciliation: ProtectedHdReconciliation }
```

`GET /api/vault/secrets/{name}/hd-reconciliation` returns exactly
`{ok:true, reconciliation:ProtectedHdReconciliation}`. The path name must satisfy
`^[A-Za-z_][A-Za-z0-9_]*$` and is looked up exactly: no trim, case-fold, search,
body, expected-value input or alternate selector. The response is fresh JSON with
`Cache-Control: no-store`. The snapshot has exactly these nine fields:

```ts
type ProtectedHdReconciliation = {
  name: string;
  secret_id: string; // nonempty saved record ID
  kind: "keypair";
  protection: "protected";
  signer_kind: "hd-seed";
  bitcoin_profile: "bitcoin-address-types-v1";
  envelope_sha256: string; // lowercase 64-hex; unchanged rev0.6 algorithm, section 5
  accounts: Array<{
    account: number; // true integers 0..19, ordered, exactly 20 rows
    evm: string; // lowercase compressed SEC1, 66 hex
    bitcoin: string; // lowercase BIP86 INTERNAL compressed SEC1, 66 hex
    bitcoin_p2pkh: string; // lowercase BIP44 compressed SEC1, 66 hex
    bitcoin_p2wpkh: string; // lowercase BIP84 compressed SEC1, 66 hex
    solana: string; // lowercase Ed25519 public key, 64 hex
  }>;
  expected_account0: {
    eth: string; btc_legacy: string; btc_segwit: string;
    btc_taproot: string; solana: string;
  };
};
```

Return the complete six-field public table, INCLUDING disabled indices, rather
than inventing a table digest/canonicalizer. Public keys link derived accounts
not exposed in ordinary enabled-only cards. Therefore GET reuses the SAME
`require_secret_create_access` check as protected-HD create (currently Editor or
higher), THEN requires the saved record ID's existing resource-use ACL. This is
not a new permission, list/view parity, management-only authority, a Passkey
ceremony or permission to use custody. HTTP admission likewise uses create's
existing role floor. Preserve the existing resource-use policy, including its
established role/instance semantics; this endpoint does not change which saved
records that policy permits. Do not add a public-metadata exception to its
saved-ID check.

One storage-owned projection validates exact protected/keypair/hd-seed
classification, the envelope/profile and the whole cached table/settings before
returning any snapshot. All fields come from ONE authorized row, not separate
name-based rereads. `expected_account0` is derived from that validated saved
row0 using the five canonical address rules above, not echoed caller values.
`envelope_sha256` hashes the ORIGINAL persisted ciphertext/nonce/raw wrap strings
using section 5, never parsed/reserialized wrap metadata or the F4 envelope-object
hash. No mutable labels, settings, usage, timestamps or policy revision enter the
snapshot. The two extra BTC addresses remain consistency-only, not new protected
receiving/signing choices. No ciphertext, nonce, wrap metadata, seed, words,
passphrase, private key or root/factor material is returned.

POST builds the SAME projection from the validated inserted/read-back row INSIDE
the existing create rollback unit, and emits it only after commit. Projection
failure rolls back wallet/resource/audit/provision and any newly established
setup even if a storage caller catches the error; independently durable setup
survives. Do not fetch by name after commit to construct a POST snapshot. Ordinary
and standard-HD create responses and all existing lists stay unchanged. GET is a
pure read: no custody/version probe, decrypt/sign/reveal, grant, setup/factor/root
mutation or promotion, audit/use write, resource creation, repair or event.

Failures use the existing error envelope: invalid exact name -> 400
`invalid_name`; absent exact record -> 404 `secret_not_found`; create-floor or
saved-resource refusal -> 403 `resource_access_forbidden` at the API/storage
boundary (HTTP admission may return its existing `instance_access_forbidden`);
wrong classification, corrupt envelope/cache or unknown/missing profile -> 409
`invalid_hd_wallet`. Authentication retains the existing HTTP behavior. Return
no partial reconciliation on error. Missing old-server endpoint, timeout, 5xx,
404 or any other failure is UNCONFIRMED, never proof that nothing was saved.

Both normal POST and later GET feed the SAME consumer comparison of the completed
operation: exact name/classification/profile, every field of all 20 ordered rows,
locally computed rev0.6 envelope fingerprint and all five expected#0 strings.
If an ID was observed previously, require that same ID even if another record
copies the public/encrypted material. If the response was lost before any ID was
known, learn the ID only after a FULL match; never adopt by name/existence or
three displayed addresses. Malformed/partial, mismatching, stale-response or
same-name replacement results stay blocking/unconfirmed. Cosmetic account label
changes are not identity changes. No automatic create retry, new seed, overwrite,
compensating delete, setup replay, signing or reveal fallback follows. A match is
public consistency at the time of that read, not AEAD/seed/PRF attestation or a
guarantee against later authorized changes; fresh signing checks remain required.

Acceptance must use real API return fields, not injected keys or DB-only proof:
normal POST/GET snapshot equality; actual commit then lost response -> exact GET;
a different same-name winner and known-ID replacement; all 20 rows with only#0
enabled; all five saved-derived addresses; raw-wrap order/Unicode fingerprint;
missing/partial/malformed/error cases; create-floor and saved-ID ACL refusal; no
GET side effects; ordinary/standard/list parity. Existing duplicate/no-overwrite
storage tests do not establish consumer reconciliation.

The public metadata is a consistency boundary, not remote attestation that the
sandbox generated ciphertext. Only the sandbox can authenticate its AAD and
validate the decrypted seed/key relationship. Server structure checks must not
be described as that cryptographic proof.

## 5. HD signing release and completion

Reuse the standard chain/account selection and pinned signing snapshot, with
an explicit protected Bitcoin address_type as specified below; do not run
protected HD through avault or standard headless routing.
Protected takes precedence before any `hd-seed` fast path or avault-version gate.
Backend obtains selected enabled account/key from persisted state, never caller
metadata. Signing request persisted once; every re-read/completion checks that
same record/key/envelope/input/derivation still matches.

The sandbox `sign` request retains EXACTLY the ordinary four payload keys:

```ts
{ material:{name,envelope:{ciphertext,nonce,wrap_meta}},
  scheme, signingContext, context:<existing signed operation context> }
```

Its HD branch is selected by the typed record metadata. The sandbox gets the
selection ONLY from authenticated `context.release.derivation`, not another
outer caller selection or public table. An ordinary record rejects HD release
extras; an HD record rejects a missing/malformed HD binding. Parent card/hook
compares persisted request derivation to the signed release, and REST completion
repeats that persisted chain/account and explicit protected Bitcoin address_type.
Only protected REQUEST INTAKE may normalize an omitted Bitcoin type to p2tr;
persisted request, authenticated release, sandbox validation and completion never
normalize a missing type or select a replacement. Signed display kind stays
`keypair`.

The two tier-specific wire unions are distinct. This contract does NOT widen an
existing standard validator to the protected union:

```ts
// Existing STANDARD legacy omitted-type branch: byte-for-byte unchanged on ALL
// chains, including Bitcoin. Its release keeps the existing fields/bindings.
type StandardHdLegacyDerivation = { chain:"evm"|"bitcoin"|"solana", account:number };
// NEW PROTECTED branch only:
type ProtectedHdDerivation =
    { chain:"bitcoin", account:number, address_type:"p2tr" }
  | { chain:"evm"|"solana", account:number };
```

The separately authorized standard3.2.6 explicit-BTC-type extension is governed
by its OWN contract; it is not changed or enabled by this document. In particular,
never insert `address_type:"p2tr"` into an existing standard omitted-type request,
release, fingerprint, avault command or completion. Keep the current standard
`{chain,account}` binding tests and standard typed-extension tests as separate
regressions. Ordinary records likewise gain no HD fields.

The NEW PROTECTED-HD signed `context.release` exact field union is:

```
{ name, scheme, digest OR messageHex, publicKey, signingContext,
  derivation: {chain:"bitcoin", account, address_type:"p2tr"}
            | {chain:"evm"|"solana", account},
  signingKey: {secret_id, public_key, envelope_sha256} }
```

All fields in this protected-only release are exact. `publicKey` and
`signingKey.public_key` are the SAME canonical
selected HEX STRING, not public-key objects; digest XOR messageHex. Secret ID is
a nonempty existing record identifier, `envelope_sha256` is lower-case 64-digit
hex. Derivation is exactly chain/account for EVM/Solana, and exactly
chain/account/address_type:"p2tr" for Bitcoin; no type on other chains, no extra
path or profile supplied by callers, and account is a true integer0..19 (not a
Python bool). The table in section1 fixes the only permitted chain/type/scheme/context
pairs. Digest is lowercase 64-digit hex; Solana messageHex is nonempty lower-case
even-length exact message hex within existing message/packet bounds. Use the
EXISTING `hd_signing_binding` fingerprint unchanged for BOTH HD tiers:

```
SHA256(UTF8(Python json.dumps([ciphertext, nonce, wrap_meta],
                            separators=(",", ":"), ensure_ascii=True)))
```

`wrap_meta` stays its original string. Do not use the envelope-object helper or
operation-context canonical JSON and do not parse/reorder the wrap string.
Matching JS (code units, not a Unicode-mode regex):

```js
JSON.stringify(theStringTriple).replace(/[\u007f-\uffff]/g,
  c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
```

Non-BMP characters produce two lower-case
four-digit surrogate escapes. The separate operation-context signature/challenge
codec stays unchanged. The backend's 18 parity vectors are committed at
`tests/fixtures/vault_signing/protected-hd-fingerprint-vectors.json`
(SHA256 ec59b255b57229ebbe4485e28649e7ad42aa1237ef3af5d9b9214caf7e051949).
Codec reference fixtures (PROPOSED, public and NEVER FUND):

- `tests/fixtures/vault_signing/protected-hd-fingerprint-vectors.json`, exact
  backend 18-vector bytes, SHA256
  `ec59b255b57229ebbe4485e28649e7ad42aa1237ef3af5d9b9214caf7e051949`.
- `tests/fixtures/vault_signing/protected-hd-codec-vectors.json`: all five entropy
  lengths, final words/passphrase/seed, all 20x5 immutable keys, full AAD text/hex,
  deterministic payload and wrapped-DEK AEAD, raw-wrap order/Unicode distinctions,
  current v2 legacy AAD bytes, and negative codec/identity mutations. Empty VMK
  copies in these crypto-only fixtures are NOT valid setup/restore envelopes.
  Generator and provenance: `tests/fixtures/vault_signing/protected-hd-generator/`.
  SHA256 `acc6b89ac03c9585618f373f9e26e018fd93513227f117195e3869ca9dfb5ffe`.
  The unchanged revision0.4 fixture adds two BIP44/84 public-key precommitments per index, five exact
  expected#0 addresses, two official BIP84 public receiving vectors, and both-AEAD
  tamper refusals for the extra keys. Old66e vector-only ACK does not carry over.

The exact document and BOTH fixture hashes must be in every freeze ACK. These
reference probes are not product implementation, WebAuthn or funds acceptance.

They cover controls/DEL/Unicode/escaping, original wrap order/format and one-byte
changes. Codec-only invalid-envelope/lone-surrogate examples do NOT authorize
those values in real envelope or signed-context input.

Sandbox parses the typed HD record and whole signed operation, verifies its
fingerprint and chain/context pairing, derives the selected key from opened seed,
and checks it against the immutable AAD table and signed public key. Before fresh
UV, the trusted review shows actual account index, fixed path and signing address
from that table, not parent display text. HD ceremony phases are explicit:
locked -> PRF unlock -> authenticate root/context/AAD and build actual account/
path review -> OPERATION-BOUND fresh UV -> recheck frozen operation/session ->
sign and consume. Already unlocked starts at authenticated review, never skips
UV. Do NOT reuse the generic locked-R3 random-unlock then alreadyConfirmed/none
branch as proof of this HD requirement. Cancellation/expiry/reset/lock-race at
either ceremony refuses the operation; a retry restarts validation and may never
fall through to an unlock-only signature. This can mean two Passkey ceremonies
when initially locked (unlock plus sign approval), reflected honestly in frames.
The fresh R3 challenge binds the WHOLE signed operation including HD selection
and fingerprint. No grant, boolean confirmation, unlock or public signature stands
in for that sandbox ceremony.

The sandbox result is EXACTLY the existing `{scheme, signature, recovery_id}`:
64-byte lowercase signature hex for these three schemes; recoverable ECDSA has
an integer recovery_id 0..3, the other two have null. Selected derivation remains
in the persisted/API result, not a new sandbox-result key. Backend requires
valid r/s, low-S and recovery-id recovery to the selected persisted secp point;
Ed25519 verifies the original exact message and BTC Schnorr verifies the BIP86
TWEAKED output of the persisted internal point. Then it uses
the existing expiry/one-use/CAS completion. Protected HD stays PENDING during
browser work; one fresh short writer transaction reserves then rereads request
and record, requires unexpired pending state, and rechecks record ID/tier/profile,
enabled account/key/AAD table/envelope fingerprint and exact scheme/input/context/
signed snapshot. Verify signature and CAS pending->approved once; persist outcome,
audit/usage/last_signer together and dispatch the existing continuation only after
successful commit. No writer lock spans browser interaction. Standard HD keeps
its pending->signing->approved avault claim path; do not conflate them.
The browser repeats persisted chain/account, explicit protected Bitcoin
`address_type:"p2tr"` and signing context at completion. Omitted protected
selection/type, a changed/unsupported type, type on EVM/Solana, or any difference
from the persisted protected derivation REFUSES; do not normalize or backfill at
completion. A caller browser_signed boolean is not authorization.

Names have TWO distinct roles. The mutable ACCOUNT LABEL is
`public_meta.hd_accounts[i].name`; changing only that label while a request is
pending does NOT invalidate signing identity or force resealing. Keep any label
already in the authenticated operation as its frozen display snapshot; do not
rewrite signed context to refresh it. Fresh completion still rereads/revalidates
all security fields and must not reject solely because this cosmetic label (or
a revision changed solely by that label) differs. By contrast, the SECRET RECORD
NAME is `context.release.name` / `material.name` and the envelope AAD's name; it
must still identify the original record ID and remain unchanged. Changing that
secret name, deleting/recreating the same name, or changing key/envelope/index/
profile/payload/context refuses completion. Account/path/key/profile/immutable
projection and actual enabled state are revalidated; label-only permission never
waives any such check. Test label-only pending rename succeeds versus secret-name
change/name-reuse refusal, plus projection/profile tampering and standard omitted-
type byte parity, at their actual owning boundaries. An existing
valid public signature over identical material can still satisfy an eligible
request under the accepted current model; do not advertise independent per-request
ceremony attestation.

Implementation inventory that must be tested end-to-end: protected precedence
before `_preflight_hd_signing`/CLI avault discovery and HD headless/account guards;
`create_sign_request` must not overwrite an HD Solana binding with the ordinary
Solana snapshot; selected derivation must reach `_attach_signed_sign_operation_context`,
protected `vault_sign`, validation/completion, and the Web protected completion
branch (currently only the standard branch repeats chain/account). Exercise CLI,
API and Web with unavailable/old avault, false stored flags, nonzero accounts,
stale envelopes, missing/mismatched Bitcoin address_type and request reuse. Thread
that type with the same persisted derivation through sign request, signed release,
lookup, selected-key verification and completion. Merely relaxing standard-only
checks is not an implementation.

## 6. Compatibility, recovery and unified UX

No database migration is expected: additive typed metadata and existing keypair/
hd-seed row. Verify, do not assume, released 3.2.6 safely lists/degrades and refuses
all signing/value delivery of protected HD with NO AUTOMATIC mutation in supported
read/refusal/startup flows, then resumes on upgrade. Ordinary keys/standard HD
work throughout. Do not claim every explicit old-version management write is
blocked: old `update_secret_classification` can retag this future row as standard.
Unsupported-downgrade management edits are outside the non-mutation guarantee;
warn not to edit/classify new HD entries using old clients. Demonstrate the known
old-write limitation separately; never silently count it as a successful refusal.
Do NOT expand RC16/3.2.6 to retrofit a future feature guard. New 3.2.7 HD management
rejects tier/envelope replacement and fails closed for contradictory old-edited
state. Existing Passkeys are reused, never re-enrolled/reset just for a test.

Protected HD recovery copy: **Words plus the original passphrase restore the
wallet even if the Passkey is lost.** Ciphertext/root metadata backup preserves
existing records; losing the Passkey still prevents opening those ciphertexts.
Seed/entropy backup is not the DB; test consistent DB/WAL+root/factor restoration
and independent word+passphrase recovery separately. Anyone with the recovery
words and passphrase bypasses Vault approval outside Avibe.

One Web owner designs standard/protected create/import/backup, the same steps,
wording where state matches, three-word quiz and account cards. Include both
tiers' create-passphrase field in ONE owner frame set before any UI merges.
Standard create-passphrase may ship as a small Web-only PR after frame approval;
it must not wait for HD custody. Parent form never handles protected plaintext.
Web receives the handoff only after its RC16 work is ready; #3673 was confirmed
MERGED Nov13. Owner BTC-switch3.2.6 has priority. The policy3.2.6 Web consumer is a
separate prerequisite, not an excuse to silently fold this HD work into it.

## 7. Release and acceptance contract

HD remains3.2.7. Branch work may begin after exact contract ACK before stable;
merge/publication still wait Product/lead's applicable release go. Policy-turn-off
+ five display refinements independently aim at3.2.6 per Product/owner Nov13
14:04, ONLY when its sandbox pin, #3652 and actual Web consumer are complete and
exercised together without delaying the BTC RC. Pin + backend alone is not ready.
HD follows in a new immutable release/pin. The durable shared setup/root contract
in Avibe #3652 (new head c5ce87af under review, not yet landed at this writing) is
an explicit prerequisite for the final HD consumer, absent on checked base14a33c.
Land that contract normally first; no stacking, cherry-picking its private code
or reimplementing setup. Independent pieces can be prepared on a fresh default
branch. Estimates/checkpoints use writing hours,20–40min review/CI rounds and
named waits; old Nov16–20 human-day dates are withdrawn. A missing dependency
is reported with actual remaining hours and next signal, not an invented ready
consumer. No mutation of retained bytes.
A source-derived candidate is checked against live publication for equality,
never chosen from live downloads. Separate publication/install authority stays.

Policy physical acceptance folds into the FINAL3.2.6 owner hand-test; no extra
policy-only ceremony. Protected HD is tested on FINAL3.2.7 with its own installed
consumer head/pin recorded. This supersedes the earlier one-combined-HD-window
plan. Prior policy source/CI/synthetic/owner evidence does not clear changed HD
content or require redundant re-enrollment/ceremonies.

Funds-safety acceptance is Product's mnemonic acceptance.md adapted to this
custody path: official + >=20 random12/24-word/passphrase cases, all20x5 public
keys independently derived/stored/precommitted plus all currently enabled
three-chain shown/signature-verified keys; actual displayed words->stored seed;
reference-wallet address checks at the exact section1 paths (do not infer a
wallet's hardened-account #i matches this public-address #i); failure/corruption/rollback;
independent complete-feature GPT/xhigh review; one final visible owner session
with explicit disposable-wallet budget/destinations, SOL #0/#2, BTC testnet4 #0/#1,
EVM testnet #0/#1 receive/spend, signatures verified before broadcast. Synthetic
WebAuthn/browser tests are labelled synthetic. Heavy matrices use one lead-granted
local Incus slot; no live Mac synthetic load or real secrets in background tests.
Final feature/consumer PR is not Merge-ready until the final acceptance is done.

Each PR must have one owner-specific persistent combined CI/review Watch, armed
before its first watched push/review trigger and retained through merge cleanup.
Current-head SHA-bearing Codex conclusion is mandatory; failed/unpicked/stalled
reviews auto-trigger one @codex review per recovery round, never while a review
is already running. After every completed pushed head, arm a Harness one-shot
pickup-check Task at +10 minutes: re-read the current head; only if it has no
valid pass AND no running/eyes pickup, post one standalone `@codex review`, record
that comment and verify pickup. Reports name both Watch and pickup-Task IDs.
After three same-head stalls, escalate instead of looping.

## 8. Joint-ACK checklist

- [ ] Sandbox author: payload codec / record AAD / trusted ceremony / release parity.
- [ ] Backend author: create storage / signed snapshot / completion / old-version bounds.
- [ ] Independent GPT/xhigh: cross-boundary confusion and recovery/funds invariants.
- [ ] Web owner after RC16 ready: actual client names / unified approved frame plan.
- [ ] Exact public vectors accompany final frozen revision; hashes reported to all lanes.
