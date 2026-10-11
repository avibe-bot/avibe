# A12 compaction-quality baselines

One row per valid run of `a12_compaction_quality.py`, newest first. A prompt change is judged against the last row of
the previous prompt version. The score counts the planted facts the probe's JSON answer still states, each judged on
its own field: three ground rules with their polarity, two changed files with their values and a reason each, the last
error verbatim, and the next step.

| Date | Avibe | Prompt | Model (window / output) | Checkpoints (mode: tokens before → after) | Repairs / failed | Score | Missed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | `de84d3b59` (master), Incus master env | `checkpoint-v3` | claude-haiku-4-5 (32,000 / 4,000) | normal: 26,009 → 25,272; rolling: 38,035 → 5,331; normal: 24,090 → 5,520 | 0 / 0 | 9/9 | — |

## Notes

### 2026-10-11, `checkpoint-v3`

**Run.** Session `sesy9fy2gnybu`. It was valid by every ground-truth check:
- every Agent run succeeded;
- the fixture ended as scripted, with exactly the two CHANGES.md lines;
- both check outputs were in the tool results, in order;
- all 3 checkpoints came before the probe;
- the probe committed no tool result.

All 3 checkpoints were model-written under `checkpoint-v3`, each accepted on its first final reply: no repair round,
no failed checkpoint turn. The script was launched without `vibe` on the PATH and drove the instance's CLI next to its
Python.

**Observation, not scored.** The first, normal checkpoint made little room (26,009 → 25,272 estimated tokens). The cut
kept the latest log read verbatim, so the next request overflowed and the ladder rolled. Every run so far shows this.

**Earlier runs the same day, not comparable.**
- `ses396nx6ty94` (9/9) used a free-text probe, and its validity was never checked.
- `sesvb45nauz87` was valid under the then-current gate but used the rule format before polarity. It scored 8/9: its
  REFILL_RATE reason, "REFILL_RATE=0 prevented any token refills", is the planted reason in other words, outside the
  phrase list.
- Neither run had a repair round or a failed checkpoint turn. This run's answer gave that reason as "enable token
  bucket refills", which the list covers. Reason matching stays a keyword heuristic (README, known limits).
