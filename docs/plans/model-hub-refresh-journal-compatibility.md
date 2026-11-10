# Model Hub refresh-journal compatibility fix

## Change contract

CPA v8.0.63 writes a durable `.codex-refresh/<hash>/receipt` journal under its
auth directory (`internal/auth/codex/refresh_journal_file.go`, upstream commit
`6db7d4b839d86c5b24e17bbe73bcd4db18ef1b1a`). The previous flat-file audit rejects
the directory and prevents an otherwise healthy engine from starting.

- Accept only the named `.codex-refresh` directory at the auth root; continue
  rejecting other directories, symlinks and special files.
- Audit journal descendants as private directories and regular private files,
  without reading, deleting or interpreting receipt contents. Preserve existing
  `enforce=True` repair of regular-file permissions; unsafe directories and links
  fail closed. Tolerate a journal entry disappearing during CPA's atomic receipt
  replacement, not an entry with an unsafe type or permissions.
- Keep old flat credential directories working. No migration, engine version
  change, credential rotation, deployment or release is involved.
- Doctor repair JSON must emit its existing result, including one `kind`, with
  the normal CLI schema wrapper, rather than raising a duplicate-argument error.

## Test contracts

The state audit owns filesystem admissibility. A synthetic journal with a claim,
receipt and temporary receipt exercises the new engine shape; table-driven unsafe
entries protect rejection and non-mutation in both audit modes. Existing tests
cover only flat credentials and lock sidecars, not a journal subtree. A rename
case protects the distinct live-engine race where a temporary receipt vanishes
between enumeration and `lstat`; a permissions case verifies regular-file repair
without changing receipt bytes. Extend the existing native-Windows startup test
to verify journal DACL inheritance: POSIX tests cannot exercise this boundary.

The CLI test runs parser -> command -> real dry-run repair -> JSON output, with
no repairs performed. Existing repair tests stop at the result dictionary, so
miss the duplicate `kind` in the output adapter.

## Validation evidence

- Pre-fix production files: both journal acceptance cases fail with the original
  unsafe-entry error; the real Doctor dry-run fails with duplicate `kind`.
- Fixed code: 28 state/audit/lock tests and 54 Doctor tests pass.
- Private filesystem tests: 5 pass locally, 4 native-Windows cases skip on macOS;
  the existing Windows CI job owns those native checks.
- Changed-file Ruff and `git diff --check` pass.

CI and exact-head Codex review remain delivery gates. No production paths or
credentials are used. No remote deployment or service restart was performed.

## Known by design

Journal contents and filenames below the reserved root belong to CPA. Avibe
checks filesystem safety, not the journal schema, and does not delete refresh
receipts with an OAuth grant: CPA uses them to preserve unresolved outcomes.
