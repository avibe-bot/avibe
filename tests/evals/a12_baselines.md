# A12 compaction-quality baselines

One row per valid run of `a12_compaction_quality.py`, newest first. A prompt change is judged against the last row of
the previous prompt version. The score counts the planted facts the probe's JSON answer still states, each judged on
its own field: three ground rules, two changed files with their values and a reason each, the last error verbatim, and
the next step.

| Date | Avibe | Prompt | Model (window / output) | Checkpoints (mode: tokens before → after) | Repairs / failed | Score | Missed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | `de84d3b59` (master), Incus master env | `checkpoint-v3` | claude-haiku-4-5 (32,000 / 4,000) | normal: 27,149 → 25,331; rolling: 38,069 → 5,602; normal: 24,172 → 5,869; normal: 24,159 → 6,068 | 0 / 0 | 8/9 | the reason for REFILL_RATE (scorer recall; see notes) |

## Notes

### 2026-10-11, `checkpoint-v3`

**Run.** Session `sesvb45nauz87`, valid by every ground-truth check:
- every Agent run succeeded;
- the fixture ended as scripted;
- both check outputs were in the tool results, in order;
- the Session compacted three times before the probe, and the probe called no tool;
- all 4 checkpoints were model-written under `checkpoint-v3`, each accepted on its first final reply: no repair round,
  no failed checkpoint turn.

The probe's own input crossed the threshold, so its Turn checkpointed once more before answering: the fourth row.

**The miss.** For `src/config.py` the answer's `why` was "REFILL_RATE=0 prevented any token refills, causing cascading
denials". That is the planted reason in other words, but the fact's phrase list ("never refill", "0 means", "zero",
"of 0", "enable") does not cover it. The miss is a recall limit of the keyword scorer, not lost context. The scorer
was not changed after seeing this answer, so the row stands as measured. The values, the other reason, all rules, the
verbatim error, and the next step were all stated.

**Observation, not scored.** The first, normal checkpoint made little room (27,149 → 25,331 estimated tokens): the cut
kept the latest log read verbatim, so the next request overflowed and the ladder rolled.

**History.** An earlier run on the same day (Session `ses396nx6ty94`, 3 checkpoints, no repair or failure) used a
free-text probe. Its keyword scorer could credit facts from the wrong part of the answer, and the run's validity was
never checked. That row was replaced by this one; it is not comparable.
