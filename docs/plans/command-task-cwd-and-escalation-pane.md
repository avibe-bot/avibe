# Command Tasks: an Addressable Working Directory, and an Honest Escalation Pane

Follow-up to `scheduled-command-task.md` (shipped in cde33c9d). Two defects found
while converting a real daily job — a `sub2api` re-auth reminder — from an Agent
message task to a command task. Both are small, both are independent, and both
come from the same root: a command task reuses a data model whose fields were
written to answer *Agent* questions, and two of those fields are now being asked
a *command* question they were never labelled for.

---

## Issue 1 — `--cwd` is refused for exactly the command tasks that most need it

### What happens

```
$ vibe task add --name sub2api-reauth-remind \
    --cron '0 11 * * *' --timezone Asia/Shanghai \
    --cwd /essd/qiqi/code/fish/f2debug \
    --shell '...' --on-failure agent --message '...'

{"ok": false, "code": "cwd_with_existing_session",
 "error": "--cwd only applies when this definition creates new Sessions",
 "hint": "An existing target Session keeps its own working directory."}
```

Drop `--cwd` and the task is created. At fire time the command runs from
`/home/qiqi/code/cc/slack` — the bound Session's workdir, an unrelated chat
router directory — with no field anywhere on the definition recording that.

### Why

`_resolve_definition_session_cwd` (`vibe/cli.py:5081-5109`) answers **one**
question — where a Session this definition creates should run — and it is the
only thing consulted for `--cwd`. Its `existing` branch refuses (`cli.py:5060-5068`),
on a rule that is correct on its own terms: an existing Session owns its
directory, and a definition pointing at one has no business rewriting it.

The rule predates command tasks. A command task binds to an existing Session
for a reason unrelated to where it runs: `--on-failure agent` needs somewhere to
escalate. `_resolve_session_policy` returns `existing`, the branch fires, and the
command's cwd is refused as collateral.

`_command_definition_spawn_cwd` (`cli.py:5111-5137`) is the function that
converts the Session answer into the command answer. It already documents the
split — *"`_resolve_definition_session_cwd` answers a Session question… A command
cannot wait for that"* — and already special-cases `create_per_run` by falling
back to the invocation directory. `existing` is the one policy it deliberately
passes through untouched, because a bound definition is supposed to read its
directory live from the Session.

That live read is `_bound_session_workdir` (`core/scheduled_tasks.py:6609-6650`),
and its docstring states this issue as a known consequence:

> `--cwd` is REFUSED for a definition bound to an existing Session
> (`cwd_with_existing_session`), on the rule that the Session owns its working
> directory — so an escalating command task legitimately stores `cwd=None`. A
> message task loses nothing to that: the Agent turn starts in the Session's
> workdir. A command has no turn to inherit it from, so `None` fell through to
> the `~/.avibe` fallback and `--shell './scripts/sync.sh'` — the form the docs
> use — ran from the product state directory, with **the one flag that could
> have said otherwise rejected by the Session rule**.

That function is the mitigation, not the design. It stopped commands landing in
`~/.avibe`; it did not give the user a way to say where the command runs.

### Why the inherited directory is the wrong answer

Not merely unstated — unstable. `_bound_session_workdir` reads the Session row
**live at fire time**, by design, so a command follows a Session whose workdir
later changed. For a message task that is right: the turn belongs to that
conversation. For a command it means `vibe session update --cwd` on an unrelated
conversation silently relocates a cron job, with no edit to the task and nothing
in its history showing a change.

The failure mode is quiet. Every path in the fallback chain is validated with
`isdir`, so a relocated command does not error — it runs, from somewhere else.
A script robust to its cwd (ours resolves everything off
`Path(__file__).resolve().parent`) never notices. A script using a relative path
breaks, or writes into the wrong tree.

### Proposal

Teach the refusal the difference between the two questions.

1. `_resolve_definition_session_cwd` grows a `has_command: bool = False`
   parameter. In the `existing` branch, refuse only when `not has_command`.
   The message stays exactly as it is for message tasks.
2. When `has_command` and the policy is `existing`, resolve `--cwd` through the
   existing `_resolve_existing_cwd` (giving the same `cwd_not_found` check every
   other policy gets), and return it as the **command's** cwd:
   `session_workdir` stays `None`, `task.cwd` gets the resolved path.
