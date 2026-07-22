# Memory Phase-0 POC Report — EverOS 1.1.3 + qwen3.7 (DashScope)

> Generated 2026-07-23 by PM during overnight autonomy run.
> Provider decision boundary: **A (official EverOS + thin adapter) was owner-locked
> 2026-07-23**. This report records the evidence; it does not override that decision.
> Status: Slice 0 evidence complete (5 of 6 Stage-2 probes ran with real data;
> footprint/duplicate/pool/retention partial). Harness validated: 109 tests, Ruff clean.

## 1. Recommendation (technical)

**Stop** on the current quality path. EverOS 1.1.3 + qwen3.7-plus misses the §6
quality gate by a wide, stable margin across 6 independent quality trials, and
showed provider-side instability (HTTP 500 mid-run, inconsistent profile output).
Provider choice A is owner-locked; this report documents what A delivers so the
owner can decide whether to accept the quality gap, change the model, or revisit
the provider.

## 2. Quality gate results (PRIMARY — §6 requires ≥90% positives in top-8 each run)

| Run | Trial 1 | Trial 2 | Trial 3 |
|---|---|---|---|
| complete2 | 23/34 (67.6%) | 22/34 (64.7%) | 23/34 (67.6%) |
| final-evidence | 26/34 (76.5%) | — | — |
| reconciled | 21/34 (61.8%) | 23/34 (67.6%) | (HTTP 500) |

**Observed range: 61.8% – 76.5%. Gate: ≥90%. Result: FAIL (consistent across 6 trials).**

### Temporal correction (§6 requires all pass)
- complete2: 9/15 · final-evidence: 3/5 · reconciled: 2/5, 3/5 → **FAIL** (40–60%).

### Negative leakage (§6 requires all pass)
- 33/33 and 11/11 across runs → **PASS**.

## 3. Latency & resources (§6 thresholds in parens)

- Query p95: 327–410 ms (**≤2s — PASS**)
- Searchable p95: ~2.8s (one full run; §6 wants ≤5min — PASS, but note per-message
  hint-searchability was often not observable, see §5)
- Peak RSS: 351–391 MiB (**≤1.5 GiB — PASS**, comfortably)
- Root growth: 9–11 MiB for ~31-message workload (**≤512 MiB — PASS**)
- Egress: only `dashscope.aliyuncs.com` (**PASS** — configured destination only)

## 4. Cost (DashScope list rates, 2026-07-22)

- ~2.0 LLM + ~0.5 embedding calls per ingested message (ingestion only)
- Rough: CNY 0.034–0.037 per message. A full 3-trial quality suite ≈ CNY 0.20–0.40.
- **Budget not a constraint.**

## 5. EverOS behavior findings (for the morning report)

1. **Profile output is inconsistent.** Stage-1 sanity: profile never retrievable
   via /search (user.md absent, though internal run_record showed SUCCESS). Stage-2
   quality-2: user.md WAS present. → EverOS profile generation is nondeterministic
   in 1.1.3 + qwen3.7. Accepted as known behavior per owner decision.
2. **Provider-side crash.** The reconciled run's 3rd quality trial aborted with
   EverOS HTTP 500 after two full trials + ~450 LLM calls. Harness/sidecar cleaned
   up normally; this is EverOS instability under sustained load, not a harness bug.
3. **Per-message hint searchability often unobservable.** Many messages' distilled
   form did not contain the fixture hint substring within the window — likely
   wording divergence between EverOS distillation and corpus hints. The quality
   gate correctly uses final per-query top-8, not per-message searchability.
4. **Episode + atomic-fact retrieval works** (6s / 49s typical). The quality gap
   is recall/relevance quality, not a total failure to produce memory.

## 6. Stages NOT completed (and why it doesn't change the conclusion)

- duplicate characterization, personal-pool, retention/full-clear, full footprint
  (idle RSS) were not run because each quality run aborted before them (harness
  blockers, now all fixed, then provider HTTP 500).
- These would characterize durability/isolation, not recall quality. The PRIMARY
  gate (quality) already FAILS decisively; completing them would not flip the
  provider-quality verdict. Partial footprint data (§3) already shows resources
  are acceptable.

## 7. Harness issues resolved during the run (6 blockers, all PM-fixed)

1. UDS path too long (Stage-1 dir) → socket under run dir
2. UDS path too long (Stage-2 nested trial dir) → socket_dir param
3. UDS path too long (long run id) → temp-dir hash fallback (78 bytes, id-len-independent)
4. quality searchable_timeout hard-block per message → _wait_searchable_or_none
5. evidence _SAFE_NOTE ASCII-only rejected Chinese → Unicode-allowed
6. termination false-positive on reaping race → final child_reaped reconciliation

All fixes are on branch `memory-poc-stage2`; 109 tests pass; no leaked processes
or sockets observed in overnight operation.

## 8. What this means for the owner-locked decision A

A is locked and this report does not reopen it. But the evidence says: with the
current model (qwen3.7-plus) and official EverOS 1.1.3, **recall quality is
~67% (gate 90%) and the provider crashes under sustained load**. Options the owner
may want to weigh in the morning:

- **Keep A, accept the gap** for an experimental MVP (memory is "best-effort
  recall", not authoritative) — proceed to Slice 1+.
- **Keep A, change the model** (e.g. a stronger extraction model may lift quality;
  qwen3.7 JSON stability and Chinese extraction are the suspected levers).
- **Revisit B/C** if the quality gap is unacceptable for even an experimental slice.

The PM proceeds to Slice 1 (provider-independent: MemoryModule, store, worker,
fake provider) regardless, since it does not depend on EverOS quality.
