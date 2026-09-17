# Bounded Codex Model Hub catalogs

## Contract and cause

Content-addressed publication has no retirement owner. The Agent caches a path,
but each per-working-directory app-server continues to consume that path after
the cache is invalidated. Export failure must never select an older generation.

Codex 0.154.0 (`openai/codex`, commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`) calls
`ConfigManager::load_with_overrides` in `thread_start_task`; Config construction
calls `load_model_catalog`, which reads the file again. The hermetic native
consumer test removes the catalog after initialization and observes
`thread/start` fail to load configuration. Handshake-only protection is unsafe.

## Ownership and retention

- Agent cache, pending launch and live transport acquire independent pins on
  the immutable file and explicitly close them at their lifecycle boundaries.
  Invalidating the cache does not release the transport's pin. Finalization is
  only a fallback; captured exception tracebacks cannot delay ordinary release.
- A short cross-process publication lock covers creating/reusing the immutable
  path, acquiring its pin and sweeping. Identical bytes preserve the inode.
- POSIX uses shared file locks. Windows open handles deny deletion. The native
  child inherits the pin, including when its Avibe parent exits first.
- The pinned catalog path is the final app-server override, so backend extra
  arguments cannot silently select a different, unprotected generation.
- Retain all protected generations plus at most one unprotected history file,
  selected deterministically by filename. Age and mtime never prove inactivity.
- Only ordinary, singly linked files matching
  `standard-responses-[0-9a-f]{16}.json` in the Codex Hub directory are eligible.
  Unrelated names, directories, symlinks and hard links are untouched.
- Cleanup is best effort at publication and release. Failure logs preserve the
  path and exception; publication and existing consumers remain usable. The
  bound is restored by a successful later sweep.

## Scope and validation

Changes stay in catalog publication and the Codex Agent/transport lifecycle.
No database, daemon, shared lifecycle rewrite or production-state cleanup.
Validation covers unchanged/changed content, startup and active pins,
invalidation, failed/cancelled launch, actual exit, export and cleanup failures,
cross-process races, and the actual native consumer in isolated homes with
loopback-only egress.

The native inheritance and configuration-reread evidence is for Codex 0.154.0 on
macOS. Custom launcher wrappers must preserve inherited handles for protection
after the Avibe parent dies; normal parent-owned lifecycle protection does not
depend on child inheritance. Pinned descendants can conservatively extend a
generation's lifetime. This change does not manage manually launched processes
that bypass Avibe's pin-acquisition protocol.

## Progress

- [x] Read current issue, default branch and consumer lifecycle.
- [x] Reproduce post-initialize catalog reread with native Codex 0.154.0.
- [x] Implement and test ownership, retention and failure boundaries.
- [x] Complete focused suites and changed-file Ruff: 513 passed, 8 opt-in native
  prompt tests skipped; two additional native catalog tests passed on 0.154.0.
- Exact-head Codex review and GitHub CI are tracked in the implementation PR.