3. `_command_definition_spawn_cwd` needs no change. `task.cwd` is already first
   in the fire-time chain (`core/scheduled_tasks.py:6696-6699`), so a stored
   value wins over the bound Session automatically, and omitting `--cwd` keeps
   today's inherit-from-Session behaviour unchanged.

Call sites: `vibe task add` (`cli.py:3297`) and `vibe task update`
(`cli.py:3944`) pass `has_command`. `vibe task update` already threads
`task.has_command` into `_command_definition_spawn_cwd` at `cli.py:3968`, so the
value is in hand at both.

**Not** `_resolve_run_cwd` (`cli.py:4970-4979`). `vibe agent run` has no command
lane; its identical refusal remains correct.

### Also: the pane never shows where a command runs

`WatchDetail` renders `harness.detail.cwd` ("Working directory",
`ui/src/components/workbench/HarnessPage.tsx:1474`). `TaskDetail` renders no cwd
field at all, so the directory a command runs in is invisible in the UI even
when explicitly stored. Add the same field to `TaskDetail`, gated on
`isCommand`. Where `task.cwd` is null, showing the em-dash placeholder is
honest — the answer genuinely is not on the definition — but the better display
names the inherited source ("Session workdir") rather than implying none exists.

### Docs

`--cwd`'s help text reads as Session-only ("Working directory for Sessions
created by this task"). It becomes two sentences: the Session meaning for
message tasks, the spawn-directory meaning for command tasks. The "Command
tasks" help section gains a line saying the command runs from `--cwd`, else the
bound Session's directory, else the runtime default.

### Tests

| Test | Change |
| --- | --- |
| `tests/test_cli_task_command.py:1702` | Asserts today's refusal. Re-point at a **message** task (still refused); add a command-task case that now succeeds and stores `cwd`. |
| `tests/test_scheduled_tasks.py:14796` | Same split. |
| `tests/test_cli_agent_run_schema.py:1801` | Unchanged — `vibe agent run` keeps the refusal. |
| new | `--cwd` on an escalating command task stores it on the definition and beats the bound Session's workdir at fire time. |
| new | No `--cwd` still inherits live from the bound Session (guards `_bound_session_workdir`). |
| new | `--cwd /nonexistent` on a command task raises `cwd_not_found`, not `cwd_with_existing_session`. |
| new (ui) | `TaskDetail` shows the working directory for a command task. |

### Risk

Low, and one-directional: this accepts input that is currently an error. No
stored definition changes meaning — `cwd=None` still resolves through the same
chain. The only behaviour change is for a task created *after* the fix *with*
`--cwd`, which stops following its Session. That is the point, and it is opt-in
per task.

Scenario catalogue: this needs an SCT entry of its own. `scheduled-command-task.md:157`
records the refusal as settled design ("the Session owns its directory"); that
line should be amended to point here rather than left contradicting the new
behaviour.

---

## Issue 2 — the escalation lane is rendered as if it were the job

### What happens

A command task with `--on-failure agent` shows, top to bottom:

```
COMMAND        vibe vault run --env … -- …/remind.py
SCHEDULE       every day at 11:00
NEXT RUN       today 11:00
AGENT          claude · claude-opus-5 · effort high        ← failure-only
SESSION        DevBot                        TELEGRAM ↗    ← failure-only
SESSION MODE   Reuse the same session   DELIVERY  Default  ← failure-only
MESSAGE        The sub2api re-auth reminder command failed…← failure-only
ON FAILURE     Escalate to an Agent    TIMEOUT   300s      ← the qualifier
LAST RUN       2026-08-04 10:37:04 GMT+8
```

Five consecutive fields describing a path that runs on no healthy day are
rendered at the same visual weight as `COMMAND` and `SCHEDULE`, and the field
that explains them arrives *after* all five. A reader scanning top-down
concludes an Agent runs daily on Opus with high effort — the exact cost the task
was rewritten to avoid. `MESSAGE` is the sharpest case: on a message task that
label means the payload sent every run; here the identical label means a triage
prompt sent almost never.

### Why the fields are shown at all — and why that part is right

