# A12 compaction-quality baselines

One row per run of `a12_compaction_quality.py`, newest first. A prompt change is judged against the last row of the
previous prompt version. The score counts the planted facts the probe answer still states: three ground rules, two
changed files with a reason each, the last error verbatim, and the next step.

| Date | Avibe | Prompt | Model (window / output) | Checkpoints (mode: tokens before → after) | Repairs / failed | Score | Missed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-11 | `de84d3b59` (master), Incus master env | `checkpoint-v3` | claude-haiku-4-5 (32,000 / 4,000) | normal: 25,931 → 25,252; rolling: 37,912 → 5,328; rolling: 34,010 → 15,803 | 0 / 0 | 9/9 | — |

## Notes

### 2026-10-11, `checkpoint-v3`

**Run.** Session `ses396nx6ty94`. The 7 scripted Turns and the probe needed no extra reads. All 3 checkpoints were
model-written, each accepted on its first final reply: no repair round, no failed checkpoint turn.

**Observation, not scored.** The first, normal checkpoint made little room (25,931 → 25,252 estimated tokens). The
cut kept the latest log read verbatim, so the Turn reached the window and the ladder rolled twice. The facts still
survived all three checkpoints.

**Scorer.**
- The scorer is a keyword match: case-insensitive phrases, and every group of a fact must match. It is coarse by
  design. Read the answer in the JSON before trusting a drop.
- This run's answer gave the REFILL_RATE reason as "to enable token bucket refills". The first phrase list missed
  that wording and scored the run 8/9.
- The list now also accepts "enable"; the fact's other check still requires "refill". The recorded answer was
  re-scored offline at 9/9, with no new model call.
