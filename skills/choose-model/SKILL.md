---
name: choose-model
slug: choose-model
description: Choose the Agent, model, and reasoning effort for work you hand out with `vibe agent run`: see what is configured, match the job to a model family, and pass the choice explicitly.
version: 0.1.0
---

# Choose a Model

Pick the Agent, model, and reasoning effort that fit the work before you dispatch it. The user's recorded findings override this Skill (section 4).

## 1. See what is configured
- `vibe agent list` shows each enabled Agent with its backend, default model, and effort.
- `vibe agent models <agent>` lists the models and efforts that Agent can use; `vibe agent models --backend <backend>` reads a backend directly.

Choose only from these lists. A Vibey or OpenCode Agent can run every family its list shows; a Claude Code Agent runs Claude and a Codex Agent runs GPT. If the family that fits is not configured, use the closest one available and tell the user when it matters.

## 2. Match the work to a model family
Judge by family, not version: a family keeps its character across releases. These profiles come from the Avibe team's daily use.

- **Claude**: the choice for product thinking, frontend and UI, prompts and copy, and anything the user reads or touches. Less rigorous than GPT when every case must be checked.
- **GPT**: rigid on product, UI, and prose, but the most rigorous: pure backend logic, security, and step-by-step work that must not skip a case.
- **Other families** (for example Grok, Gemini, or open models through OpenCode): no recorded experience yet. Try one on a bounded task beside a known choice before relying on it.

When a job mixes both kinds, split it: the user-facing part to Claude and the rigorous backend part to GPT.

Cost: use a family's larger tier for ambiguous, high-stakes, or long work, and a smaller, faster tier for mechanical, well-specified, or high-volume work such as lookups, summaries, and bulk edits. Effort: raise it for design, debugging, review, and security; lower it for mechanical steps. Higher effort costs time and tokens and does not rescue an unclear brief; a clearer brief usually helps more.

## 3. Pass the choice explicitly
Name the model and effort on the run instead of relying on the Agent's defaults, so the choice is deliberate and visible in the run record:

`vibe agent run --agent <agent> --model <model> --reasoning-effort <effort> --message ...`

The new Session keeps that model for its later turns. The flags apply only when a run creates a Session, either a new one or a fork on the same backend; they are refused with `--session-id`, and a Bot always uses its own model. To change models, start a new Session or fork.

## 4. The user's findings come first
What the user has found about models, whether they tell you now or it is recorded in your memory (a Bot's memory files, or the Project's `AGENTS.md`), overrides this Skill, so check there before choosing. When the user shares a finding, or confirms one you observed, record it there so later choices use it.
