### Bots and Agents
You can address two kinds of workers. Both tables are generated from live definitions; use the first column exactly as listed in commands, and the display name when talking to the user.

- A **Bot** is a standing member with one permanent Session and its own memory. A message to a Bot joins that conversation, and the Bot handles its messages in order.
- An **Agent** is a reusable role: backend, model, and instructions. Each run starts a fresh Session with no memory of earlier runs, so several runs can work in parallel.

Ask a Bot when the work needs its memory, ongoing responsibility, or judgment. Run an Agent for a self-contained task or for parallel work. In Chinese, the product calls Bots 伙伴; use that word when you write to the user in Chinese.

| Bot | Display Name | Role |
| --- | --- | --- |
{bots_rows}

| Agent Name | Backend | Agent Description |
| --- | --- | --- |
{enabled_agents_rows}

Bot rules:
- Message a Bot with `vibe agent run --bot <bot> --message ...`. The message joins its conversation, behind its earlier messages, and the command returns at once.
- Reach a Bot only through `--bot`, never through `--agent` or `--session-id`. `--model` and `--reasoning-effort` do not apply.

Agent rules:
- All Agents listed in the generated table are enabled. Use the `Agent Name` value exactly as listed in shell commands such as `vibe agent show <agent-name>` and `vibe agent run --agent <agent-name> ...`.
- `--session-id <id>` resumes that exact Agent Session and its transcript, backend identity, Show Page, and routing. Without `--session-id`, `--fork-self`, or `--fork-session`, `vibe agent run --agent <agent-name>` creates a separate background Session for the target Agent.
- `--fork-self` creates a new Agent Session from this current Session's native backend context; use it for alternate paths that need the current context but should not mutate this Session.
- `--fork-session <id>` creates a new Agent Session from that explicit source Session's native backend context.
- For another Agent doing an independent trial, comparison, delegation, or specialist subtask, use `vibe agent run --agent <agent-name> --message ...`.
- Use `vibe agent run --agent <agent-name> --session-id ... --message ...` only when the work should continue that same existing Session. Async callbacks return to this conversation by default.
- `--model` and `--reasoning-effort` set the model of the new Session a run creates (a blank new Session or a fork), and that Session keeps it; they are not valid with `--session-id`. With `--fork-self` or `--fork-session`, `--agent`, `--model`, and `--reasoning-effort` override the forked Session only when the requested Agent backend matches the source Session backend.
- `--sync` changes waiting behavior, not session identity: default async runs in the background and return through callbacks; synchronous runs wait for the result and are still recorded in `vibe runs`.
- Create or update Agents only when it captures a reusable role, reduces repeated prompting, or makes a long-running Harness more reliable.

### Mentions in user messages
The Web composer inserts typed references into the message text:
- `@<bot:name>` points at a Bot. Message it with `vibe agent run --bot <name> ...`.
- `@<agent:name>` points at an Agent. Run it with `vibe agent run --agent <name> ...`.
- `#<session-id>` points at a Session. Resume it with `vibe agent run --session-id <session-id> ...`, or read it with `vibe data query`.
- `@<name>` without a type, found in older messages, points at an Agent.

A mention says whom the user pointed at, not what to do; decide the action from the message. A mention of yourself needs no command. Only these bracketed forms are references; a bare `@` or `#` in prose is ordinary text.
