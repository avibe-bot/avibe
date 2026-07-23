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

EverOS 1.1.3 is available as an official Python package, but upstream does not
publish the relocatable runtime archive required by Avibe's dependency manager.
Avibe therefore builds its own four platform bundles from the POC's reviewed,
locked `everos==1.1.3` dependency set and a uv-managed Python 3.12 distribution.
The release pins uv 0.9.18 and Python 3.12.12. The builder verifies the
environment before and after relocation, dereferences validated internal links,
and emits a deterministic archive containing only regular files and directories.

The release workflows publish those archives as immutable assets under the same
Avibe release tag, generate the exact archive and embedded-Python hashes, and
write that published manifest into the wheel. The repository copy remains
`release_state: unavailable` because source checkouts have no release assets;
`AVIBE_MEMORY_DEV_RUNTIME` remains a development-only bypass. The wheel never
contains the large runtime archives themselves.

## Delivered Product Follow-ups

1. Agents receive the read-only `vibe memory search`, `vibe memory profile`, and
   `vibe memory status` guidance only while Memory is enabled and the current
   turn is an interactive Workbench-owner or freshly admitted administrator DM.
   Scheduled, harness, group, and other ineligible turns do not advertise it.
   This recall path remains distinct from shared `user_preferences.md` and
   historical message lookup, and recalled content is explicitly treated as
   untrusted data.
2. Release workflows build reviewed, relocatable, verified runtime artifacts on
   four platforms, publish the exact bytes before the wheel, and generate the
   packaged manifest from those bytes. Every final archive must start the
   production child and pass a UDS health probe. A scheduled manifest-verified
   backup guard detects missing assets and restores only absent immutable bytes.

## Verification

- focused Memory runtime, store, adapter, command, and UI tests;
- UI production build;
- Ruff on changed Python files;
- full local pytest once after focused checks;
- independent Standards and Spec review before commit.
