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
| All public identities and signatures match independent corpus | Real custody import/sign integration | Wrong derivation, tweak or selected account | Corpus ready; isolated real-custody execution pending |
| Browser displayed words equal HPKE-sealed seed and stored public identities | Web create/import component and Incus E2E | Display uses one seed, import seals another | Web-owned; pending |
| Restart, binary upgrade and full custody-state restore | Real isolated processes / file store | DB alone works but missing machine key makes funds inaccessible | Pending |
| Corrupted or mismatched envelope/key/name fails closed | Real avault through API | Wrong restored state signs under wrong key | Pending |
| Reject chain/scheme/account mismatch and disabled account | Existing API tests plus real custody negatives | Ambiguous selection reaches key use | Existing focused coverage; expanded matrix pending |
| Ask-before-signing and authorized lowering | Shared ordinary-keypair/HD policy follow-up | Agent bootstraps factor or replays/misbinds downgrade | Blocked on explicit first-factor trust decision |

Tests already covering malformed custody replies, immutable approval targets, wrong-account signatures, record replacement during signing and legacy-key CLI compatibility remain primary tests for those contracts; do not duplicate them as source-text checks.

## Recovery and semantic limits

- A passphrase wallet requires **the words plus the exact passphrase** to recover. A valid different passphrase produces a different wallet; it is not detectable as a wrong password by BIP39.
- A database backup is not a custody backup: record the machine-store/key dependency separately. A full-state file-store relocation test does not prove platform-Keychain portability.
- A failed create before commit leaves no wallet; a lost response **after** commit may leave a complete wallet. Retrying must discover/preserve it rather than overwrite it. Do not promise every error means nothing was saved.
- Standard HD raw-digest/message signing does not prove transaction amount or fee semantics. Typed-context verification and raw/blind approvals must be reported separately; never label unparsed values verified.
- Previous released-3.2.5 → HD → released-3.2.5 evidence established byte preservation and refusal by the old reader, not the new full matrix or an avault binary upgrade.

## Execution

Use a runner-created isolated local Incus worktree environment after obtaining the lead's heavy slot. One sequential worker; cargo one job (two maximum), explicit HOME/XDG/AVAULT_HOME and file store, small consistent SQLite backups, no network broadcast. Keep real owner state, services and Keychain untouched. Report exact source/binary versions honestly: development master currently reports 0.1.10; any consumer version-probe override is test-only and does not satisfy final released-binary acceptance. Remove the owned environment and return the slot after execution.
