# Avibe-managed CPA v8.1.2

Avibe pins `model-hub-engine-v8.1.2-1` for its Model Hub runtime. The upstream base is `router-for-me/CLIProxyAPI` tag `v8.1.2`, commit `85225381d89915595598ed13766cd79890c03a84`, built with `go1.26.10`.

## Changes from the v8.0.63 patch set

The first five patches are carried byte-for-byte in the same order. Their contracts and removal conditions are documented in `../model-hub-cpa-v8.0.63/README.md`.

- **Compatibility-provider item IDs:** Rebased the long ID exception on the v8.1.2 HTTP and WebSocket call sites. Preserve the new WebSocket explicit-empty-instructions snapshot as well as the `isCompat` choice. Native Codex keeps its ordinary ID normalization; image-specific requests are unchanged. The primary checks are `TestCodexExecutorExecuteStreamKeepsUpstreamIDsOverTheLimitForCompatModel` and the positive and negative `TestSanitizeCodexInputItemIDs*Compat*` cases.
- **Config reads and writes:** The old private `config-locked-reads.patch` is superseded by a different upstream implementation, not by the nominal upstream PR. Do not restore the old process-only mutex or replace the new compare-and-swap writer. Management reads and the watcher call the snapshot/shared-lock API; writes in the management, startup and comment-preserving YAML paths use the exclusive file transaction. Rewrites preserve the open file and sync its bytes. `TestConfigFileOSLocks`, `TestConfigFileCASWithConcurrentWriters`, `TestConfigFileRollbackRestoresCompleteSnapshot` and `TestConfigSnapshotCASRejectsDifferentBytesAndInode` exercise the upstream implementation. The watcher, management and startup callers remain part of the review boundary.
- **Claude retry headers:** Keep v8.1.2's `statusErr` fields (including terminal TOS state) and direct-response primary-header precedence. Filter the separate upstream header collection before the direct/ordinary response branches; a trusted CPA cooldown takes precedence over upstream retry timing. The source and fast decode checks plus the `TestExecutionErrorMessage*` consumer checks run against this port.

Build all six archives with `scripts/build_model_hub_engine.py --candidate`, verify the produced manifest and archives with the release guard, then exercise the selected engine through a normal Avibe wheel with synthetic upstreams. The verified build is published as a non-latest engine release before the Avibe pin lands; this is not an independent channel promotion.
