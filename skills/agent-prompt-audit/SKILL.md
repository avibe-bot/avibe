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
confidence, and a **proposed change** per finding — a file hunk for
file-owned text, or a before/after payload plus the exact update command for
text stored in Avibe state (Agent prompts, Task and Watch messages). Apply
edits only when the request asks for them. A clean surface is a valid result; do not
manufacture findings.

## 1. Scope and target

State these at the top of the report as assumptions instead of asking:

- **Scope**: the Agent, Skill, file, or symptom the user named; otherwise the
  whole surface that reaches the Agents in question.
- **Targets**: the backend and model that actually produced the behavior —
  from the run or session record (`vibe runs show <id>`, `agent_sessions`),
  since overrides, forks, and later edits make the current Agent definition
  (`vibe agent show <name> --json`) a comparison point, not the source. Shared
  text (global rules, AGENTS.md, Skills) must work on every backend that loads
  it. Take model-specific behavior from the vendor's current documentation,
  through a docs Skill or tool when one is loaded, otherwise the vendor's
  public docs; if neither is reachable, say so and lower confidence on
  model-specific claims rather than relying on memory.

## 2. Inventory the lifecycle surface

List what you found before auditing it. The layers, by when they reach the Agent:

1. **Always loaded**: Avibe-injected runtime prompt, global rules, the project
   AGENTS.md/CLAUDE.md chain, the Agent's system prompt, and the Skill
   descriptions on the injected catalog page (`vibe skill list`, page 1).
2. **On demand**: Skill descriptions on later catalog pages or marked
   `disable-model-invocation`, Skill bodies, and references.
3. **At work time**: delegation briefs, Task and Watch messages (re-sent on
   every fire), callback and review-loop prompts, tool descriptions.

Always-loaded and recurring text has the highest leverage; audit it first.

Where each layer lives, who owns it, and how to change it. Verify paths against
the current machine; they are starting points, not guarantees.

| Layer | Typical location | Owner / edit channel |
| --- | --- | --- |
| Avibe runtime prompt (capabilities, Harness, Skills catalog, quick replies) | Avibe repo `core/prompts/*.md`, assembled by `core/prompt_registry.py` | Avibe repository PR; propose, do not patch locally |
| Global rules | Backend global files such as `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` | If a file is generated or imports other files (`@path` lines, symlinks, a generated-file header), edit the source and regenerate as it documents; otherwise edit directly |
| Project rules | nearest `AGENTS.md` / `CLAUDE.md` chain from the workdir up | The repository's delivery process |
| Agent system prompt, model, effort | `vibe agent show <name> --json` → `agent.system_prompt` | `vibe agent update <name> --system-prompt-file <file>` |
| Backend-native agent definitions | `~/.claude/agents/*.md`, backend config dirs | Edit the file directly |
| Skills (catalog page 1 descriptions injected; other descriptions and bodies on demand) | User skill dirs per backend (e.g. `~/.claude/skills`, `~/.codex/skills`; often symlinked to a shared dir — follow links to the real owner), Avibe built-ins under `skills/` in the Avibe repo, project `.agents/skills/` | Owner of that directory; built-ins via Avibe PR |
| Scheduled Task messages | `vibe task list` / `show` | `vibe task update` |
| Watch messages (re-sent on every fire) | `vibe watch list` / `show` | `vibe watch update`; see `background-watch-hook` before re-creating |
| Delegation briefs and callbacks | `agent_runs.message` / `result_text` | The orchestrating Agent's prompt or Skill that writes them |

## 3. Gather behavioral evidence

