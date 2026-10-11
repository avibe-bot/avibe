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
- A probe Turn then asks, from memory, for the ground rules, the files changed and why, the last error verbatim,
  and the next step. The answer is scored against the nine planted facts.

**Where it runs.** It writes real Sessions and spends real tokens, so it runs only inside a regression instance
(`VIBE_DEPLOYMENT_ENV=regression`); elsewhere it exits with status 2.

**How to run it.**
1. Give Vibey a model whose Model Hub definition has a window of at most 64,000 tokens. For example, add
   `claude-haiku-4-5` in Settings → Models → Vibey and edit its window to 32,000 and its max output to 4,000.
2. In the instance (`python3 scripts/incus_regression.py shell --target ...`), run:

   ```bash
   cd /opt/avibe/source && /opt/avibe/venv/bin/python tests/evals/a12_compaction_quality.py --model claude-haiku-4-5
   ```

3. Read the JSON it prints:
   - the prompt version and model;
   - each compaction (mode, sizes, and whether the model wrote it);
   - each checkpoint turn's outcome and repair;
   - the per-fact score and the probe answer.
4. Add a row to `a12_baselines.md`, then restore the model definition.

The run makes about a dozen Turns, plus their checkpoint turns, on the small window.
