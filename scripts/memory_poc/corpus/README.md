# Memory POC corpus (Lane P2)

Synthetic fixture corpus and predeclared expectations for the EverOS phase-0
POC. This directory is the measuring stick for the provider decision: its
quality decides whether the POC verdict means anything.

- `manifest.json` — corpus revision and counts (validates against
  [`../CONTRACT.md`](../CONTRACT.md) → *corpus/manifest.json*).
- `sessions.jsonl` — one message per line, ordered by `(session_key, seq)`.
- `queries.jsonl` — predeclared positive / negative / temporal expectations.

All three validate against the frozen CONTRACT exactly. Expectations were
declared **before any run** and are never tuned to results.

## 1. Principal and scenario (entirely synthetic)

One synthetic principal — a solo developer building a personal side project,
codename **Seagull** — talks to the agent across three sessions in the fixed
`personal` project. No real names, employers, chat logs, or personal data
appear; the only proper nouns are generic developer tooling (PostgreSQL,
Fly.io, Stripe, …), which are not personal data. Text is Chinese-first with
natural mixed English for tech terms and product names.

| Session | `session_key` | Story beat | Day (offset) |
|---|---|---|---|
| Kickoff | `s1` | Initial tech/business choices, stable preferences, goals | day 0 |
| Two-week review | `s2` | Corrections to early choices, episodic events, more prefs | day 14 |
| Later session | `s3` | Cross-session facts, more corrections, buffered tail | day 21 |

Two-plus `session_key`s for one principal exercise POC §5.3 (personal-pool
coherence across sessions); `s3` is a natural "third session" for querying the
global pool.

## 2. How the fixtures stress the design (deep-dive §7)

EverOS keeps **append-oriented history** (episodes/atomic facts retain dated
evidence) but a **single rewritten profile snapshot**, and temporal correction
is an LLM-guided convention, *not* an enforced temporal data model. The corpus
is built to expose exactly that seam:

- **Five temporal pairs.** Each *old* statement (`temporal-old`, in `s1`) and
  its *correction* (`temporal-new`, in `s2`/`s3`) name the **same subject**
  (database, deploy platform, state manager, pricing, payment scope) but the
  correction **deliberately never repeats the old value token**. So the `forbid`
  hint (old value) matches only the superseded statement and the `expect` hint
  (new value) matches only the correction — a stale-outranks-correction failure
  is mechanically detectable under substring matching. Corrections are clearly
  dated later (day 14 / day 21 vs day 0).
- **Retained history is respected.** Because superseded values legitimately
  survive in episode history, the authoritative stale test is the `temporal`
  **rank** comparison (correction must outrank superseded), not an absolute
  `negative` forbid. Absolute `negative` forbids are therefore reserved for
  values that never appear in the corpus at all (see §4).

## 3. Matching-rule discipline (CONTRACT)

Matching is NFC-normalized, case-insensitive substring match on returned item
text. To keep every expectation decidable from fixture text alone:

- **Positive queries are paraphrase/synonym-driven, not lexical echoes** of the
  fixture (e.g. "每个月能赚到多少钱" → `MRR`; "内测什么时候开跑" → `2026-08-01`;
  "带静态类型的语言" → `TypeScript`), so vector recall is genuinely tested.
- **Every `expect.text_hint` is a distinctive noun phrase contained in the
  referenced fixture message(s)** — a proper noun, exact date, or number that
  any faithful distillation must preserve. A local checker confirms each
  positive/temporal hint is a substring of its `seq_refs` text.
- **Every `negative` forbid hint is verified absent from the entire corpus**, so
  retained history can never turn a negative into a guaranteed failure.
- **Every `temporal` forbid hint is verified present in a `temporal-old` message
  and absent from its correction message**, guaranteeing the pair is decidable.

## 4. Query inventory

