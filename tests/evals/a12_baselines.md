# A12 compaction-quality baselines

One row per valid run of `a12_compaction_quality.py`, newest first. A prompt change is judged against the last row of
the previous prompt version. The score counts the planted facts the probe's JSON answer still states, each judged on
its own field: three ground rules with their polarity, two changed files with their values and a reason each, the
CHANGES.md entry, the last error verbatim, and the next step with its action. One more fact checks that the
checkpoints written before the probe, and the probe answer, keep the checkpoint request out of the user's record.

| Date (UTC) | Avibe | Prompt | Model (window / output budget) | Checkpoints past the facts (mode: tokens before → after) | Repairs / failed | Score | Missed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | `de84d3b59` (master), Incus master env | `checkpoint-v3` | claude-haiku-4-5 (32,000 / 4,000) | rolling: 33,528 → 15,563; rolling: 32,686 → 15,800 | 0 / 0 | 10/11 | leak: checkpoint 1's next step |

## Notes

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
