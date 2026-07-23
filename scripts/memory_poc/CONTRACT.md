# Memory POC Harness Contract (archived, frozen v1)

The harness is retained as evidence tooling, not as an actively maintained
production suite. Rerun it only when the provider, provider version, LLM, or
embedding model changes, or when a release decision requires new evidence.

This file freezes the data shapes shared by the two POC lanes. Lane P1
(harness/) and Lane P2 (corpus/) both cite this file. Neither lane changes it;
deviations route through the PM session.

Normative source: `docs/plans/memory-mvp/memory-poc-everos.md` (experiments and
pass criteria). This contract only fixes file shapes, not thresholds.

## Directory layout

```text
scripts/memory_poc/
├── CONTRACT.md   # this file; changes only via the PM
├── harness/      # Lane P1: env/lock, launcher, runner, probes, measurement
└── corpus/       # Lane P2: fixtures, queries, expectations, corpus README
```

Local run state (never committed) lives under `.runtime/memory-poc/` per the
POC document; `.env.poc` is discovered in the current worktree first, then the
primary checkout `/Users/rk/work/chainbot/avibe-bot/avibe/.runtime/memory-poc/`.

## Corpus files (P2 produces, P1 consumes)

### corpus/manifest.json

```json
{
  "corpus_revision": "2026-07-22.1",
  "message_count": 0,
  "query_count": 0,
  "notes": "free text, no personal data"
}
```

### corpus/sessions.jsonl

One JSON object per line, one line per message, ordered by (session_key, seq):

```json
{
  "session_key": "s1",
  "seq": 1,
  "text": "…user message text…",
  "occurred_offset_ms": 0,
  "tags": ["preference"]
}
```

- `session_key` is opaque; the harness maps it to a synthetic EverOS session id.
- `occurred_offset_ms` is relative to run start; the harness converts to
  absolute epoch milliseconds at ingestion time.
- `tags` vocabulary (extend only via PM): `preference`, `goal`, `date`,
  `episodic`, `temporal-old`, `temporal-new`, `buffered-tail` (must remain
  buffered until explicit flush; POC §4), `kill-case` (the message around which
  the response-loss/kill experiment runs).
- At least two distinct `session_key` values for the same principal (POC §5.3).

### corpus/queries.jsonl

```json
{
  "query_id": "q001",
  "type": "positive",
  "query": "…zh or mixed zh/EN query…",
  "expect": {
    "kind": "episode",
    "session_key": "s1",
    "seq_refs": [3, 4],
    "text_hint": "…normalized substring decidable from fixture text…"
  },
  "forbid": []
}
```

- `type`: `positive` | `negative` | `temporal`.
- `positive`: `expect` required; PASS iff a top-8 item matches `text_hint`
  (and `kind` when the provider reports one).
- `negative`: `forbid` required (list of `{"text_hint": …}`); PASS iff no
  returned item matches any forbidden hint.
- `temporal`: `expect` = the correction, `forbid` = the superseded assertion;
  FAIL if the superseded assertion outranks the correction (POC §5.2).
- Matching rule: NFC-normalized case-insensitive substring match on returned
  item text. P1 implements the matcher; P2 must choose hints decidable from
  fixture text alone. A failed request or timeout is never an empty success.

## Runner CLI (P1 provides)

```text
python -m memory_poc run --stage sanity|quality|pool|duplicate|retention|footprint --run-id <id>
python -m memory_poc report --run-id <id>
```

Quality runs MUST use the production write pattern: one message added and
explicitly flushed before the next item (POC §5.2).

## .env.poc format (owner-provided; mode 0600; never committed or printed)

```text
LLM_BASE_URL=…
LLM_MODEL=…
LLM_API_KEY=…
EMBEDDING_BASE_URL=…
EMBEDDING_MODEL=…
EMBEDDING_API_KEY=…
```

## runs/<run-id>/report.json (P1 produces; PM gates on it)

```json
{
  "run_id": "…",
  "harness_commit": "…",
  "corpus_revision": "…",
  "environment": {"os": "…", "machine_class": "…", "python": "…", "lock_id": "…", "llm_model": "…", "embedding_model": "…", "endpoint_locality": "remote|loopback", "timezone": "…"},
  "criteria": [{"id": "…", "state": "pass|fail|not_measured", "value": 0, "threshold": 0}],
  "quality": [{"query_id": "q001", "pass": true, "rank": 1, "latency_ms": 0}],
  "latency": {"add_ms": {}, "flush_ms": {}, "searchable_ms": {}, "query_ms": {}},
  "resources": {"env_size_bytes": 0, "idle_rss_p95_bytes": 0, "peak_rss_bytes": 0, "root_growth_bytes": 0, "llm_calls": 0, "embedding_calls": 0},
  "egress": ["hostnames only, never URLs or keys"],
  "duplicates": {"observed": "free text of exact outcome", "count": 0},
  "recommendation": "official|fork|stop"
}
```

`criteria[].id` enumerates POC §6 bullets: `temporal_all`, `negatives_all`,
`positive_top8_rate`, `query_p95_s`, `searchable_p95_min`, `env_size_gib`,
`wheels_all_targets`, `mrm_schema_fit`, `idle_rss_p95_mib`, `peak_rss_mib`,
`root_growth_mib`, `egress_configured_only`, `loopback_no_egress`,
`launcher_uds_only`, `restart_preserves`, `clear_removes_all`,
`no_internals_needed`.

### Criterion state (v1.1 — PM decision 2026-07-22)

Each `criteria[]` entry carries an explicit tri-state so unmeasured criteria are
never confused with a measured zero:

- `state = "pass"` — measured and meets the POC §6 threshold.
- `state = "fail"` — measured and misses the threshold.
- `state = "not_measured"` — not exercised in this run (e.g. a sanity-only run
  with no live provider keys). `value` and `threshold` MUST be `null` for a
  `not_measured` criterion. Never emit `0/0` for an unmeasured criterion.

A run whose gate criteria are all `not_measured` is not a POC pass; it is a
sanity/partial run. The final provider decision requires every §6 criterion to
reach `pass`.

### Companion summary.md (v1.1 — PM decision 2026-07-22)

Each run also writes `runs/<run-id>/summary.md` alongside `report.json` (already
anticipated by POC §7). Free-form, redacted prose evidence — observed HTTP
response shapes (POC §5.1), retention locations, duplicate/restart notes — lives
in `summary.md`. `report.json` stays the frozen machine-readable schema and does
NOT absorb prose or provider-internal shapes. Same redaction rule as below.

No secrets, endpoint URLs, or fixture message bodies appear in `report.json`,
`summary.md`, or logs.
