# Patterns

Each pattern names the symptom to look for, why it misbehaves, and the fix.
Classify by function before flagging: text that routes (a Skill description,
a trigger line) may carry calibrated urgency; text that shapes behavior should
state intent with its reason.

## A. Dated prompt text

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

## B. Lifecycle control gaps

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

## C. Layering defects

- **Duplicates that disagree.** The same rule at different strengths across
  global rules, AGENTS.md, Agent prompts, and Skills; the Agent reconciles by
  guessing. Fix: one canonical home for the mechanism; other layers keep a
  principle or a pointer. Duplicates that agree and work are not a finding.
- **Wrong layer.** Project procedure in global rules, personal habits in
  repository files, long procedures in always-loaded text instead of an
  on-demand Skill, secrets or volatile facts anywhere. Fix: move it to the
  layer whose load timing matches its use.
- **Skill descriptions.** Loaded on every turn for every Agent: vague ones
  under-trigger, enumerated synonym lists tax every request. Fix: name intent
  categories and the trigger conditions in one or two sentences.
- **Stale facts.** Paths, flags, versions, or CLI shapes that no longer match
  the machine. Fix: verify against the current CLI or code and correct.

## D. Cross-backend hazards

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

## Keep list

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
