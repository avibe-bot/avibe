# HD wallet funds-safety acceptance

## Goal and boundary

Before proposing avault 0.1.11 release or enabling the seed-wallet feature, establish that displayed addresses, independently derived public identities, persisted custody and actual signatures agree. This extends `vault-hd-wallets.md`; it does not change the production version gate or authorize deployment, real-money transfers, a sandbox release or pin changes.

Backend owns the server side of address/seed linkage, persistence/fault recovery and signing-policy enforcement. Web owns displayed-word creation/import, browser derivation/sealing and rendered-card checks. Both join evidence on the same public fixture `id` and `seed_sha256`. Third-party wallet compatibility and owner-led funded hand tests remain distinct acceptance gates. The final feature needs independent adversarial GPT/xhigh read-only review before the last PR is Merge-ready.

## Corpus and oracle independence

- Official BIP39 vectors plus at least 20 newly sampled, explicitly PUBLIC NEVER-FUNDED entropy cases: five each in 12-word/24-word × empty/nonempty passphrase groups, including Unicode NFKD cases.
- Python `bip_utils`/`embit` generate the oracle independently of Rust avault and browser scure/noble. Record exact library versions, upstream vector provenance and generator command.
- For every entry and every index 0..19 compare all three chains, including EVM recovery, Ed25519 verification and BIP86 verification under the **tweaked output key**, not the internal key.
- Canonical paths: EVM `m/44'/60'/0'/0/i`; Bitcoin `m/86'/0'/0'/0/i`; Solana `m/44'/501'/i'/0'` (all Solana levels hardened).
- Testnet Bitcoin address fields re-encode the same script for an isolated spend test; they do not silently change coin type or imply a production network-selection feature.
- Synthetic mnemonic/seed fields in this corpus are intentionally public test vectors. They must never receive funds. Funded hand tests use a separate disposable wallet whose private material is never committed or logged.

## Primary tests and credible failures

| Contract | Primary owner / test | Regression being excluded | Status |
| --- | --- | --- | --- |
| Custody interruption leaves no partial wallet | `test_vault_hd.py`, API before persistence | Stub row prevents retry after failed import | Passed (RuntimeError and interruption cases) |
| Post-insert failure rolls back row, policy and audit | Same, actual SQLite transaction | Wallet row commits without companion metadata / audit | Passed |
| Lost response after commit cannot replace a seed on retry | Same, create API | Retry silently rotates an already-funded address | Passed |
| Simultaneous same-name imports have one complete winner | Same, concurrent API calls after preflight | Last writer swaps public/custody state | Passed |
| Consistent SQLite backup preserves full envelope and all account state | Same, SQLite backup API | WAL-dependent data omitted or metadata reset after restore | Passed; not proof of machine-key portability |
| Public address serialization matches independent corpus | `test_vault_hd_corpus.py`, public metadata owner | Wrong address encoding, path label or Taproot parity/tweak | Passed: 37 entries × 20 accounts × 3 chains |
| All public identities and signatures match independent corpus | Real custody import/sign integration | Wrong derivation, tweak or selected account | Passed: 37 real HPKE imports, 2,220 API signatures and independent public-key verification |
| Browser displayed words equal HPKE-sealed seed and stored public identities | Web create/import component and Incus E2E | Display uses one seed, import seals another | Web-owned; pending |
| Restart, binary upgrade and full custody-state restore | Real isolated processes / file store | DB alone works but missing machine key makes funds inaccessible | API-process restart and consistent DB/full-file-store relocation passed; released 0.1.10 ↔ unreleased HD build store compatibility passed; final released 0.1.11 binary still pending |
| Corrupted or mismatched envelope/key/name fails closed | Real avault through API | Wrong restored state signs under wrong key | Passed: ciphertext, nonce, wrapped DEK, DEK nonce, other-wallet envelope and other-wallet public key; positive controls before/after |
| Reject chain/scheme/account mismatch and disabled account | Existing API tests plus real custody negatives | Ambiguous selection reaches key use | Existing focused coverage; expanded matrix pending |
| Ask-before-signing and authorized lowering | Shared ordinary-keypair/HD policy follow-up | Normal API lowering lacks fresh verification, or replays/misbinds a downgrade | Policy scope/first-factor flow awaits Product decision; existing standard-tier approvals are not a same-OS-user hostile-shell security boundary |

Tests already covering malformed custody replies, immutable approval targets, wrong-account signatures, record replacement during signing and legacy-key CLI compatibility remain primary tests for those contracts; do not duplicate them as source-text checks.

## Recovery and semantic limits

