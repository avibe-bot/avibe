# Desktop zip 9.0.4 candidate (2026-10-16)

## Contract and scope

Replace the desktop workspace's `zip` 4.6.1 with **9.0.4**, preserving
`default-features = false` and `deflate-flate2-zlib-rs`. The manifest's lower
bound and committed lockfile both name 9.0.4. Ordinary Cargo caret semantics
are unchanged; this is not a new exact-pin policy. Do not change Dependabot's
cooldown, packaged Runtime manifests, or installation/update behavior.

The only production Rust consumer is `runtime-host/src/private_runtime.rs`.
It reads the embedded `runtime.zip` (produced with Python `ZIP_DEFLATED` by
`desktop/scripts/build-runtime-bundle.py`) through `ZipArchive::new`,
`by_index`, `enclosed_name`, metadata accessors and `Read`. Test fixtures use
`ZipWriter` / `SimpleFileOptions`. No public API adaptation is needed.

## Candidate identity and selection

- Source: <https://github.com/zip-rs/zip2/releases/tag/v9.0.4>
- Immutable source commit: `7d9988688723f1136966e0e97200f41356dc57eb`
  (annotated tag object `04b8ef34c7fc07dee738a40459dae1a9cd863ad3`).
- crates.io archive:
  <https://static.crates.io/crates/zip/zip-9.0.4.crate>
- Archive SHA-256 / registry checksum:
  `a954114b47a54df49f83d2722814e86b23d4bb1b54a1b90fb7d92b41619ddc22`.
- MIT license; MSRV increases from 1.82 to **1.88** (edition 2024).
  Desktop development uses Rust stable; local verification uses Rust 1.92.
- Registry sparse index <https://index.crates.io/3/z/zip> on 2026-10-16:
  4.6.1 and 9.0.2 are yanked; 9.0.3 and 9.0.4 are not yanked.
- No downstream patches. The lock update is limited to zip's dependency
  closure: add `typed-path`, reference the existing `nix`, remove the now
  unused `arbitrary` / `derive_arbitrary`; no unrelated dependency refresh.

9.0.4 is preferred to 9.0.3 because it adds a separator check in
`enclosed_name` for verbatim path components, an API Avibe calls directly.
It also fixes empty-stream handling. Its optional LZMA/PPMd dependency
updates do not enable those codecs in Avibe. This is a newly released
candidate, not a claim of long-term soak testing.

### Why this replaces #2700

PR #2700 was closed, not merged. Dependabot job
<https://github.com/avibe-bot/avibe/actions/runs/38858098825> recreated it on
`fbd3b7f62b4ffd2475b022c851dce8d4ef023bfc`, filtered all candidates by
cooldown, fell back to 4.6.1 and closed it as `up_to_date`. Its declared
candidate was 9.0.2 but its lockfile selected 9.0.3. This replacement names
one actual candidate throughout; it does not restore the yanked 9.0.2.

### Security assessment

The maintainer's explanation in
<https://github.com/zip-rs/zip2/issues/1075> links the mass yanks to:

- GHSA-vv5m-jxxh-4627: privileged Unix mode preservation, patched in 9.0.0.
- GHSA-4v9h-p7m9-rh26: external symlink targets, patched in 9.0.0.
- GHSA-4684-h2q9-2vcp: chmod through symlinks, patched in 9.0.1.

The RustSec entry RUSTSEC-2025-0168 concerns extraction paths fixed since
2.3.0; it does not establish a new 4.6.1-specific failure. The maintainer
also mentions further unpublished issues; their impact cannot be assessed
from the public evidence available at preparation time.

Avibe does **not** call upstream `extract` or `ZipStreamReader::extract`.
Its existing extraction loop verifies archive size/hash, checks entry count
and declared unpacked total, rejects symlink entries and non-normal paths,
creates files exclusively in staging, masks mode to `0o777`, checks the full
resulting tree, and only then publishes the slot. These facts reduce the
applicability of the listed extract-API advisories; they are not a blanket
claim that Avibe cannot be affected by any parser vulnerability.

## Validation contract

Primary owner: `PrivateRuntimeBundle::prepare` in `private_runtime.rs`.
Existing tests covered a normal Deflate bundle, signed archive identity,
installed-tree verification, reuse/repair and one `../` path. New cases
cover dependency-sensitive behavior that those cases do not exercise:

- Stored and Deflate payloads with non-ASCII names, full installation and reuse.
- Nested traversal and test-owned absolute paths; no outside file written.
- Symlink central metadata, CRC failure, truncated archive, incorrect entry
  count and unpacked-size budget; no staged/published files remain on failure.
- Unix SUID/SGID/sticky flags are stripped while executable bits survive.

Negative fixtures refresh archive identity after mutation so they reach the
ZIP reader/extractor rather than passing because of the outer hash check.
These are dependency compatibility guards, not claims of newly fixed Avibe
bugs. Expected paths/bytes/modes and invalid metadata are supplied by tests,
not by the implementation under test.

Focused commands (from `desktop/`):

```sh
cargo test -p avibe-runtime-host private_runtime --locked
cargo clippy -p avibe-runtime-host --all-targets --all-features --locked -- -D warnings
cargo fmt --all -- --check
```

CI additionally owns the full lint workflow and macOS/Windows desktop Rust
compilation/tests. No runtime binary candidate is being adopted, so the
playbook's engine-candidate Incus E2E does not apply to this Rust library
change. Actual signed desktop bundle installation and a live upgrade are
not exercised by the hermetic tests; do not claim those release checks ran.
