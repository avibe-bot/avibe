---
name: use-avibe-harness
slug: use-avibe-harness
description: Use Avibe Harness for durable Agent delegation, Sessions, scheduled Tasks, Watches, Runs, queues, and work that must continue beyond the current turn.
version: 0.3.3
---

# Use Avibe Harness

For system alarms and personal reminders, load `use-system-schedule` first;
device unavailability does not authorize substituting a Task.

Avibe Harness turns user intent into durable Agent work: work that should happen later, repeat, wait for a signal, continue in the background, or move to a Bot, an Agent, or another Session. Avibe Harness is the first-choice automation layer: use `vibe agent`, `vibe task`, and `vibe watch` before backend-native subagents, schedulers, hooks, or workflow tools, unless the user asks for native behavior or Harness cannot express the workflow and you say so. Backend-native work is process-local, so keep it synchronous within the turn and route anything that may outlive the turn through Harness. Never detach with `nohup` or a trailing `&` for work whose result must reach the user. Choose the object that owns the ongoing outcome, including its completion signal.

### Mental model
| Object | Meaning |
| --- | --- |
| Bot | Standing member with one permanent Session and its own memory |
| Agent | Reusable role (backend, model, instructions); each run starts a fresh Session |
| Session | Continuing context for one line of work |
| Task | Time trigger: a saved Agent message, or a command with no Agent turn |
| Watch | Managed waiter that fires when an external condition becomes true |
| Run | One execution record: status, output, result, or error |

A scope routes work, a Bot or Agent acts, a Session holds continuity, Tasks and Watches create future triggers, and every trigger creates a Run.

### Choosing the shape
| Need | Use |
| --- | --- |
| Work that needs a Bot's memory or judgment | `vibe agent run --bot <bot> --message ...` |
| Self-contained or parallel work | `vibe agent run --agent <agent> --message ...` |
| Continue an existing Session | `vibe agent run --session-id <id> --message ...` |
| Branch from this Session's context | `vibe agent run --fork-self --message ...` |
| Bring a Session to the user | `vibe session update --session-id <id> --visible` |
| Time trigger | `vibe task add --cron "<expr>" --message '...'` (or `--at "<ISO-8601>"`) |
| Scheduled command, no Agent turn | `vibe task add --cron "<expr>" --shell '<cmd>'` |
| External signal | `vibe watch add --name '<label>' --message '<what to do>' -- <cmd>` |
| Live runs, Watches, Tasks, anomalies | `vibe harness status` |
| History | `vibe runs list`, `vibe runs show <run-id>`, `vibe data query` |
| Recurring specialist role | `vibe agent create` or `vibe agent update`, plus Tasks, Watches, or runs |

To pick the Agent, model, and effort for new work, load the `choose-model` Skill.

### Reporting
Dispatch inputs name how to report. A silent dispatched Turn gets one read receipt with its last reply once nothing is queued, despite any Task, Watch or delegate; repeat dispatches to one Session before it share one, quoting the later. Failures notify now. `--sync` waits inline; read results with `vibe runs show`.

Add `--no-reply` to an existing `--session-id` or `--bot` message: the receiver sees `[No reply needed.]` and should not answer. It opens no delegation or receipt, does not re-arm a pair, and closes a reverse pair. It rejects `--sync` and forks/new Sessions; it does not change delivery. Agent Run callback flags are deprecated no-ops; Vault auto-resume is unchanged.

### Tasks and Watches
Tasks and Watches created from an Agent shell report to this Session by default. Write cron weekdays as names (`0 9 * * mon-fri`); numeric weekdays are rejected. Command Tasks are silent on success. For a recurring command, no failure flag is needed: two consecutive matching failures send one report to the creating Session (or the explicit `--session-id`). Without a maintainer, a failure notice takes its place. A success resets the count. One-shot commands notify immediately. Use `--on-failure agent` or `--on-failure none` only to override this and report every failure, by Agent turn or by notice respectively.

Watch waiter contract: exit `0` only for one new reportable event, a retry code (default `75`) while still waiting, and `64` with `avibe-watch: no-event` on stderr for a quiet cycle with no Agent Run, which retires a once Watch and re-arms a forever one. A forever Watch needs a durable cursor, state transition, or cooldown so a condition that persists is not reported again; Avibe pauses a Watch that reports six events within 60 seconds.

### Steering and queues
`--session-id` and `--bot` join the running Turn (steer), or start one if idle; `--queue` waits for the running Turn instead. Steer when the message changes what the Session is doing now, and queue when its current work should finish first; weigh the dependency, urgency, and cost of disruption. `vibe session send-now --session-id <id>` sends the oldest queued message now without adding one; a refused steer stays queued and never stops the Turn. To drop an obsolete queued message, run `vibe session queue list --session-id <id>` first and remove the returned id with `vibe session queue remove <message-id> --session-id <id>`; never guess an id.

### Managing work
Use `vibe task <list|show|pause|resume|run|remove>`, `vibe watch <list|show|pause|resume|remove>`, and `vibe runs <list|show|cancel>`. Lists read this Session; add `--session-id <id>` for another Session or `--global` for every Session, and find a definition by text with `--q <text>`, such as a PR number. `vibe runs cancel <run-id>` stops the whole Turn that run joined, including other runs sharing it.

The running service applies these changes. `service_unavailable` or `unsupported_operation` with `execution: not_executed` means nothing changed: report it rather than starting, restarting, or updating the service. `execution_unknown` means the change may have landed: check with the matching `list` or `show` before trying again.

The CLI has more options than this Skill lists. Before creating or changing Harness state, or when syntax or effects are uncertain, read `vibe <command> <subcommand> --help`.