- A passphrase wallet requires **the words plus the exact passphrase** to recover. A valid different passphrase produces a different wallet; it is not detectable as a wrong password by BIP39.
- A database backup is not a custody backup: record the machine-store/key dependency separately. A full-state file-store relocation test does not prove platform-Keychain portability.
- A failed create before commit leaves no wallet; a lost response **after** commit may leave a complete wallet. Retrying must discover/preserve it rather than overwrite it. Do not promise every error means nothing was saved.
- Standard HD raw-digest/message signing does not prove transaction amount or fee semantics. Typed-context verification and raw/blind approvals must be reported separately; never label unparsed values verified.
- Previous released-3.2.5 → HD → released-3.2.5 evidence established byte preservation and refusal by the old reader, not the new full matrix or an avault binary upgrade.

## Isolated runtime evidence — November 12, 2026

Runtime source was Avibe `39917beaf4652c992306ed99270d8a90b4199d34` (no runtime change from its base `3210cfa89`), with avault `fcefea28a63016085ead3650f05c7b272bae1589` built on Linux arm64 using Rust 1.92.0 and `cargo build --locked -j1`. The HD binary SHA-256 is `cb9d273224cf4f3a3137c1733b629cd904129935bf27855f9e565125c17bfdf9`. It honestly reports **0.1.10**, not a released 0.1.11: the initial API first refused its real version without persisting a wallet, then a child-process-only version-probe override exercised the unreleased implementation. Production source/pins were unchanged.

One lead-authorized, seed-none local Incus worktree environment ran sequentially with an explicit isolated file store and HOME/XDG paths. The test program passed HPKE blind boxes to the real Avibe API, enabled all 20 accounts, compared addresses/paths with the independent corpus, and signed each tuple. The independent verifier uses corpus keys rather than returned keys: EVM low-S verification and key recovery, Bitcoin Schnorr under the tweaked output key, and Solana exact-message verification. It rejects missing/duplicate tuples and wrong deterministic inputs. Its public-vector self-test passed 8 positive and 48 rejection checks.

| Stage | Independently verified signatures | Public JSONL SHA-256 |
| --- | --- | --- |
| Initial HPKE import/API signing | 2,220 (740 per chain) | `d30de6ef50d854809b552cca11f4e9ac342e5281ebdbccf65ca8f1702ed48fa7` |
| Fresh API process, same persisted state | 2,220 | `3e17b36afb776ead09ea14294ffc3bca9e718b08556e994f816a7cc484c7fb7d` |
| Consistent SQLite backup + full file-store state copied to a new path | 2,220 | `ead85edc5d7cf8d138bc9c646dc3167ef88f09440ca6a1a595cde5faa67637da` |
| Store initialized by released 0.1.10, then unreleased HD import, old-binary refusal, new-binary signing | 60 (all accounts of one synthetic case) | `37574b70af0cf425668bf0f1aa9c247d189a145927b9179dee7f41817c05ed17` |

The released 0.1.10 Linux-arm64 archive matched its published digest `ffed26317b9557c76913c07cb2dd640b088c1abe21364cb54ad43603c601d17a`; its extracted binary hash was `65ac9c85a2411a265b6f1acb3c5fab8988af6f370bbde40c84887daf3eaff4cb`. Old and new binaries reported the same machine public identity. The released binary refused an HD envelope with exit 70; the new binary subsequently signed correctly. This is **released-to-development store compatibility**, not acceptance of a future published 0.1.11 artifact.

Additional real checks passed:

- All six corruption/substitution cases refused without returning a signature, incrementing use count, or appending a successful-sign audit. Healthy positive controls succeeded before and after. Envelope failures reported `avault_failed`; substituted public-key verification reported `invalid_request`.
- A deliberately killed, owned API child reached a real INSERT/audit inside the create transaction, then received SIGKILL before commit. A new process observed the original row/policy/audit counts and SQLite integrity; retry created exactly one complete wallet and matching account-0 addresses.
- Restoring only the database into an empty custody store refused signing without modifying any of the 37 wallet rows. The consistent database snapshot contained none of the corpus's plaintext mnemonic or seed byte/hex patterns. This is not a general forensic guarantee or OS Keychain portability claim.
- Final runner health passed; the environment hold was released, the instance/project deleted through the runner, and absence read back. No funded wallet, broadcast, shared master update, owner Keychain or production service was used.

Browser-rendered address/sealed-word agreement, third-party wallets, owner-led funded spends, the shared policy gate, final released-binary acceptance and final adversarial review are still required. API-process restart is not relabeled as a browser/service E2E test.

## Execution

Use a runner-created isolated local Incus worktree environment after obtaining the lead's heavy slot. One sequential worker; cargo one job (two maximum), explicit HOME/XDG/AVAULT_HOME and file store, small consistent SQLite backups, no network broadcast. Keep real owner state, services and Keychain untouched. Report exact source/binary versions honestly: development master currently reports 0.1.10; any consumer version-probe override is test-only and does not satisfy final released-binary acceptance. Remove the owned environment and return the slot after execution.
