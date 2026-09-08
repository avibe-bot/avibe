# Delivery Rationale and Examples

Optional context for [SKILL.md](../SKILL.md), not a second checklist. Read the
relevant section when diagnosing an unfamiliar failure or reviewing the policy;
routine delivery needs only the main file and its named operational dependency.

## Git success does not prove integration correctness

Two lanes can allocate the same migration revision under different filenames.
Git merges both files, while a shared head declaration has the same value and
appears harmless. The database then sees duplicate revisions or a forked chain.
The same problem applies to route names, fixture IDs, and other shared namespaces.
This explains why the main rule compares allocated identities against the latest
default branch even after a conflict-free Git merge.

An untracked contract creates a related failure: another lane cannot obtain it
from the base branch, and the file may later block the primary checkout's
fast-forward. Since regression synchronizes the invoking checkout, a blocked
checkout can also become a stale deployment source.

## Review activity is not a verdict

A findings review contains `Reviewed commit` too. An owner replying to a thread
can create an empty `COMMENTED` review stamped with the current head. Neither
means that Codex approved that head; author, verdict shape, and revision binding
are independent evidence.

The bot's PR-body reaction is a mutable slot, not a review log. It can show
`eyes` during review and `+1` after a pass, withdrawing the old reaction each
time. Auto-review may pass only through that reaction; an explicit trigger can
produce a SHA-bearing pass comment. Trigger-comment `eyes` also disappears when
review completes, so a later empty reaction list cannot establish failed pickup.

The bundled waiter filters PR reactions to `content=+1`; it cannot establish an
`eyes` to `+1` transition. Its original cursor and the head timeline are what
allow the main policy to bind a new pass. Replacing the baseline after a push
can consume already-arrived evidence and make a completed review look silent.

A lane Run marked `succeeded` proves only that invocation ended successfully.
It does not prove that the current-head review started, the Watch remains live,
or the orchestrator received a report. Those are separate observations.

## A repeated finding calls for diagnosis, not automatic expansion

If a reviewer flags a searchable row's projected title, similar fields may
share the same omission. One useful test property is that every displayed
searchable value can be found through the search predicate. Where rendering
normalizes text, such as HTML whitespace collapsing, matching may need equivalent
normalization at its existing shared boundary rather than per-field patches.

This illustrates the main rule to state an invariant and cover the relevant
shapes. It does not prescribe a new abstraction for every repeated finding.
The circuit breaker asks the orchestrator to establish the cause and smallest
complete scope from the full inventory; it is not evidence by itself that the
architecture must change.

## Why quick-reply and regression guards stay in the main file

Two reports can both offer `合并PR`. A label-only reply cannot distinguish them.
Workbench source metadata may recover the clicked report, but the label or
nearby prose alone cannot. Similarly, an explicit button block may be removed
from text even on a destination that cannot render buttons; ordinary prose is
the capability fallback.

The regression command's switches protect different boundaries:

- `--target master` selects a target; it does not select the sender's checkout.
- Git ignores do not govern rsync exclusions. Ignored local source can be copied
  even when Git status and the deployment receipt say the checkout is clean.
- A normal rsync size/mtime check can retain different receiver bytes. A commit
  receipt then identifies the sender, not necessarily all bytes in service.
- `--reset-mode none` preserves product state, but automatic `.env.regression`
  discovery can still rewrite the runtime environment. An empty regular env
  file also takes the rewrite path; `/dev/null` is a different selector.

These examples explain the input verification, clean source reconciliation, and
runtime-state guards in the main procedure. They add no permission to provision,
change credentials, or operate an unrelated environment.
