## Summary

- capability:
- user-visible change:
- motivation:

## Scenario Coverage

- scenario IDs:
- unit / contract coverage:
- scenario coverage:
- residual manual checks:

## Validation

- commands run:
- result:

## Risks / Follow-ups

- risk:
- follow-up:

## Model Hub engine index allocation (only if applicable)

- Operation and full target release tag; minimum Avibe version (no maximum):
- Promotion: six platform/archive SHA-256 digests and manifest digest:
- Promotion: link the successful rehearsal **run and attempt**. Include its exact `untrusted_rehearsal.json` object under `rehearsal` in `patches/model-hub-engine-allocation-evidence/<16-digit-sequence>.json`; bind `payload_sha256` and `parent_sha256` (null for genesis) to the exact allocation bytes. A PR-body link or a mutable run ID is not evidence.
- Withdrawal: affected full tag, permanent public reason, listed non-withdrawn fallback tag, and minimum-version compatibility:
- Update `patches/model-hub-engine-channel-ledger/current.payload.json` with the complete desired snapshot. Freeze `issued_at` and advance the sequence whenever any byte changes, even after a failed publication. Do not omit a published release without explicitly withdrawing it.
