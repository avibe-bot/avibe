---
name: agent-prompt-audit
slug: agent-prompt-audit
description: Audit and improve the prompt surface of Avibe Agents across backends (Claude, Codex/GPT, OpenCode) — global and project rules, Agent system prompts, Skills, delegation briefs, and Task and Watch messages — using real run evidence. Use when an Agent misbehaves (stalls, over-asks, over-reaches, ignores or over-applies a rule), after a model or backend change, or when the user asks to review, clean up, or tighten prompts.
version: 0.1.0
---

# Agent Prompt Audit

An Agent's behavior is shaped by every piece of text that reaches it over its
lifecycle, not just its system prompt. This audit finds the specific
instructions that cause observed or predictable misbehavior on the backends
that actually run them, and proposes the smallest edits that fix it. The goal is
fit, not brevity: an instruction is judged by what it does to behavior, and
sometimes the fix is adding context or a missing exit condition.

Two deliverables, always: a **report** of findings with evidence and
confidence, and a **proposed diff**, one finding per hunk. Apply edits only
when the request asks for them. A clean surface is a valid result; do not
manufacture findings.

## 1. Scope and target

State these at the top of the report as assumptions instead of asking:

- **Scope**: the Agent, Skill, file, or symptom the user named; otherwise the
  whole surface that reaches the Agents in question.
- **Targets**: the backend and model each surface actually runs on
  (`vibe agent list --json`, `vibe agent show <name> --json`). Shared text
  (global rules, AGENTS.md, Skills) must work on every backend that loads it.
  Take model-specific behavior from current vendor guidance — the `claude-api`
  skill's migration notes for Claude, the `openai-docs` skill for OpenAI — not
  from memory.

## 2. Inventory the lifecycle surface

List what you found before auditing it. See
[references/surfaces.md](references/surfaces.md) for where each layer lives and
who owns it. The layers, by when they reach the Agent:

1. **Always loaded**: Avibe-injected runtime prompt, global rules, the project
   AGENTS.md/CLAUDE.md chain, the Agent's system prompt, every Skill description.
2. **On demand**: Skill bodies and references.
3. **At work time**: delegation briefs, Task and Watch messages (re-sent on
   every fire), callback and review-loop prompts, tool descriptions.

Always-loaded and recurring text has the highest leverage; audit it first.

## 3. Gather behavioral evidence

Prefer what Agents actually did over what the text seems to say. Pull recent
failed, cancelled, long-running, or user-corrected runs and read the
transcripts around the failure (`vibe runs`, `vibe data query`; recipes in
[references/surfaces.md](references/surfaces.md)). User corrections ("you
stopped", "why didn't you report", "don't ask me that") are the strongest
signal. For each symptom, find the line that produced it — or the missing line
that would have prevented it. Use `git blame` on version-controlled prompt files to
learn which incident a rule was written for, and whether that failure still
reproduces.

## 4. Classify and scan

For each instruction ask: is this **context only the author knows** (audience,
environment facts, contracts, authority, reasons — keep), or a **constraint on
behavior** (test whether it still earns its place)? Then scan against
[references/patterns.md](references/patterns.md): dated prompt text,
lifecycle-control gaps, layering defects, and cross-backend hazards. A finding
must name its pattern and a reason grounded in evidence or documented model
behavior; otherwise it is a low-confidence flag or nothing.

The keep list in that file is as binding as the patterns.

## 5. Report and diff

Per finding: location (`file:line`, Agent name, or Task/Watch id), quoted
evidence, pattern, why it misbehaves on which target, confidence (**high**:
reproduced in transcripts or documented; **medium**: consistent known behavior;
**low**: heuristic — flag only), and action (`remove` / `rewrite` with the
replacement / `move` with destination / `add` / `flag`). Order by impact.
Open with counts and the two or three findings that matter most, in prose.

In the diff, rewrite rather than delete when the concern is live, and complete
each removal: references, duplicates in other layers, tests asserting the old
text, and mirrors.

## 6. Apply through the owning channel

When asked to apply, back up first and edit where the text is owned:
the source of a generated or imported file rather than its output (then
regenerate it the way the source documents), personal files directly, Agent
prompts through `vibe agent update --system-prompt-file`, Tasks and Watches
through `vibe task update` and `vibe watch update`, repository files through the
repository's own delivery process as its AGENTS.md defines it, and
Avibe-injected prompts only as a proposal to the Avibe repository.

## 7. Verify

A removal is a hypothesis. For contested changes, probe behavior before and
after on each target backend with a scratch run that exercises the
instruction's purpose (`vibe agent run --agent <name> --sync --message ...`),
and read the transcript rather than asking the model whether it needs the
rule. Change one thing at a time where stakes are high. If a cut regresses,
re-add it in minimal form. Re-audit after any model or backend change.
