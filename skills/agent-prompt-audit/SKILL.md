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

Verify against the current machine; these are starting points. Each backend
also reads its own native configuration — config directories moved by
environment variables (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, OpenCode's config
path), and native subagent definitions such as `.claude/agents/`,
`.codex/agents/`, or OpenCode agents — so resolve what the target backend
actually loads rather than assuming default paths.

| Layer | Where | How it changes |
| --- | --- | --- |
| Avibe runtime prompt | `vibe debug prompt export --format json` lists every source; `vibe debug prompt export --format json --context-file <file>` renders a composition from the inputs you supply (backend, Agent instructions, Skill directory, context), so it approximates the target only as well as those inputs match (history in the Avibe repo `core/prompts/`, if checked out) | Proposal to the Avibe repository |
| Global rules and native backend config | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, Codex `developer_instructions` in `$CODEX_HOME/config.toml` (default `~/.codex`), OpenCode `instructions` in global or project `opencode.json[c]`, … | Edit the source if the file is generated or imports others |
| Project rules | nearest `AGENTS.md` / `CLAUDE.md` chain | The repository's own delivery process |
| Agent system prompt, model, effort | `vibe agent show <name> --json` | `vibe agent update <name> --system-prompt-file <file>` |
| Skills | user skill dirs (follow symlinks), Avibe `skills/`, project `.agents/skills/` | The directory's owner |
| Task and Watch messages (re-sent every fire) | `vibe task list` / `vibe watch list` for ids, then `vibe task show <id>` / `vibe watch show <id>` for the full text | `vibe task update`, `vibe watch update` |
| Delegation briefs and callbacks | `agent_runs.message` / `result_text` | The prompt or Skill that writes them |

Only Skill descriptions on the first catalog page are loaded every turn;
later pages, `disable-model-invocation` Skills, Skill bodies, and references
load on demand. Project Skills in that catalog resolve from the target
Session's working directory, not yours, so read them from there.

## Finding evidence

Resolve the actual target (backend, model, effort) from the run record, not
the Agent's current definition. A Session's model and effort can change during
its life and ordinary IM turns have no run record, so treat the Session row as
the current setting and mark the target unconfirmed if it may have changed.
Attribute a symptom only to prompt text that existed when it ran. Each run's
`prompt` and `message` in `vibe runs show` snapshot what a Task or Watch
actually sent; file-owned text needs Git or release history matching the run;
Agent system prompts have no history. A long-lived Codex thread also keeps
earlier injected prompt snapshots in its native history, so text since removed
may still have been in view. Where the text may have changed since the run and
no history covers it, say the attribution is unconfirmed.

`vibe runs show <id>` gives one run's prompt, result, and callback state;
`vibe data query` is read-only SQLite over `agent_sessions`, `agent_runs`, and
`messages`. Keep evidence to the Session the user reported, plus any others they point
to — a channel's `scope_id` can hold other people's threads; the user's
own corrections in `messages` are usually the sharpest evidence. For a
recurring Task or Watch, `vibe runs list --definition-id <id>` gathers its
fires across per-run Sessions. A stuck delegated turn stays `running`, so include
long-running rows when the complaint is a stall; ordinary IM turns have no
`agent_runs` row, so read that Session's messages instead. A delegated run can succeed while
its report never arrives; check `callback_status` and `callback_error` when the
complaint is a missing result. Two starting points, each run
with `vibe data query --sql-file <file>` (or `--sql-file -` for stdin):

```sql
-- The reported session and its current backend, model, and effort
select id, scope_id, agent_name, agent_backend, model, reasoning_effort, status
from agent_sessions where id = '<session>';

-- Recent failed, cancelled, or silent Agent runs in that session
select id, run_type, status, model, created_at
from agent_runs
where session_id = '<session>'
  and run_type in ('agent_run','scheduled','watch','webhook','task_escalation')
  and exit_code is null  -- command-backed Tasks record an exit code instead
  and (status in ('failed','canceled','cancelled')
       or (status in ('succeeded','completed') and coalesce(trim(result_text),'') = ''))
order by created_at desc;
```

Quote the minimum excerpt and redact secrets and unrelated private content.

A before/after probe spends the user's account and writes session state, so
propose it in the report unless the user asked for verification. A useful probe
reproduces the original conditions — same backend, model, and effort, and only
the context before the failing turn — rather than forking a session that
already holds the failure and its correction.

## From symptom to likely cause

User complaints map to recurring prompt defects. Treat these as leads to
check against the transcript, not verdicts.

| What the user sees | Where to look first |
| --- | --- |
| Agent stopped or went quiet mid-task | A "stop / wait / do not proceed" with no stated exit; "don't narrate" or "report only at the end"; a wait with no durable Watch or expiry meaning |
| Keeps asking for permission | "Ask before…" with no threshold separating reversible in-scope steps from irreversible or outward-facing ones |
| Did far more than asked | Autonomy with no scope bound or definition of done, most often on strong Claude models |
| Followed a rule where it made no sense | A bare prohibition with no reason or scope, most often on GPT/Codex; pressure language (caps, `MUST/NEVER`) |
| Behaves differently across Agents or backends | The same rule at different strengths in different layers; backend-specific tool names in shared text |
| Delegated work came back unusable | A brief missing goal, acceptance evidence, or report target; a callback that says "done" without the result |
| Recurring Task or Watch runs drift or repeat themselves | The fire message restates loaded rules, names finished work, or asks for output the recipient cannot act on |
| Stale commands, paths, or answers | Facts that no longer match the CLI or code; fossils like named-model workarounds or "now / no longer" phrasing |

## Report

Open with counts and up to three findings that matter most; zero findings is a valid report. For each
finding: location, the evidence excerpt, which idea above it violates and why
on which target, confidence (high: reproduced in transcripts or documented;
medium: consistent known behavior; low: heuristic, flag only), and the
proposed change — a file hunk, or a before/after payload plus the update
command for text stored in Avibe state. Rewrite rather than delete when the
concern is still live, and complete each removal across duplicates, tests, and
mirrors.

State each finding's confidence once and the audit's overall limits once;
repeating caveats in every paragraph buries the findings. Read-only checks,
such as `--help` or reading a file, settle a doubt faster than flagging it.
