# Why the PR Delivery Rules Exist

Background for [SKILL.md](../SKILL.md), not a second checklist. Each section
explains one rule through the failure that produced it. Read a section when the
rule seems wrong for your situation or a failure looks unfamiliar.

## A clean Git merge can still be a broken integration

Two lanes can each add a migration with the same revision ID under different
filenames. Git merges both files without a conflict, but the database now sees
two heads or a duplicate revision. Route names, fixture IDs, scenario IDs and
other shared namespaces fail the same way. That is why the final rebase compares
allocated identities with the latest default branch, even when Git reports no
conflict.

A contract file left untracked causes a related failure. Other lanes cannot get
it from the base branch, and it can later block the primary checkout's
fast-forward. Regression syncs the checkout that runs it, so a blocked checkout
can quietly become a stale deployment source.

## Review activity is not a verdict

- A finding review also says `Reviewed commit`. An owner's thread reply can
  create an empty `COMMENTED` review stamped with the current head. Neither means
  Codex passed that head. Who wrote it, what it concluded, and which commit it
  names are separate facts.
- The bot's reaction on the PR body is one mutable slot, not a log. It shows
  `eyes` while a review runs and `+1` after a pass, replacing the previous
  reaction each time. The `eyes` on a trigger comment also disappear when the
  review finishes, so an empty reaction list later does not mean the trigger was
  never picked up.
- A `+1` carries no commit. Suppose head A is under review and you push head B.
  When A's review finishes with no findings, its `+1` arrives after B was pushed
  and looks exactly like a pass for B. The bot's review summary comment does
  name the commit: its `codex-review-state` line records the full `commit_sha`,
  the status and whether there were findings. So the pass is read from that
  state, matched to the current head, and the `+1` only says it is worth reading.
- The summary comment is edited in place as each review starts and finishes.
  Read its current body, not a copy seen earlier.
- Rebuilding the Watch or resetting its cursor after a push can consume events
  that already arrived, so a finished review looks silent until you read the
  live state.
- A lane Run marked `succeeded` proves only that the invocation ended. It does
  not prove that the current head's review started, that the Watch is alive, or
  that the orchestrator received the report.

## Repeated findings call for diagnosis, not more patches

Suppose a reviewer flags that a search result's title is not searchable. Other
displayed fields probably share the omission, and fixing them one per round
invites the next finding. The useful step is to state the invariant (every
displayed searchable value matches the search predicate) and fix it at the
shared boundary, for example by normalizing text where the renderer also
normalizes it.

The circuit breaker exists to force that step. It does not mean the architecture
must change. It means: build the full inventory, find the cause, and choose the
smallest complete scope. Sometimes that scope is smaller than the PR, and the
right move is to split or drop the part that keeps generating findings.

## Self-merge is bounded

An agent that is sure of its PR and blocked on it may merge without asking,
because waiting adds delay and no information. The bounds keep that judgment
where it is cheap to be wrong:

- **Merge-ready on the exact head.** Confidence never replaces the gates.
- **Within your own task.** Another lane's area, or a shared contract, needs its
  owner.
- **No release or tag, migration, or security boundary.** These are hard to
  reverse or reach every user, so they need the user's or orchestrator's intent.

## Gate evidence and user reports are different surfaces

Exact heads, run IDs, thread counts and Watch state protect the gate. They are
not what the user asked for. The report states what changed and why it matters,
what risk remains, what happens next, and whether the PR can merge. Identifiers
appear only when they explain a blocker or the user asks for evidence.

## Quick replies need a bound target

Two reports can both offer a Merge button. A label-only click cannot tell them
apart: Workbench source metadata may recover the clicked report, but the label
and nearby prose cannot. So each offer records its PR and head, and a click
re-checks live state before acting. A button block may also be stripped on a
destination that cannot render buttons, which is why the same next step is
always stated in prose.

## Why the regression command looks the way it does

`references/local-regression.md` uses explicit switches, each guarding a
different boundary:

- `--target master` selects the environment, not the checkout that sends the
  source.
- Git ignores do not govern rsync. Ignored local files can be copied even when
  Git status and the deployment receipt say the checkout is clean.
- A normal rsync size and mtime check can keep different bytes on the receiver.
  A commit receipt then identifies the sender, not every byte in service.
- `--reset-mode none` preserves product state, but automatic `.env.regression`
  discovery can still rewrite the runtime environment. An empty regular env file
  takes the same rewrite path; `--env-file /dev/null` does not.

None of this grants permission to provision environments, change credentials or
operate anything other than the local regression environment.
