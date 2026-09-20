# Released OpenCode catalog migration (MODEL-HUB-MIGRATION-001)

The released Model Hub serializer could omit OpenCode `models`, including on
unused, empty catalogs. The current strict parser requires that catalog and a
`native_protocol` on each row. Disk loading must bridge those released shapes
before validation; API writes keep the current schema.

This supersedes the no-upgrade ruling in backend-model-catalogs.md C12 only at
the disk-load boundary. Retired fields, including OpenCode `mappings`, remain
invalid. No UI, runtime routing, schema, or credential behavior changes.

Invariants:

- Missing catalogs become rows in the existing `menu.checked` order; an empty
  selection becomes `models: []`. Existing rows retain their IDs and metadata.
- A missing native protocol is supplied only by unambiguous persisted route
  Sources, or matching model inventory in the configured Source order when no
  route override exists. Only current native protocols are representable;
  Chat Completions, conflicting protocols, and missing evidence require repair.
- Keep selected IDs, exact routes, Source order, opaque credential references,
  and unrelated config. Do not guess protocols from model names or providers.
- Modern configs are unchanged. Ambiguous or malformed input retains the
  original file and uses the existing recovery warning/write guard. Successful
  migration uses the existing private backup and concurrent-write protections.
- Migration is forward-only. The backup preserves the original bytes for manual
  recovery; no automated downgrade/rollback support is introduced. The current
  pre-fix reader accepts the migrated current-schema payload.

Validation uses real disk loads, persistence/reload, an unchanged modern control,
failure preservation, settings API recovery/save, existing Model Hub/config and
Member permission suites, and changed-file lint. Tests isolate all home/backend
state and never invoke real providers. PM owns independent browser acceptance,
PR, review/CI, and any later deployment decision.