| Type | Count | Design |
|---|---|---|
| `positive` | 34 | ≥30 required. Paraphrase/synonym queries over preferences, goals, dates, episodic events, and current-state facts. |
| `temporal` | 5 | `expect` = correction, `forbid` = superseded, same subject. DB, deploy, state manager, pricing, payment scope. |
| `negative` | 11 | 8 unrelated-fact (pets, sports, city, OS, Django, Kubernetes, AWS, coffee — never in corpus) + 3 stale-assertion (current-marker + old value phrasings that only ever attach to the new value in the fixture, so they are decidably absent). |

Temporal pairs (subject → old ⇒ new):

| Subject | Old (`s1`) | New (`s2`/`s3`) | Temporal query |
|---|---|---|---|
| Database | MongoDB | PostgreSQL | q035 |
| Deploy platform | Heroku | Fly.io | q036 |
| State management | Redux | Zustand | q037 |
| Pricing | free / donation (捐赠) | $5/month subscription | q038 |
| Payment scope | Alipay + WeChat (支付宝/微信) | Stripe only | q039 |

## 5. Coverage — POC §4 mandatory content

| POC §4 requirement | Where satisfied |
|---|---|
| ≥30 predeclared zh / mixed positive queries | `queries.jsonl` q001–q034 (34 positive) |
| Negative queries for unrelated facts | q040–q047 (8 unrelated-fact) |
| Negative queries for stale assertions | q048–q050 (stale current-state, decidably absent) + `temporal` rank tests q035–q039 |
| Temporal corrections (old choice replaced) | 5 pairs, `temporal-old`↔`temporal-new`; e.g. DB MongoDB⇒PostgreSQL (q035) |
| Stable preferences | s1#7 TypeScript, s1#8 pnpm, s1#9 Neovim/LazyVim, s2#5 Conventional Commits, s2#6 dark mode, s2#11 ≤50-line functions, s3#2 2-space indent, s3#3 lo-fi |
| Goals | s1#1 3-month MVP, s1#10 $1000 MRR, s2#7 i18n, s3#4 learn Rust |
| Dates | s1#1 2026-09-30, s2#8 2026-08-01, s3#5 renew 3 月 15 日 |
| Short episodic events | s1#11 scaffold, s1#12 hackathon, s2#1 OAuth bug, s2#9 first deploy, s2#10 slow-search feedback, s3#1 payment refactor |
| ≥2 sessions for same principal | `s1`, `s2`, `s3` |
| `buffered-tail` (buffered until explicit flush) | s3#8 (incomplete Pomodoro idea, session tail) |
| `kill-case` (response-loss/kill experiment) | s2#10 |

## 6. Coverage — POC §6 criteria that this corpus feeds

| Criterion id (CONTRACT §report) | Corpus content that decides it |
|---|---|
| `temporal_all` | q035–q039: every correction's `expect` must outrank its `forbid`. Each pair names the same subject with non-overlapping value tokens so the assertion is mechanically checkable. |
| `negatives_all` | q040–q050: no returned item may match any `forbid` hint. All hints verified absent from the corpus, so a pass reflects the provider, not the fixture. |
| `positive_top8_rate` | q001–q034: ≥90% must return the expected item in top-8 each clean run. Paraphrase/synonym phrasing tests genuine recall rather than lexical echo. |

## 7. Notes / caveats for P1 and the PM

- `occurred_offset_ms` is monotonic across the file; day spacing (0 / 14 / 21)
  makes corrections unambiguously later than the statements they supersede.
- Stale-assertion negatives (q048–q050) are intentionally conservative: under
  contiguous substring matching plus retained history, the robust stale signal
  is the `temporal` rank test. The stale negatives add defense-in-depth by
  forbidding "finality/current-marker + old value" phrasings that the fixture
  only ever attaches to the *new* value.
- A local checker (run during authoring) confirms: contract-shape, count
  integrity vs `manifest.json`, tag-vocabulary, positive/temporal hints ⊆
  referenced fixture, negative forbids ∉ corpus, and temporal forbids ∈ old ∧ ∉
  correction. No expectation was adjusted to any run output.
