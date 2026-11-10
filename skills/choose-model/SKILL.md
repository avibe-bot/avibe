---
name: choose-model
slug: choose-model
description: Choose the model, reasoning effort, and speed for work you hand out or continue. Match them to the work and how fast it is needed. Keep work in the Session that holds its context when the model should change, and pass the choice explicitly.
version: 0.2.0
---

# Choose a Model

Before you dispatch or continue work, pick the model, reasoning effort, and speed that fit it. What the user has found about models comes first (section 5).

## 1. See what is configured
- `vibe agent list` shows each enabled Agent with its backend, default model, and effort.
- `vibe agent models <agent>` lists the models and efforts that Agent can use.
- `vibe session get --session-id <id>` shows a Session's current model, effort, and speed.

Choose from these lists. A Vibey or OpenCode Agent runs every family its list shows. A Claude Code Agent runs only Claude, and a Codex Agent only GPT. If the family that fits is not configured, use the closest one and tell the user when it matters.

## 2. Match the work to a family
Judge by family, not version; a family keeps its character across releases. These profiles come from the Avibe team's daily use.

- **Claude**: product thinking, frontend and UI, prompts and copy, anything the user reads or touches. Less rigorous than GPT when every case must be checked.
- **GPT**: stiff on product, UI, and prose, but the most rigorous: backend logic, security, step-by-step work that must not skip a case.
- **Other families** (Grok, Gemini, open models): no recorded experience yet. Try one on a bounded task next to a choice you know before relying on it.

When a job mixes both kinds, split it: the user-facing part to Claude, the rigorous backend part to GPT. Within a family, the larger tier suits ambiguous or high-stakes work; the smaller, faster tier suits well-specified, mechanical, or high-volume work.

## 3. Set effort and speed for the work
Effort is how long the model thinks before it acts. Set it by how hard the work is to get right:

- **low**: lookups, status checks, renames, reformatting, and summaries of a known source. The answer is obvious once it is found.
- **medium or high**: ordinary implementation against a clear spec.
- **high or xhigh**: design, debugging an unknown cause, review, and security.
- **max**: only for the hardest problems where a wrong answer is expensive. It is slow.

Then weigh urgency. When the user is waiting in the conversation, or the work is a quick check, take a faster tier and lower effort; a good answer now beats a perfect one in ten minutes. For background or long work where quality matters more than time, spend more. Cost rises with tier and effort, the same way.

For example: "is PR 3442 merged?" gets a small tier on low. A failing test with a clear stack trace gets medium. "Why do replies sometimes go missing?" gets a large tier on xhigh. An overnight security review of the Vault boundary gets GPT's large tier on xhigh or max.

Higher effort does not rescue an unclear brief. A clearer brief usually helps more.

Fast mode runs the same model on its vendor's faster tier, at a higher price per token. It suits a Session the user is actively waiting on, not background or bulk work. A Session started with `vibe agent run` runs at the standard tier; the user turns Fast on or off in the Web model menu.

## 4. Pass the choice, and keep work where its context is
For a new Session, name both, so the choice is deliberate and visible in the run record:

`vibe agent run --agent <agent> --model <model> --reasoning-effort <effort> --message ...`

When work already under way should move to another model, effort, or speed, the Session that holds its context is usually the right place to continue. The model menu there switches it from the next Turn. A command for Agents to do this is coming. Until it lands, ask the user to switch the Session in the Web model menu, then continue there. If a Turn is running, send the next message with `--queue` so it starts on the new model after that Turn, or stop the Turn first with `vibe runs cancel <run-id>`. Switching works in both directions. After a hard design phase, the mechanical follow-up can drop to lower effort.

Start a new Session instead when:
- the backend must change: a Claude Code Session cannot run GPT, and a Codex Session cannot run Claude;
- the context is very long and mostly spent. The first Turn on a new model re-reads all of it without the prompt cache, and a fresh brief can be cheaper;
- the work is separate anyway;
- the user is not there to switch it and the work cannot wait.

To keep the context and leave the original as it is, fork with `vibe agent run --fork-session <id> --model <model> --reasoning-effort <effort> --message ...`.

A Bot runs on the model the user chose for it, and `--model` is refused with `--bot`.

## 5. The user's findings come first
What the user has found about models overrides this Skill. That covers what they tell you now and what is recorded in your memory (a Bot's memory files, or the Project's `AGENTS.md`), so check there before you choose. When the user shares a finding, or confirms one you observed, record it there so later choices use it.
