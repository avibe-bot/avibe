---
name: agent-prompt-audit
slug: agent-prompt-audit
description: Audit and improve the prompt surface of Avibe Agents across backends (Claude, Codex/GPT, OpenCode) — global and project rules, Agent system prompts, Skills, delegation briefs, and Task and Watch messages — using real run evidence. Use when an Agent misbehaves (stalls, over-asks, over-reaches, ignores or over-applies a rule), after a model or backend change, or when the user asks to review, clean up, or tighten prompts.
version: 0.1.0
---

# Agent Prompt Audit

An Agent's behavior comes from every piece of text that reaches it over its
lifecycle, not just its system prompt. The audit's job is to find which text
causes the behavior the user sees, on the backend and model that actually ran
it, and to propose the smallest change that fixes it. Judge each instruction
by what it does to behavior, not by its length: sometimes the fix is adding a
missing reason or exit, and a clean surface is a valid result.

Deliver a report of findings — each with its evidence, confidence, and a
concrete proposed change — and apply changes only when asked.

## Core ideas

**Evidence over reading.** What Agents actually did beats what the text seems
to say. Start from real runs and user corrections ("you stopped", "why didn't
you report", "don't ask me that"), trace each symptom to the line that caused
it or the missing line that would have prevented it, and use `git blame` to
learn what incident a rule was written for and whether it still happens.
A finding without evidence or documented model behavior is a flag, not a fix.

**Context is kept; constraints must earn their place.** Facts only the author
knows — environment, contracts, ownership, quality bar, the reason behind a
rule — are what prompts are for. Behavioral constraints are what go stale.
Keep exact scripts where one sequence is safe (destructive commands, auth,
merge gates, key custody), prohibitions against failures that still reproduce,
and the scope bounds that make autonomy safe.

**Say intent and reason, not pressure or method.** Caps, `MUST/NEVER`, and
emphasis without a reason make current models rigid; step scripts for judgment
work and strategy coaching usually do worse than the model's own plan; fixed
formats, word caps, and "don't narrate" rules produce silence or starved
answers. Fossils — named-model workarounds, incident numbers as authority,
"now/no longer" phrasing, one session's stumble made permanent — should become
the current rule they stand for.

**Every stop needs an exit.** Agents run across turns, wake on callbacks, and
hand work to each other, so the costliest defects are lifecycle gaps: a
"stop/wait" with no statement of what the turn produces instead, asking
permission for reversible in-scope steps, continuing without bounds after
repeated failure, waiting with no durable waiter or expiry meaning, briefs
missing the goal or report target, callbacks that say "done" without the
result, and Task/Watch messages that restate rules on every fire.

**One home per rule, at the layer whose timing fits.** Always-loaded and
recurring text has the most leverage and deserves the most scrutiny.
Duplicates that disagree force the Agent to guess; keep the mechanism in one
place and a principle or pointer elsewhere. Agreeing fallbacks are fine. Long
procedures belong in on-demand Skills, not always-loaded rules.

**Shared text runs on every backend.** GPT/Codex tend to follow a bare
prohibition or stop literally, so they need scope and exit conditions; strong
Claude models tend to over-reach, so they need scope bounds and a definition
of done; tool names and native mechanics dangle on other backends. Take
model-specific behavior from the vendor's current docs, and lower confidence
when you cannot reach them.

**A removal is a hypothesis.** For contested changes, compare behavior before
and after with a scratch run on the target that produced the failure, and read
the transcript rather than asking the model whether it needs the rule.

## Where the surface lives

Verify against the current machine; these are starting points.

| Layer | Where | How it changes |
| --- | --- | --- |
| Avibe runtime prompt | Avibe repo `core/prompts/*.md` | Proposal to the Avibe repository |
| Global rules | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, … | Edit the source if the file is generated or imports others |
| Project rules | nearest `AGENTS.md` / `CLAUDE.md` chain | The repository's own delivery process |
| Agent system prompt, model, effort | `vibe agent show <name> --json` | `vibe agent update <name> --system-prompt-file <file>` |
| Skills | user skill dirs (follow symlinks), Avibe `skills/`, project `.agents/skills/` | The directory's owner |
| Task and Watch messages (re-sent every fire) | `vibe task list`, `vibe watch list` | `vibe task update`, `vibe watch update` |
| Delegation briefs and callbacks | `agent_runs.message` / `result_text` | The prompt or Skill that writes them |

Only Skill descriptions on the injected catalog page (`vibe skill list`,
page 1) are loaded every turn; later pages, `disable-model-invocation` Skills,
Skill bodies, and references load on demand.

Resolve the actual target from the run or session record, not the Agent's
current definition, which may have changed since. For evidence, `vibe runs`
and read-only `vibe data query` over `agent_sessions`, `agent_runs`, and
`messages`, scoped to the sessions in question. Agent-bearing run types are
`agent_run`, `scheduled`, `watch`, `webhook`, `hook`, and `task_escalation`;
the rest are command executions, and an empty result means silence only on a
finished run. Quote the minimum excerpt and redact secrets and unrelated
private content. For a before/after probe on the historical target, fork the
affected session and pin its model:
`vibe agent run --fork-session <session> --model <model> --reasoning-effort <effort> --sync --message ...`.
