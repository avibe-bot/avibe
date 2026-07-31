# Agent Archive Delete

## Goal

Make Agent deletion preserve every durable reference while removing the Agent from normal user-facing catalogs and selectors.

## Contract

- User-created and imported Agent names cannot start with `_`; that namespace is reserved for Avibe internals.
- Deleting a non-built-in Agent is one SQLite transaction that:
  - renames the Agent to a short internal name such as `_pm-a1b2`;
  - disables it and records `archived_at` plus its original display name;
  - rewrites scope, Session, and task/watch definition references to the archived name;
  - rewrites nonterminal Agent Run references so queued work keeps its exact Agent configuration;
  - rewrites structured name snapshots embedded in scope and definition JSON;
  - moves the global default to another enabled, visible Agent when necessary.
- Archived Agents are excluded from normal catalog lists, including lists that include ordinary disabled Agents.
- An Agent archived while enabled may resolve only through a persisted reference. An Agent that was already disabled stays unusable. Neither can be selected for new work, edited, enabled, or made the default.
- Terminal run and event records keep their original Agent name because they are immutable execution snapshots, not live references.
- Built-in Agents retain their existing deletion lock.
- User-visible rename updates the existing Agent row and the same durable references atomically; it no longer clones and deletes.

## Atomicity

The Agent row is updated first inside a single write transaction, followed by every live reference. Any validation or write failure rolls the entire operation back. The old public name becomes available only after all references point to the archived name.

Definition rewrites stamp a dedicated Agent-binding revision in metadata. Full-row task/watch updates compare that marker, so a payload read before rename/archive cannot restore the old public name after the transaction commits.

## Compatibility

- The DELETE API and `vibe agent remove` command keep their existing entry points, but return archive details instead of rejecting referenced Agents.
- Existing Sessions and scheduled definitions resolve disabled archived Agents through a reference-only resolver. Direct Agent selection continues to require an enabled, visible Agent.
- Session create/update rejects archived Agent names, and an archived project default is not inherited by a new Session.
- API payloads expose the original display name for archived Agents while retaining the internal name as the reference key.

## Verification

1. Unit-test name reservation, catalog hiding, default reassignment, reference migration, JSON migration, and rollback.
2. Exercise API and CLI deletion contracts against temporary SQLite state.
3. Verify existing Session/task execution can resolve the archived Agent while direct selection cannot.
4. Build the UI and run focused backend tests.
5. Use the shipped CLI path to archive the local `pm` Agent only after isolated verification passes, then confirm all reference counts moved and no active/queued run was disturbed.
