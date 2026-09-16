# Preserve release intent and the established installer entrypoint

## Scope and contracts

Two failures observed during the packaged preview trial have separate owners:

1. Release notes read notification intent from the remote annotated tag, not a
   runner-local tag that checkout can replace with its peeled commit. Fetch the
   exact remote tag object without changing local tag refs, and require its
   peeled source to match the source used for the notes. A lightweight tag's
   commit message is not a tag annotation. Existing strict HTML-comment body
   fallback, both current/legacy notification markers, and the exact source/run
   readiness marker remain intact. Remote lookup or source mismatch fails before
   notes mutation; it must not silently turn a silent release into a notification.
2. The POSIX installer first reuses a recognized Avibe `vibe` entrypoint in an
   eligible writable directory on the original PATH, in PATH order. Keep the
   entrypoint directory, not its resolved package-generation directory.
   Relative, transient, and sbin entries remain ineligible for this preference.
   Explicit generation/tool-environment bins are transient too, consistent with
   the shared activation owner's existing stable-launcher boundary.
   Recognize the shipped `vibe.cli:main` console script statically, including
   legacy pip/uv installations. Never execute an unknown same-name command to
   discover ownership. With no eligible existing entrypoint, retain selection
   order among safe destinations. Occupied unrecognized paths, including broken
   links, are not available for fresh installation or fallback activation.
   Off-PATH, broken, and non-executable entries do not override PATH.
   Staging, source-generation snapshot, and shared atomic activation stay owned
   by the existing installer protocol.

Windows already selects an explicit stable directory (configured tool bin or
user-local bin), rather than the first writable PATH entry. No Windows change
is needed for this failure.

## Verification

- Execute the actual notes workflow shell against disposable real Git repos:
  annotated tags with current/legacy intent, checkout-rewritten local refs,
  lightweight tags, prose-only mentions, existing-body fallback, idempotence,
  unavailable remote refs, and mismatched remote sources.
- Run the complete POSIX installer in isolated homes with its existing fake
  package producer, real console-script shape, and activation protocol. Check
  unowned executable/file/link/directory occupants in both PATH orders without
  executing or overwriting them. Check the final launcher, original
  generation preservation, absence of a second entry, source snapshot, PATH
  precedence, and excluded directories. Failure must preserve the old launcher.
- Run adjacent release/installer tests, changed Python lint, shell syntax, and
  whitespace checks. PR review and exact-head CI remain required.

Local result: 517 tests passed with zero skips across the installer, actual
notes/public-verification shell, release state machine, update notification
consumer, upgrade/activation flow, and package integrity suites. Changed-file
Ruff 0.4.9, Bash syntax, whitespace, and UI build passed. Before the fix, the
new regressions reproduced lost annotation intent, commit-prose false positives,
missing/mismatched remote acceptance, and lost stable-launcher source identity.
The task environment needed its own editable package/UI build for isolated
upgrade subprocess probes; no production package was installed.
Ownership regressions failed before the installer guard and passed afterward;
the current managed/legacy launcher fixtures use executable Python entrypoints,
not arbitrary same-name shell programs. Unknown custom wrappers are preserved,
not probed or automatically adopted.

## Boundaries and known-by-design

No version/tag/asset changes, workflow dispatch/rerun, publication, production
installation, service restart, configuration change, or real Show Page edit.
Official v3.1.0 remains paused. This work does not change release package
ownership, platform matrices, verification gates, or activation semantics.
PR creation/review is authorized; merge awaits a separate owner instruction.

Separately authorized Show Page repairs are outside this repository change and
do not expand its release, deployment, or merge authority.
