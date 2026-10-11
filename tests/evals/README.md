# Offline evaluations

LLM-in-the-loop evaluations of agent behavior that unit tests cannot pin: they make real model calls, so CI never
runs them, and pytest never collects them (no `test_` prefix). Each records a baseline per prompt version, so a prompt
change is judged against the last one.

## A12: compaction quality

Plan: `docs/plans/avibe-agent-core.md` §6, A12. Script: `a12_compaction_quality.py`. Baselines: `a12_baselines.md`.

**What it does.**
- It drives a real Vibey Session through the instance's own `vibe agent run`. Every model call goes through that
  instance's Model Hub, and every checkpoint through the shipped C-9 code.
- The scripted session runs in a temporary project with a temporary Agent. Across its Turns the user:
  - states three ground rules;
  - has the agent change two files, each for a stated reason;
  - has it run a check that fails twice;
  - agrees on a next step.
- Log reads sized from the model's window push the context over the threshold. The script adds up to three more
  reads until the Session holds at least two checkpoints.
- A probe Turn then asks, from memory, for the ground rules, the files changed and why, the last error verbatim, and
  the next step, as one JSON object. Each of the nine planted facts is judged on its own field:
  - a rule only with its polarity: each rule is `{"action", "kind": "must" | "must_not"}`;
  - a changed value only inside its file's entry, and a reason only in that entry's `why`;
  - the last error as the whole line.

**Valid runs only.** A run is scored only if ground truth (the database rows and the fixture on disk) shows that it
happened as scripted:
- every Agent run succeeded within its wait;
- the fixture ends with BURST = 5, REFILL_RATE = 1, exactly the two CHANGES.md lines for those changes, `allow()`
  still defined, and `vendor/legacy_api.py` untouched;
- both check outputs are in the Session's tool results, in order;
- the Session compacted at least twice before the probe, and the probe ran no tool (judged by committed tool results,
  not the display traces);
- every model-written checkpoint used the checkout's prompt version.

Otherwise the JSON says `"valid": false` with the reasons, no score is given, and the script exits with status 3. A
failed or detached Agent run stops it at once. A non-empty `--project-dir` is refused. Everything reported comes from
the rows: a checkpoint is model-written only when its row names a summarizer, and the prompt version is the
summarizers'.

**Where it runs.** It writes real Sessions and spends real tokens, so it runs only inside a regression instance
(`VIBE_DEPLOYMENT_ENV=regression`); elsewhere it exits with status 2.

**How to run it.**
1. Give Vibey a model whose Model Hub definition has a window of at most 64,000 tokens. For example, add
   `claude-haiku-4-5` in Settings → Models → Vibey and edit its window to 32,000 and its max output to 4,000.
2. In the instance (`python3 scripts/incus_regression.py shell --target ...`), run:

   ```bash
   cd /opt/avibe/source && /opt/avibe/venv/bin/python tests/evals/a12_compaction_quality.py --model claude-haiku-4-5
   ```

   It drives the `vibe` next to that Python (`/opt/avibe/venv/bin/vibe`); `--vibe` names another one.

3. Read the JSON it prints:
   - whether the run is valid, and why not;
   - the prompt version and model;
   - each compaction (mode, sizes, and whether the model wrote it);
   - each checkpoint turn's outcome and repair;
   - the per-fact score and the probe answer.
4. Add a row to `a12_baselines.md`, then restore the model definition.

The run makes about a dozen Turns, plus their checkpoint turns, on the small window.

**Known limits.** The threat model is an honest model behaving imperfectly, not an adversary gaming the scorer.
- The scorer matches keywords inside the right field, so a correct reason in unexpected words can be missed. Read the
  answer before trusting a drop.
- A run is checked against the scripted end state, not against every way an agent could misbehave. Checks that only
  matter against deliberate gaming, or that the recorded runs already satisfy, are not added.