Prefer what Agents actually did over what the text seems to say. First
resolve the sessions in scope, then pull their recent failed, cancelled,
long-running, or user-corrected Agent runs and read the
transcripts around the failure. User corrections ("you
stopped", "why didn't you report", "don't ask me that") are the strongest
signal. For each symptom, find the line that produced it — or the missing line
that would have prevented it. Use `git blame` on version-controlled prompt files to
learn which incident a rule was written for, and whether that failure still
reproduces.

`vibe data query` is read-only SQLite; `PRAGMA` is not authorized, so sample a
row (`select * from <table> limit 1`) to see columns. Scope every query to the
audited Agent's sessions so unrelated conversations never enter the evidence:
resolve them first, then filter by `session_id`. Only Agent-bearing run types
(`agent_run`, `scheduled`, `watch`, `webhook`, `hook`, `task_escalation`)
reflect prompt behavior; `hook_send`, `task_run`, and `watch_runtime` are
command executions. Empty `result_text` means silence only on a terminal run;
queued or running rows are in flight unless they are older than the work
should take.

```sql
-- Sessions in scope
select id, agent_backend, model, reasoning_effort, title, last_active_at
from agent_sessions where agent_name = '<agent>'
order by last_active_at desc limit 20;

-- Failed, cancelled, silent, or stuck Agent runs in those sessions, last 14 days
select id, run_type, status, agent_backend, model, created_at,
       coalesce(trim(result_text),'') = '' as no_result
from agent_runs
where session_id in ('<session>', ...)
  and run_type in ('agent_run','scheduled','watch','webhook','hook','task_escalation')
  and created_at > datetime('now','-14 days')
  and (status in ('failed','canceled')
       or (status in ('succeeded','completed') and coalesce(trim(result_text),'') = '')
       or (status in ('queued','running') and created_at < datetime('now','-2 hours')));

-- User corrections in those sessions (adjust keywords to the user's language)
select session_id, created_at, substr(content_text,1,200) text
from messages
where author = 'user' and session_id in ('<session>', ...)
  and created_at > datetime('now','-14 days')
  and (content_text like '%why did%' or content_text like '%stop%'
       or content_text like '%为什么%' or content_text like '%卡住%' or content_text like '%不要%');

-- Transcript around a hit: locate messages, then read the relevant span
select id, author, type, created_at, length(content_text) len,
       substr(content_text,1,200) head
from messages where session_id = '<session>' order by created_at;

select substr(content_text, max(1, instr(content_text,'<phrase>') - 300), 1200) excerpt
from messages where id = '<message>';
```

`vibe runs show <id>` gives one run's prompt, result, and callback state. The
session's backend and model are in `agent_sessions`.

## 4. Classify and scan

For each instruction ask: is this **context only the author knows** (audience,
environment facts, contracts, authority, reasons — keep), or a **constraint on
behavior** (test whether it still earns its place)? Then scan against the
patterns below. A finding must name its pattern and a reason grounded in
evidence or documented model behavior; otherwise it is a low-confidence flag
or nothing. The keep list at the end is as binding as the patterns.

Each pattern names the symptom to look for, why it misbehaves, and the fix.
Classify by function before flagging: text that routes (a Skill description,
a trigger line) may carry calibrated urgency; text that shapes behavior should
state intent with its reason.

### A. Dated prompt text

- **Pressure language.** Caps `MUST/NEVER/CRITICAL`, `!!`, emphasis without a
  reason. Current models over-apply it and turn rigid in gray areas; an anxious
  prompt yields a hedging Agent. Fix: state the one or two real constraints
  plainly, with the reason. Hedges on real requirements (`try to`, `if
  possible`) are read literally as optional; make them plain.
- **Method over goal.** Step scripts for judgment work, strategy coaching,
  exhaustive prohibition lists. The model's own plan usually beats the script,
  and a ban on a failure it wasn't making can anchor it. Fix: outcome,
  constraints, how to verify; keep exact steps only where one sequence is safe.
- **Thinking and format scaffolds.** "Think step by step", scratchpad tags,
  "think harder/less", numeric word caps, fixed progress-update cadences,
  "never use bullets". Depth belongs to the reasoning-effort setting; caps
  starve hard answers; anti-format rules written for over-formatting models now
  strip formatting readers want. Fix: remove, or say when the behavior is wanted.
- **Update suppressors.** "Don't narrate", "hold findings for the end". Current
  models already under-narrate; with these present users see silence. Fix:
  remove, or say when user-facing text is wanted.
- **Fossils.** Named-model workarounds, relative phrasing (`now`, `no longer`,
  `instead of`) that diffs against a version the Agent never saw, incident IDs
  and PR numbers as authority, one session's stumble encoded as a permanent
  rule. Fix: state the current rule; generalize accreted special cases into
  the principle they share.
- **Padding.** Generic virtues, repetition-as-reinforcement, identity stubs
  standing in for context. Fix: say it once, where it applies; replace the stub
  with audience, product, and quality bar.

### B. Lifecycle control gaps

Agents run across many turns, wake on callbacks, and hand work to each other.
Most costly Avibe failures are here, and the fix is often *adding* a clause.

- **Stop without an exit.** A rule says "stop", "pause", "wait", or "do not
  proceed" but not what the turn must produce instead. Literal backends
  (notably GPT/Codex) end the turn silently. Fix: say that stopping changes
  the work, not whether it continues, and that the turn ends with a next
  action or a delivered report.
- **Permission loops.** "Ask the user before…" without a threshold, so the
  Agent asks about reversible, in-scope steps. Fix: name what warrants asking
  (major trade-off, irreversible or outward-facing action, genuinely ambiguous
  direction) and say to proceed otherwise.
- **Unbounded autonomy.** The inverse: continuation with no scope bound after
  repeated failure. Fix: continue only with the smallest complete, reversible,
  contract-preserving action.
- **Silent waits.** Instructions to wait on a signal with no durable waiter,
  no timeout meaning, or no report on expiry. Fix: route through Harness
  (`vibe watch`, `vibe task`) and say what an expiry or error must produce.
- **Lossy handoffs.** Delegation briefs that omit the goal, acceptance
  evidence, or where to report; callbacks that return "done" without the
  result. Fix: a brief carries outcome, constraints, evidence required, and
  the report target; a callback carries the result itself.
- **Recurring-message drift.** Task and Watch messages re-sent on every fire
  that restate rules already loaded, reference finished work, or ask for
  output the recipient cannot act on. Fix: say only what this fire must do
  and how to report.

### C. Layering defects

- **Duplicates that disagree.** The same rule at different strengths across
  global rules, AGENTS.md, Agent prompts, and Skills; the Agent reconciles by
  guessing. Fix: one canonical home for the mechanism; other layers keep a
  principle or a pointer. Duplicates that agree and work are not a finding.
- **Wrong layer.** Project procedure in global rules, personal habits in
  repository files, long procedures in always-loaded text instead of an
  on-demand Skill, secrets or volatile facts anywhere. Fix: move it to the
  layer whose load timing matches its use.
- **Skill descriptions.** Those on the injected catalog page are loaded on
  every turn; later pages and `disable-model-invocation` Skills reach the
  Agent only when listed or named. Vague injected ones under-trigger,
  enumerated synonym lists tax every request. Fix: name intent
  categories and the trigger conditions in one or two sentences.
- **Stale facts.** Paths, flags, versions, or CLI shapes that no longer match
  the machine. Fix: verify against the current CLI or code and correct.

### D. Cross-backend hazards

Shared text runs on backends with different defaults. Check each shared line
against every backend that loads it.

- **Literal compliance** (GPT/Codex tend here): a bare prohibition or stop
  condition is followed exactly, including where it shouldn't apply. Needs
  explicit scope and exit conditions.
- **Over-reach** (strong Claude models tend here): scope expansion,
  over-verification, and delegation beyond the ask. Needs scope bounds and a
  definition of done.
- **Backend-specific mechanics** (tool names, hook syntax, native subagents)
  in shared text dangle on other backends. Fix: phrase the intent, or move
  mechanics into backend-specific files.

### Keep list

These stay even when a pattern matches:

1. Context only the author knows: environment facts, contracts, authority and
   ownership, quality bar, and the reasons behind constraints.
2. Exact scripts for fragile operations: destructive commands, auth, merge
   gates, key custody, anything where one sequence is safe.
3. Prohibitions against failures that still reproduce, visible in transcripts.
4. Scope bounds that make autonomy safe; removing them is not "de-prescribing".
5. A short role line; a single deliberate end-of-prompt recap.
6. Working redundancy between layers that agrees, such as a fallback line for
   when a Skill fails to load.

## 5. Report and proposed changes

Per finding: location (`file:line`, Agent name, or Task/Watch id), the
minimum excerpt that establishes the behavior (redact secrets, credentials,
and unrelated private content), pattern, why it misbehaves on which target,
confidence (**high**: reproduced in transcripts or documented; **medium**:
consistent known behavior; **low**: heuristic — flag only), and action (`remove` / `rewrite` with the
replacement / `move` with destination / `add` / `flag`). Order by impact.
Open with counts and the two or three findings that matter most, in prose.

In each proposed change, rewrite rather than delete when the concern is
live, and complete each removal: references, duplicates in other layers, tests asserting the old
text, and mirrors.

## 6. Apply through the owning channel

When asked to apply, back up first and edit where the text is owned:
the source of a generated or imported file rather than its output (then
regenerate it the way the source documents), personal files directly, Agent
prompts through `vibe agent update <name> --system-prompt-file <file>`, Tasks and Watches
through `vibe task update` and `vibe watch update`, repository files through the
repository's own delivery process as its AGENTS.md defines it, and
Avibe-injected prompts only as a proposal to the Avibe repository.

## 7. Verify

A removal is a hypothesis. For contested changes, probe behavior before and
after on each target backend with a scratch run that exercises the
instruction's purpose. Probe the target resolved in step 1, not the Agent's
current definition: fork the affected session and pin its model and effort
(`vibe agent run --fork-session <session> --model <model> --reasoning-effort <effort> --sync --message ...`),
and read the transcript rather than asking the model whether it needs the
rule. Change one thing at a time where stakes are high. If a cut regresses,
re-add it in minimal form. Re-audit after any model or backend change.
