# Memory MVP Architecture Follow-ups

## Background

The Memory MVP architecture review identified a mix of product-scope decisions,
implementation defects, layering debt, and repository cleanup. Cross-format
runtime upgrades remain part of the MVP contract.

## Goal

Finish the reviewed Memory architecture without weakening its existing user
contract:

- preserve data across same-format and explicitly compatible runtime upgrades;
- reject an incompatible non-empty provider root while keeping the previous
  runtime active;
- allow an incompatible runtime to activate after Clear all leaves a verified
  empty owned root;
- roll back the pointer, sentinel, runtime metadata, and sidecar if activation
  fails;
- keep platform protocol details inside IM adapters;
- remove test-only or dead Memory paths that have drifted from production;
- make the POC and review-document lifecycle explicit.

## Implementation

1. Fix provider-root inspection so ownership and sentinel validation happen
   before the root is scanned, then apply compatibility based on emptiness.
2. Add manager-to-controller activation tests for incompatible empty-root
   success and rollback.
3. Add a normalized ordinary-text fact to `MessageContext`. Supported IM
   adapters must set it from their native event; Memory fails closed when it is
   absent.
4. Remove `enqueue_capture`, `MemoryProviderMessageFailure`, `increment_missed`,
   and unused Memory UI translations after moving tests to production paths.
5. Mark the EverOS POC as executed and archived with its partial evidence and
   failed quality gate preserved. Remove the four rev37 comparison copies after
   the review version map is retired.
6. Publish the managed runtime artifacts before changing the packaged manifest
   from `unavailable` to `published`. Never publish placeholder digests.

## Managed Runtime Release Boundary

As of 2026-07-23, the release tag `memory-runtime-v1.1.3-1` does not exist and
the repository has no reviewed build pipeline for a relocatable Python 3.12 plus
EverOS environment on all four declared targets. The POC virtual environment is
host-bound and is not a publishable artifact. This branch therefore deliberately
keeps `vibe/memory_runtime_manifest.json` at `release_state: unavailable` and
supports local dogfood only through `AVIBE_MEMORY_DEV_RUNTIME`.

Managed installation remains a release blocker until a separate release change
builds and clean-host tests all four archives, publishes those exact bytes, and
then records their archive and embedded-Python hashes in the manifest.

## Verification

- focused Memory runtime, store, adapter, command, and UI tests;
- UI production build;
- Ruff on changed Python files;
- full local pytest once after focused checks;
- independent Standards and Spec review before commit.
