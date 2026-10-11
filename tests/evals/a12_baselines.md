# A12 compaction-quality baselines

One row per valid run of `a12_compaction_quality.py`, newest first. A prompt change is judged against the last row of
the previous prompt version. The score counts the planted facts the probe's JSON answer still states, each judged on
its own field: three ground rules with their polarity, two changed files with their values and a reason each, the
CHANGES.md entry, the last error verbatim, and the next step with its action. One more fact checks that the
checkpoints written before the probe, and the probe answer, keep the checkpoint request out of the user's record.

| Date (UTC) | Avibe | Prompt | Model (window / output budget) | Checkpoints past the facts (mode: tokens before → after) | Repairs / failed | Score | Missed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | `346d2f35a` (master `aaf018a4b` with this prompt), Incus master env | `checkpoint-v4` | claude-haiku-4-5 (32,000 / 4,000) | rolling: 33,328 → 15,321; rolling: 32,432 → 15,324 | 0 / 0 | 11/11 | — |
| 2026-10-11 | `de84d3b59` (master), Incus master env | `checkpoint-v3` | claude-haiku-4-5 (32,000 / 4,000) | rolling: 33,528 → 15,563; rolling: 32,686 → 15,800 | 0 / 0 | 10/11 | leak: checkpoint 1's next step |

## Notes

### 2026-10-11, `checkpoint-v4`

**Run.** Session `seskp94mab5eu`. It was valid by every ground-truth check:
- every Agent run succeeded;
- the fixture ended as scripted, with exactly the two CHANGES.md lines;
- both check outputs were in the tool results, in order;
- the last planted fact is at context position 23, and both checkpoints summarized past it (to 26 and 30);
- the probe committed no tool result.

Both checkpoints were model-written under `checkpoint-v4`, each accepted on its first final reply: no repair round,
no failed checkpoint turn. Three log reads were needed. The run predates the C-9 token-estimate fix
(`fix/c9-token-estimate`, not merged at run time), which changes when checkpoints fire.

**Checked by hand (README, known limits).**
- Both check runs were `bash` calls.
- Both checkpoint turns made no tool round.
- Every checkpoint and every Turn was served by claude-haiku-4-5.

**The leak, against `checkpoint-v3`.**
- Neither checkpoint, nor the probe answer, carries the checkpoint request.
- Each checkpoint's next steps begin with the work in flight, "Provide one-line summary of logs/day1.log" (then
  day2): the work as it stood before the request.
- The `checkpoint-v3` row's first checkpoint recorded "(Awaiting user instruction; checkpoint paused before
  attempting any further work.)" there instead.

**Scoring.** The run scored 10/11 at the time. The next step, "resolve burst window overlap error before writing
regression test in tests/test_burst.py for burst of exactly 5", missed only because the verb list held "write" and
not its stem. With "writ", as with "creat", it counts, and the run scores 11/11.

**Observation, not scored.** Both checkpoints listed "Resolve burst window overlap error" before the agreed test.
That step is the agent's inference from the failing check; the user never asked for it. The probe answer carried it
forward.

**An earlier `checkpoint-v4` run, not scored.** In `ses6grpy6z3we` the agent rewrote CHANGES.md without its
"# Changes" heading. The gate took the first line for the heading, dropped the BURST entry, and judged the run
invalid. The gate now skips only heading lines. That run's checkpoints carried no leak either.

### 2026-10-11, `checkpoint-v3`

**Run.** Session `seszkhn28xbgp`. It was valid by every ground-truth check:
- every Agent run succeeded;
- the fixture ended as scripted, with exactly the two CHANGES.md lines;
- both check outputs were in the tool results, in order;
- the last planted fact is at context position 23, and both checkpoints summarized past it (to 26 and 30), so every
  scored fact went through both;
- the probe committed no tool result.

Both checkpoints were model-written under `checkpoint-v3`, each accepted on its first final reply: no repair round,
no failed checkpoint turn. Three log reads were needed.

**Checked by hand (README, known limits).**
- Both check runs were `bash` calls.
- Both checkpoint turns made no tool round.
- Every checkpoint and every Turn was served by claude-haiku-4-5.
- The output budget Vibey used is 4,000: the definition's maximum, under a quarter of the window.

**Scoring.** The run was recorded at 9/9. The scorer then added two facts:
- the CHANGES.md entry, which this answer has;
- the checkpoint request kept out of the user's record (with `checkpoint-v4`), which this run fails. Its first
  checkpoint's next steps begin "1. (Awaiting user instruction; checkpoint paused before attempting any further
  work.)".

It also bound the REFILL_RATE reason to refilling. The same run re-scores 10/11.

**Observation, not scored.** Both checkpoints were rolling, after an overflow. Each log read took the context from
under the threshold to past the window in one tool result, so no threshold checkpoint ran first.

**Earlier runs the same day, not comparable.** They planted the facts between the log reads. In `sesy9fy2gnybu`
(the previously recorded row, 9/9 under the nine-fact scorer), the last checkpoint before the probe summarized only to
position 29. The REFILL_RATE change and its reason (Turn at 31), the last error, and the next step (48) were still
verbatim in the context, so four of the scored facts never went through a checkpoint. `ses396nx6ty94` and
`sesvb45nauz87` also used earlier probe formats.