`routesSomewhere` (`ui/src/components/workbench/HarnessPage.tsx:1223-1230`) gates
the block on real bindings: `on_failure === 'agent'`, or a pinned agent, session,
or delivery target. That gate is SCT-043, which fixed the inverse bug — three
label helpers resolve a stored `null` to a confident answer ("Inherits default
agent", "Reuse the same session", "Default · session location"), so a pure
`--on-failure none` command used to advertise routing it deliberately had none
of. Its rule, *"gated on the BINDINGS, not on the kind"*, is correct and stays:
an escalation turn really is routed by exactly these fields.

SCT-043 settled **whether** to show them. It never addressed **what they mean
once shown**, and for a command task the answer is different from a message
task's.

### Proposal

No change to `routesSomewhere`. Change order and labelling only.

1. **Move `ON FAILURE` above the routing block** when `isCommand`. The
   conditional should read before the fields it governs. `TIMEOUT` leaves that
   grid and pairs with the new working-directory field instead — both are
   process mechanics belonging with `COMMAND`, and pairing it with `ON FAILURE`
   is what currently drags the qualifier down below the routing.
2. **Group the five under a heading** — "On failure → escalation" — for command
   tasks with `on_failure === 'agent'`. Message tasks render exactly as today.
3. **Relabel `MESSAGE` to "Triage prompt"** (new key `harness.detail.triagePrompt`)
   when the message exists only as escalation instructions. The distinction is
   already load-bearing in the CLI, where `--message` means the payload for a
   message task and the failure prompt for a command task.
4. Consider a muted note on `AGENT` for this case — the pane states model and
   effort with no indication of how rarely they apply.

### Tests

`ui/src/components/workbench/HarnessPage.test.tsx:470-540` — `TaskDetail command
task` already covers this area, including SCT-043 ("drops the Agent routing a
pure command task deliberately has none of") and its counterpart ("keeps the
routing fields wherever there is something to route"). Both assert presence and
absence, not order, so both survive unchanged. Add:

- an escalating command task renders `ON FAILURE` before `AGENT` in document order;
- its message renders under the triage-prompt label, and a message task's under
  the plain `MESSAGE` label.

New scenario entry, sibling to SCT-043: *the task pane presents the escalation
lane as the job it only runs instead of*.

### Risk

Presentation only; no gating, storage, or dispatch change. The one judgement
call is heading wording, which is i18n-visible and needs the same key added to
every locale in `ui/src/i18n/`.

---

## Sequencing

Independent — either can land alone. Issue 1 touches CLI + core + docs + tests;
Issue 2 touches UI + i18n + tests. Suggested order is Issue 1 first, so
`TaskDetail`'s new working-directory field arrives with something real to
display and both panes get their layout reviewed once.

---

## Status

Both implemented on this branch. Shipped shape differs from the sketch above in
two places, neither material:

- `TIMEOUT` moved *up* beside the new working-directory field rather than staying
  at the bottom — both are command mechanics, and the pane reads better with the
  process facts together.
- `_command_definition_spawn_cwd` also grew `stored_cwd`. Without it, letting a
  command task store a `cwd` created a new bug rather than fixing one: the update
  path writes the Session answer with `update_cwd=True` on every edit, so
  `vibe task update --name` would have erased the pin. Covered by SCT-051.

Scenario catalogue: SCT-050, SCT-051, SCT-052.

### Review pass

Three P2 findings, all taken. Each is the same shape as the bugs above — a field
whose second meaning was not carried all the way through.

- **A policy change promoted the command's directory onto a new Session.**
  Retargeting at `--create-session*` without `--cwd` carries the stored directory
  forward from `task.cwd`, which is correct while `cwd` has one meaning and wrong
  the moment Issue 1 let it have two. The Session half now reads
  `metadata["session_workdir"]` for a command task (`_stored_session_workdir`),
  which a bound definition never had and a per-run definition leaves unset on
  purpose (SCT-047), while the command half survives on `stored_cwd`. That also
  stops a policy change re-stamping the directory the *update* ran from —
  SCT-051's rule in the lane the explicit flag does not cover. **SCT-053.**
- **The escalation heading was keyed on the kind.** A command task with
  `on_failure: none` and a real delivery target keeps the routing fields
  legitimately (that is SCT-043's whole rule), so the heading made the pane say
  "Notice only (no Agent)" and "escalation" about one task, one field apart.
  Keyed on `taskOnFailure(task) === 'agent'` — the same value the field above it
  prints. **SCT-054.**
- **"Session working directory" named an outcome the pane cannot verify.**
  `_bound_session_workdir` answers `None` for a deleted row, a NULL workdir or a
  failed read, and every fallback is `isdir`-validated, so the command can land on
  the runtime default while the pane points at a Session — beside the "Session
  deleted" the Session field prints two rows up. The field names the *chain* the
  fire walks and drops the Session term where the binding is visibly gone.
  Resolving it exactly would need the backend to publish the source; the pane
  stops making a claim it cannot support instead. **SCT-055.**

### Second review pass

Four findings, all taken. Two of them are the first pass's own fix landing one
line short.

- **The Session reservation still read the command's answer.**
  `_stored_session_workdir` split the two halves on the read side, and then
  `_reserve_definition_session(..., workdir=cwd, ...)` was handed `cwd` — the
  variable `_command_definition_spawn_cwd` had just overwritten with the
  command's. So `--create-session` on a pinned command task reserved its
  replacement escalation Session in the build directory, which is exactly the
  defect SCT-053 closed, reintroduced through the same field one line later. Both
  call sites (add and update) now pass `session_workdir`. **SCT-056.**
- **`--cwd` was refused on the very tasks Issue 1 was for.**
  `_reject_inert_create_once_cwd_update` is a Session rule — a reserved reusable
  Session owns its workdir, so the flag is inert — but it ran before the
  command-aware resolution, so it reached a command task first. Repointing a
  nightly build meant `--create-session`, discarding an escalation Session that
  had nothing to do with the request. Softened for commands exactly as Issue 1
  softened the `existing` refusal. The same branch (`command_only_cwd`) also stops
  the `else: cwd = task.cwd` fall-through writing a command directory into
  `metadata["session_workdir"]` on an unrelated edit. **SCT-057.**
- **The `--help` epilog described a chain the code does not walk.** It promised
  "else the runtime default", but `cmd_task_add`'s pure-command branch stores
  `os.getcwd()` and `_command_definition_spawn_cwd` returns it for an unpinned
  `create_per_run` — SCT-047's rule, which the epilog was written before. Reworded
  to say what is stored.
- **Scenario IDs were missing from the tests and the PR body.** AGENTS.md:250
  asks for them in both. Added for SCT-050 – SCT-057.

### Third review pass

Three findings, all taken. The first is the same conflation again, in the last
lane that still read `task.cwd` for the Session's answer.

- **An edit that asks nothing about directories still placed a Session.**
  `command_only_cwd` covers the lanes where the *flag* arrives; the plain
  `else: cwd = task.cwd` fall-through was left, and for a command task that value
  is the command's half. So `vibe task update --name` on a `create_per_run`
  definition wrote it into `metadata["session_workdir"]` and pinned every future
  per-run Session to the directory `task add` was typed in — undoing SCT-047's
  deliberate blank — and a `create_once` definition that had not reserved yet
  reserved there. Each half now carries forward from where that half is stored.
  **SCT-058.**
- **The runtime half of SCT-050 carried no scenario ID.**
  `test_a_stored_cwd_beats_the_bound_sessions_workdir` is the assertion that the
  stored `cwd` actually wins at fire time; ID added, and the catalogue entry now
  names it alongside the CLI test.
- **`--cwd` was undocumented in the user references.** `docs/CLI.md` and
  `docs/CLI_ZH.md` list the command-task flags and explain what `vibe task update`
  may change; both now include `--cwd`, with a paragraph on what it means on a
  command task versus a message task. (`docs/COMMANDS*.md` covers IM slash
  commands, not the `vibe` CLI, so nothing there applies.)

### Fourth review pass

Two findings, both taken, both about a claim rather than the code.

- **The new `docs/CLI.md` paragraph overstated the split.** "On a command task
  `--cwd` means only where the command runs" is true for a binding — existing, or
  a reusable Session already reserved — and false for `--create-session*`, where
  `_resolve_definition_session_cwd` folds the flag into `session_workdir` on
  purpose: a Session created by this edit and its command run in the same place.
  Documented as the two cases it is, EN and ZH, with the way to opt out (pass a
  scope, omit `--cwd`).
- **"Runtime default" named a config key the run may never reach.** Past
  `_runtime_default_workdir()` — unset on a fresh install, and `isdir`-revalidated
  on every fire — `_execute_command_task` falls through to the Avibe state
  directory. Same overclaim SCT-055 fixed, one link further down. Both strings now
  name the terminal fallback, asserted on the `en` and `zh` copy directly, since
  the copy is where the claim lives. Folded into **SCT-055** rather than given its
  own ID: same invariant, same field.

Scenario catalogue: SCT-050 – SCT-058.
