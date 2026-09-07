# Companion Principles and Skill-Scoped Show History

## Goal

Keep Avibe's resident behavior guidance appropriate for a general AI companion,
not just an execution agent. The owner-approved six principles remain one static,
unconditional, source-addressable block. The English translation lives in
`core/prompts/agent-working-principles.md`.

Initiative includes understanding the person and following through on entrusted
work. Ordinary necessary steps do not require repeated permission; initiative
does not expand the mandate. First principles, Occam's razor, ongoing
responsibility, and honest reporting remain part of the guidance. No new
interaction classifier, authorization switch, or per-turn prompt branch is added.

## Show History Ownership

The resident Show Pages block routes creation, editing, and restoration to
`skills/use-show-pages/SKILL.md`. That Skill owns page history guidance, including
the distinction between Avibe-managed Git and a user's repository with separate
Avibe shadow history. These local rules do not govern unrelated repositories.
They protect Avibe checkpoints without prohibiting separately entrusted work in
the user's repository.

`vibe show status --json` exposes the current page's history facts on demand:

| Field | Meaning |
| --- | --- |
| `path` | The existing Show Page workspace, as before |
| `history.mode` | `managed` or `self-managed`, from the existing ownership probe |
| `history.checkpointing_active` | Whether the running checkpoint service is active |
| `history.git_dir` | The Avibe history directory, never the user's Git directory |

Status does not initialize or repair Git. The reported directory may not exist
before the first checkpoint. The Skill checks the resolved Git directory before
using workspace-relative Git, so an enclosing user repository cannot stand in
for an uninitialized Show repository. Inactive checkpointing does not imply that
old history is gone or that new edits have been saved.

The old history prompt sources, registry entries, and rendering helpers are
removed rather than kept as a second instruction source. Automatic checkpoint
creation, recovery mechanics, and repository ownership detection are unchanged.

## Acceptance

- Production and debug export emit the same ordered, source-addressable blocks
  for every supported backend. Principles remain unconditional and occur once.
- Show history state is not a prompt-rendering dependency. Changing checkpoint
  availability or repository ownership cannot change prompt bytes or revisions.
- Every unaffected prompt block retains its bytes and relative order against
  baseline commit `627ed97aebe26f4cc01cca3ffe159433d721ebdf`.
- Studio exports history instructions in the Show Pages Skill, not Runtime Core.
- Status reports ownership and checkpoint state without changing either history.
- Native Git inspection and file restoration work with the Skill's commands in
  isolated managed and user-owned repositories; checkpoints remain platform-owned.

Behavioral assessment should contrast conversation, research, delegated action,
and ongoing responsibility. Check appropriate initiative and restraint together,
not just fewer questions or more tool calls. Deterministic tests establish prompt
composition and Git mechanics, not a guarantee about every model's behavior.

This change does not deploy the running Avibe or modify Studio's saved drafts.
