# Agent-generated static Standard secrets (B1)

Related: #3338, target 3.2.7, `2026-10-13-vault-multi-field-secrets.md`,
`avault-custody-core.md`. This is the Avibe integration contract; the static
random generator and sealing algorithm belong to avault.

## Invariants and boundaries

- `vibe vault generate NAME` creates only a new static Standard secret; an
  existing static Standard secret requires an explicit, unused `--field`.
  Existing fields, including the default alias, are never overwritten or
  rotated. Protected and keypair targets fail with a `vibe vault request` hint.
- The CLI is a thin, value-free client. A named `vault.generate` request goes
  over the existing authenticated private control channel to a service-owned
  worker. Editor admission and remote invocation authority follow the existing
  control machinery. No SQLite, random generation, cryptography or avault
  invocation happens in the calling Agent CLI process. No offline fallback,
  install, service start or restart is attempted.
- The service uses the merged shared recipe normalizer from B2, checks target
  and metadata before custody, then calls avault >=0.1.13:
  `generate-secret --name RECORD --type password --length N` with optional
  `--no-digits`, `--no-symbols`, `--no-lookalikes`; hex/base64 use `--bytes N`.
  Only the existing three-string Sealed envelope returns to Python. Custody
  output is not echoed in errors. Version errors expose the fixed version floor.
- Password defaults: 24 characters, 12..100. The user picks 8..100 in the
  browser or a request preset; an Agent-generated password deliberately keeps
  custody's floor of 12 (Product, 2026-11-14), and 8..11 is refused before
  custody with a pointer to a request preset. Avibe refuses above 100.
  Letters always, digits/symbols on, lookalikes allowed. Hex/base64 default to
  32 bytes, 16..64. Cross-type, unknown, wrong-type or out-of-range recipe
  options reject the entire call. Python does not implement or test the RNG
  algorithm.
- A preflight snapshot is repeated under a short writer reservation after
  custody; all storage, metadata, provenance and audit writes share one
  savepoint/transaction. Concurrent new-name/new-field writers cannot overwrite
  each other. A failed generated audit rolls the whole operation back, even
  when a storage caller catches its exception. Custody does not hold a DB lock.
- Metadata flags follow existing `vault edit` replacement/preservation
  semantics. Omitted fields, unrelated tag categories and internal policy keys
  remain intact; a metadata change during custody is a conflict, not a lost
  update.

## Provenance and public result

Store provenance in the existing `public_meta`; no schema migration:

```json
{"generated":[{"field":"password","by":"agent","session_id":"ses_example","session_title":null,"at":"2026-11-14T00:00:00+00:00","type":"password","length":24}]}
```

One entry per current generated field. The typed public projection accepts only
these scalar fields, `by: agent|cli`, nullable session ID/title, and exactly one
format parameter (`length` or `bytes`). It never reflects arbitrary nested
public metadata. Creation cannot supply provenance: it is storage-owned.
Default-label renames move provenance; field replacement, removal and default
rotation clear only the affected entry. B3 atomic batch mutation applies the
same helpers to final labels, including rename-plus-replacement in one commit.

A Session ID comes only from the existing caller environment, never from recipe
or request arguments; its title is resolved server-side. An owner-terminal call
without a resolved Session is allowed and records `by: cli`, null ID and title.
Known boundary: the private channel authenticates the local control transport;
its descriptive caller fields are not a cryptographic identity boundary against
same-UID local callers. No new caller proof or session-required rule is added.

`list`, `find`, REST and generate expose the same typed generated array. The
command returns the normal masked list row as `secret`, plus an operation-level
`generated: {by,session_id,type,length|bytes}` summary. It does not replace the
row's array with a second incompatible shape. Successful operations audit
`generated` with that value-free summary, plus the existing audit access
snapshot; rejected operations store neither a record nor a generated event.

## Validation and integration gates

- Storage: new-name/new-field/default alias/kind/tier guards, actual concurrent
  commits, metadata race rejection, savepoint rollback, per-write-path
  provenance maintenance, strict masking and list/find/REST projection.
- CLI/control: DB-free caller, no offline fallback or process start, parser
  errors with help, typed IPC/recipe/metadata rejection, editor admission,
  minimum-version errors, no spoofable provenance argument, stdout/stderr/log
  and audit sentinel checks.
- Wrapper: exact flags, strict Sealed output, version floor before custody,
  generic malformed/error output without reflecting process bytes.
- Opt-in real custody smoke:
  `AVIBE_TEST_AVAULT_GENERATE_BINARY=/absolute/test/avault` runs only an explicit
  compatible test binary with `--store file` and a private 0700 `AVAULT_HOME`.
  The service receives only envelopes. A silent intended child checks delivery
  format; generated plaintext is never returned to Python or test logs. Tests
  skip clearly when no explicit binary is supplied. Synthetic custody mocks
  prove orchestration, not end-to-end plaintext isolation.
- No consuming PR before the avault primitive contract is merged and its final
  version known. B2/B3 and concurrent skill changes are consumed only from merged
  master. Release/pin, exact-head independent security review, merge and local
  Incus regression are owned by the parent lanes. This writer does not merge,
  deploy, install or restart runtime components.
