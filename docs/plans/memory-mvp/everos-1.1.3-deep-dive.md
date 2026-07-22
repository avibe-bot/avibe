# EverOS 1.1.3 Technical Deep Dive

> Status: source review complete, runtime POC not yet run, 2026-07-21
>
> Scope: the installable PyPI artifact `everos==1.1.3`, not the later
> EverCore architecture currently described on the upstream `main` branch.
>
> Purpose: give reviewers enough implementation detail to decide what the
> Avibe phase-0 POC must prove. This document does not select EverOS for
> production.
>
> Related documents:
> - Provider decision: `docs/plans/memory-mvp/memory-plugin-product-research.md`
> - Provider POC: `docs/plans/memory-mvp/memory-poc-everos.md`
> - Product scope: `docs/plans/memory-mvp/memory-plugin-everos-phase1.md`
> - Technical boundary: `docs/plans/memory-mvp/memory-plugin-everos-phase1-tech.md`

## 1. Executive answer

EverOS 1.1.3 is a credible first POC candidate because it packages user
episodes, an evolving profile, atomic facts, agent cases/skills, Markdown
storage, and local embedded indexes in one Python service. It is much closer to
Avibe's desired personal-memory product than a vector database or flat-fact
store alone.

Its most important semantic property is that it maintains two different views
of the same conversation:

- **History is append-oriented.** Episodes and atomic facts retain dated
  evidence. A later contradiction does not erase the earlier conversation.
- **The profile is a current snapshot.** The LLM emits `add`, `update`,
  `delete`, or `none` operations, after which EverOS rewrites one `user.md`
  file. It does not append a second profile record or maintain profile
  validity intervals.

For the concrete example "I like A" followed the next day by "I no longer like
A", the expected shape is therefore:

| Surface | Expected result |
|---|---|
| Episode timeline | Two dated pieces of evidence remain |
| Atomic facts | Usually an old positive fact and a new negative/correction remain |
| Current profile | One rewritten snapshot should update or remove the old preference |
| Search | May return both historical statements unless the caller asks for the profile/current view |

This is an **LLM-guided convention, not an enforced temporal data model**.
EverOS has no deterministic contradiction key, `valid_from`/`valid_to` fields,
or semantic uniqueness constraint. A weak model can still keep two profile
items, delete the preference without recording its new state, or return stale
evidence first. Temporal correction is consequently a phase-0 acceptance test,
not a capability we should assume from the prompt.

The upstream benchmark runner is useful but does not provide verified release
evidence. Its README contains a 93.3% LoCoMo **sample report**, while the tagged
repository contains no corresponding result artifacts. Install size, RSS,
provider-call count, write-to-search latency, query latency, and contradiction
quality remain unmeasured for Avibe.

## 2. Evidence and version identity

### 2.1 Evidence labels

This document uses three evidence levels:

| Label | Meaning |
|---|---|
| Source-verified | Observed in the exact 1.1.3 PyPI source artifact, its pinned EverAlgo dependencies, generated OpenAPI, or tagged source |
| Upstream claim | Stated by upstream documentation without a committed raw result that reproduces it |
| POC-required | Not established for Avibe until measured with our corpus, providers, and hardware |

Source review can establish code paths and schemas. It cannot establish memory
quality, real provider compatibility, resource use, latency, or reliable
behavior under crashes.

### 2.2 Exact artifact under review

| Item | Value |
|---|---|
| Package | `everos==1.1.3` |
| Published | 2026-07-10 |
| Python | `>=3.12` |
| License | Apache-2.0 |
| Wheel | `everos-1.1.3-py3-none-any.whl`, 470,289 bytes |
| Wheel SHA-256 | `f54086f9d4e52420eab70030dc8c92b76852c5b5e40d8f485226078f0f78fed0` |
| Source archive | `everos-1.1.3.tar.gz`, 1,190,171 bytes |
| Source SHA-256 | `57a3365748d63780cb375b98b7480a5c01655569f7429a409717be58577c514d` |
| Git tag | `v1.1.3`, commit `45656d331e5af669d6c958f6af5a3400a5b0fb33` |

The artifact metadata comes from the [PyPI 1.1.3 release](https://pypi.org/project/everos/1.1.3/)
and its [release JSON](https://pypi.org/pypi/everos/1.1.3/json). Production must
pin and verify the wheel, not install an unbounded upstream branch.

### 2.3 Release-source drift

The PyPI source archive declares version `1.1.3`, but the `v1.1.3` Git tag's
`pyproject.toml` declares `1.1.2`. The tag and source archive also have
materially different README content. The default TOML configuration is
identical, and the tagged repository contains useful benchmark source that is
omitted from the source archive's include list.

This means there is no single upstream tree that can be treated casually as
the release source of truth:

1. Runtime and API claims in this document prefer the exact PyPI artifact.
2. Tagged benchmark code is treated as supplementary release evidence.
3. The wheel hash and generated OpenAPI must be captured in an Avibe lock.
4. Behavior inferred only from a later `main` branch is excluded.

The current upstream repository has evolved into EverCore and advertises
different architecture and benchmark results. Those results may be relevant to
the vendor's direction, but they are not evidence for the lightweight 1.1.3
sidecar being evaluated here.

## 3. Capability model

EverOS separates memory by `app_id`, `project_id`, owner, and memory kind.
Version 1.1.3 implements the following business artifacts:

| Kind | Owner | Meaning | Write pattern | Search/get support |
|---|---|---|---|---|
| Episode | User | Narrative distilled from a conversation boundary | Daily append | Yes |
| Atomic fact | User | Smaller factual statements extracted offline | Daily append, hidden | Used by retrieval; no public direct get type |
| Foresight | User | Predicted future need/action | Daily append, hidden | Internally indexed; not part of Avibe MVP |
| Profile | User | Current explicit facts and inferred traits | Single-file rewrite | Yes |
| Agent case | Agent | A past tool-use/problem-solving case | Daily append, hidden | Yes |
| Agent skill | Agent | Consolidated reusable procedure | Named `SKILL.md` directory | Yes |
| Knowledge document | Global scope | Imported source document | Knowledge tree | Dedicated API |
| Knowledge topic | Global scope | Extracted section/topic | Knowledge tree | Dedicated API |

Two server modes decide which pipelines run:

| Mode | Boundary detector | User memory | Agent cases/skills | Tool rows |
|---|---|---|---|---|
| `chat` | Conversation boundary detector | Yes | No | Ignored for user-memory extraction |
| `agent` | Agent boundary detector | Yes | Yes | Used for agent case/skill extraction |

Avibe's personal-memory MVP needs only the user track. Running in `chat` mode
reduces surface area and avoids paying for agent-memory extraction that the MVP
does not expose.

## 4. Technology stack

### 4.1 Runtime and libraries

| Layer | Technology | Role |
|---|---|---|
| Runtime | Python 3.12+ | Service and CLI runtime |
| Validation/config | Pydantic 2, pydantic-settings | Request models and layered settings |
| HTTP | FastAPI, Uvicorn, python-multipart | REST API and file upload |
| CLI/TUI | Typer, Textual | Service operations and demo UI |
| Relational state | SQLModel, SQLAlchemy async, aiosqlite, Alembic | Buffers, MemCell archive, queues, OME state |
| Retrieval index | LanceDB/Arrow | Vector ANN, BM25, and scalar filters |
| File format | Markdown, YAML frontmatter, PyYAML | Human-readable business artifacts |
| File convergence | watchdog, watchfiles, anyio, portalocker | Native file events, config reload, locks |
| LLM clients | OpenAI Python SDK | OpenAI-compatible LLM and embedding calls |
| Scheduling | APScheduler | Offline Memory Engine strategies |
| Tokenization | Jieba | Chinese BM25 tokenization |
| Observability | structlog, Prometheus client | Structured logs and metrics |
| Optional parsing | `everalgo-parser[svg]>=0.2.1` | Multimodal document/content parsing |

The package pins its algorithm layer separately:

- `everalgo-user-memory==0.3.1`
- `everalgo-agent-memory==0.3.1`
- `everalgo-rank==0.4.1`
- `everalgo-knowledge==0.1.1`

This split matters for reproducibility: pinning only `everos==1.1.3` is
currently sufficient because these four versions are exact dependencies, but
an audit or fork must include their source as part of the evaluated system.

### 4.2 Code organization

The source declares a one-way layered architecture:

```text
entrypoints (FastAPI, Typer)
    -> service orchestration
        -> memory workflows/strategies
            -> infrastructure (Markdown, SQLite, LanceDB, OME)
```

Import-linter contracts enforce this direction and prevent higher layers from
opening private storage modules directly. That is a good match for Avibe's own
requirement to integrate through a narrow provider adapter rather than read
EverOS databases.

## 5. End-to-end implementation

### 5.1 Write path

The normal ingestion path is:

```text
POST /api/v1/memory/add
  -> validate and normalize messages
  -> lock (app_id, project_id, session_id)
  -> merge with SQLite unprocessed_buffer and deduplicate by message id
  -> boundary-detection LLM
  -> complete MemCell(s) + incomplete tail
  -> archive raw MemCell payload(s) in SQLite
  -> synchronously extract and append user episode Markdown
  -> emit Offline Memory Engine events
  -> return accumulated/extracted

POST /api/v1/memory/flush
  -> force the remaining tail through extraction
  -> return extracted/no_extraction
```

Boundary detection retries malformed model output, but the API does not expose
a caller-supplied idempotency key or a durable receipt for every downstream
artifact. Avibe therefore cannot infer exactly-once materialization from a 2xx
response alone. EverOS generates an internal message id from `session_id`, the
message timestamp, and its index within the request. That naturally deduplicates
an identical replay while those rows are still in the current buffer, but it is
not a global ledger for messages that have already been extracted.

`/add` and `/flush` make the episode Markdown durable before reporting an
extracted result. Profile, facts, foresight, cases, and skills are asynchronous
OME products and can lag or fail independently.

### 5.2 Offline Memory Engine

The default strategies are:

| Strategy | Default | Trigger/effect |
|---|---|---|
| Atomic fact extraction | Enabled and required | Per user MemCell |
| Foresight extraction | Enabled | Per user MemCell; additional LLM call |
| Profile clustering | Enabled | On every extracted episode |
| Profile extraction | Enabled | On every profile-cluster update |
| Agent case extraction | Enabled in `agent` mode | Per agent MemCell |
| Skill clustering | Enabled in `agent` mode | On every agent case |
| Agent skill extraction | Enabled in `agent` mode | After skill clustering |
| Episode reflection | Disabled | Weekly cron when explicitly enabled |

`ome.toml` changes are hot-reloaded in roughly two seconds. Main
`everos.toml` changes require a restart. For an Avibe POC, foresight and all
agent strategies should be disabled so cost and latency measure the proposed
MVP rather than deferred features.

### 5.3 Markdown-to-index cascade

EverOS runs cascade in the API process; it is not a separate OS daemon:

```text
Markdown create/modify
  -> watchdog native event (FSEvents/inotify)
  -> durable md_change_state row in SQLite
  -> entry-level content hash diff
  -> embed only added/changed entries
  -> upsert LanceDB vector + BM25 + scalar columns
  -> advance queue LSN
```

This design allows direct Markdown edits to become searchable and allows the
LanceDB directory to be rebuilt. It also means reads are eventually consistent:
`/search` and `/get` can lag a successful `/add` or `/flush`.

Upstream memory API documentation claims typical cascade latency below one
second and up to roughly 10-15 seconds under load. That is an upstream claim,
not a release benchmark. The POC must measure the distribution and define the
Avibe worker's readiness/polling behavior.

### 5.4 Read path

`/search` supports four methods:

| Method | Implementation shape | Cost/quality implication |
|---|---|---|
| `keyword` | BM25 lexical recall | Cheapest; exact terms; language/tokenizer-sensitive |
| `vector` | Dense cosine recall | Semantic recall; one embedding call |
| `hybrid` | Lexical/vector fusion with rerank behavior by kind | Default balanced mode |
| `agentic` | Iterative cluster-path retrieval with cross-encoder rerank | Highest latency and provider cost |

User retrieval combines episodes with smaller atomic-fact evidence. Profile is
returned only when requested with `include_profile=true` or through `/get` with
`memory_type=profile`. This distinction is important for current-state queries:
searching the timeline is not equivalent to reading the profile.

## 6. Storage architecture and retention

### 6.1 On-disk layout

```text
<root>/
  everos.toml
  ome.toml
  <app_id>/<project_id>/
    users/<user_id>/
      user.md
      episodes/episode-<YYYY-MM-DD>.md
      .atomic_facts/atomic_fact-<YYYY-MM-DD>.md
      .foresights/foresight-<YYYY-MM-DD>.md
    agents/<agent_id>/
      .cases/agent_case-<YYYY-MM-DD>.md
      skills/skill_<name>/SKILL.md
    knowledge/<category>/<document>/...
  .index/
    sqlite/
      system.db
      system.db-wal
      system.db-shm
      ome.db
      ome.aps.db
      ome.db.lock
    lancedb/
      <memory-kind>.lance/...
  .tmp/
```

Markdown contains the inspectable, editable business artifacts. LanceDB is a
derived retrieval index. SQLite is operational state **and also a raw evidence
archive**.

### 6.2 What "Markdown source of truth" does and does not mean

Upstream describes Markdown as the source of truth because users can inspect
and edit the distilled memory, and LanceDB can be rebuilt from it. That is true
for the visible memory artifacts, but it is not the full retention story:

- `unprocessed_buffer` stores raw messages that have not reached a boundary.
- `memcell.payload_json` retains complete raw, boundary-grouped conversation
  payloads after the buffer is cleared.
- Profile regeneration reads those MemCell payloads from SQLite.
- No supported MemCell retention/cleanup operation was found in 1.1.3.
- OME run records, cluster state, cascade queue state, and scheduling state also
  live in SQLite.

Deleting only `.index/lancedb` is a valid index rebuild. Deleting all `.index`
preserves visible Markdown but loses raw evidence and operational history, so
it is not an operationally lossless rebuild. Avibe's disclosure and Clear all
must treat the entire dedicated EverOS root as personal data.

### 6.3 Durability and consistency

Default SQLite settings are WAL, `synchronous=NORMAL`, foreign keys enabled,
in-memory temporary tables, a 5,000 ms busy timeout, a 64 MiB journal limit,
and a 2 MiB per-connection page cache. The cascade queue is durable and replays
after a crash.

There are nevertheless distinct consistency boundaries:

| Boundary | Consistency |
|---|---|
| `/add` to raw buffer/MemCell | Synchronous SQLite write |
| Extracted `/add` or `/flush` to episode Markdown | Synchronous before response |
| Episode Markdown to search | Eventual cascade |
| Episode to profile/facts | Eventual OME, with independent retries/failure |
| Markdown edit to search | Eventual cascade |

### 6.4 Governance gaps

Version 1.1.3 has no supported public operation to:

- delete one episode, fact, or profile item;
- forget all artifacts derived from one source message;
- rebuild a profile after selective source deletion;
- obtain a profile version history;
- enforce a raw MemCell retention period;
- authenticate API clients.

The knowledge API does have document replacement/deletion, but that does not
provide deletion semantics for personal memory. The Avibe MVP therefore exposes
only disable and whole-root Clear all.

## 7. User profile behavior

### 7.1 Profile schema

There is one `users/<user_id>/user.md` file per user and scope. Its frontmatter
contains:

| Field | Shape | Meaning |
|---|---|---|
| `id` | string | Stable profile id such as `profile_<owner>` |
| `user_id` | string | Profile owner |
| `summary` | string | Short summary derived from the first usable profile item |
| `explicit_info` | list | Direct facts/preferences with category, description, evidence |
| `implicit_traits` | list | Inferred trait, description, basis, and evidence |
| `profile_timestamp_ms` | integer | Newest source time represented by the rewrite |

The body repeats the summary. The file is overwritten atomically as one current
snapshot; it is not an append log.

### 7.2 Initial extraction

With no prior profile, one LLM call receives chronological MemCells and returns
`explicit_info` and `implicit_traits`. The prompt instructs the model to:

- extract user facts, not assistant suggestions;
- use the conversation language in the output;
- require multiple signals for inferred traits;
- keep evidence with time context.

EverOS validates only the JSON shape. It does not independently verify that
the evidence supports the extracted claim.

### 7.3 Incremental update

When a profile exists, the model receives indexed old items plus newer
conversation evidence and returns operations:

| Operation | Intended use |
|---|---|
| `add` | Genuinely new information unrelated to existing items |
| `update` | Supplement, correction, or evolution of an existing item |
| `delete` | Explicit negation, outdated/trivial data, or direct contradiction |
| `none` | No useful user-profile information |

The code applies those operations by list index, rebuilds `summary`, and rewrites
the file. It does not run a second semantic contradiction check after applying
the operations.

Profile extraction currently runs after every clustered MemCell
(`PROFILE_EXTRACTION_INTERVAL = 1`). If the merged profile exceeds 45 total
explicit and implicit items, a second LLM compaction call asks for at most 30
items. Compaction is lossy and also model-dependent.

### 7.4 Detailed contradiction example

Assume the same owner and fixed Avibe scope.

Day 1 input:

```text
User: I like A and usually choose it on weekends.
```

Likely artifacts after OME completes:

```text
episodes/episode-2026-07-21.md
  - User said they like A and tend to choose it on weekends.

.atomic_facts/atomic_fact-2026-07-21.md
  - User likes A.

user.md
  explicit_info:
    - category: preference
      description: User likes A and often chooses it on weekends.
      evidence: On 2026-07-21 the user said ...
```

Day 2 input:

```text
User: I no longer like A. Please do not recommend it again.
```

Likely historical artifacts:

```text
episodes/episode-2026-07-22.md
  - User said their preference changed and A should not be recommended.

.atomic_facts/atomic_fact-2026-07-22.md
  - User no longer likes A.
```

For `user.md`, the prompt permits several semantically reasonable operation
sequences:

```json
{"operations":[{"action":"update","type":"explicit_info","index":0,
  "data":{"description":"User no longer likes A and does not want it recommended."}}]}
```

or:

```json
{"operations":[
  {"action":"delete","type":"explicit_info","index":0},
  {"action":"add","type":"explicit_info","data":{
    "category":"preference",
    "description":"User dislikes A and does not want it recommended.",
    "evidence":"On 2026-07-22 the user explicitly corrected the preference."}}
]}
```

Both leave one current profile item. A pure `delete` may leave no current
preference item. A model failure may leave both. EverOS itself does not select
between these outcomes; the configured LLM does.

The answer to "one record or two?" is therefore:

- **two historical records/evidence points:** yes, by design;
- **two profile records:** no, there is only one profile file;
- **two conflicting items inside that profile:** not intended, but possible;
- **a deterministic supersession link:** no.

If Avibe later needs explainable temporal truth, a separate temporal fact model
or validity ledger is required. Editing the prompt alone cannot provide that
contract.

### 7.5 What the user can observe

For the MVP, the UI and `/memory` surfaces should present two explicitly named
views:

- **Current profile:** the latest `user.md` snapshot, with a warning that it is
  inferred and may be wrong.
- **Timeline/search:** dated episode evidence that can include superseded
  statements.

Combining both into an unlabeled result would make correct historical retention
look like a contradiction bug.

## 8. Models, providers, and performance implications

### 8.1 Default model configuration

| Function | Default model | Default endpoint | Required protocol |
|---|---|---|---|
| Extraction/boundaries/profile | `openai/gpt-4.1-mini` | OpenRouter | OpenAI chat-compatible |
| Multimodal parsing | `google/gemini-3-flash-preview` | OpenRouter | OpenAI-compatible multimodal chat |
| Embedding | `Qwen/Qwen3-Embedding-4B` | DeepInfra OpenAI endpoint | OpenAI embeddings |
| Rerank | `Qwen/Qwen3-Reranker-4B` | DeepInfra inference | Provider-specific rerank shape |

API keys are empty by default. EverOS cannot perform useful extraction/search
until LLM, embedding, and normally rerank providers are configured.

### 8.2 Supported model shapes

There is no hard allowlist for the general LLM. Any endpoint accepted by the
OpenAI client can be configured, including OpenAI-compatible OpenRouter,
DeepInfra, vLLM, or Ollama deployments. Practical compatibility is narrower:

- the boundary and extraction model must follow long prompts and return valid
  JSON consistently;
- the model must handle Chinese/mixed-language evidence for Avibe;
- multimodal models must accept the content types EverAlgo sends;
- provider context windows must fit boundary/profile payloads;
- a protocol-compatible model is not automatically quality-compatible.

No 1.1.3 model-quality matrix was found. The POC must test the exact model and
endpoint combination that Avibe would ship.

### 8.3 Embedding constraints

The LanceDB schemas use a fixed 1,024-dimensional vector. The OpenAI-compatible
client slices longer responses to 1,024 values client-side. It does not pad
shorter vectors, so an embedding model must return at least 1,024 values and
must be validated against schema insertion.

Embedding defaults:

| Parameter | Default |
|---|---|
| `timeout_seconds` | `30.0` |
| `max_retries` | `3` |
| `batch_size` | `10` texts per request |
| `max_concurrent` | `5` requests |
| output dimension | `1024` |

Changing embedding model semantics requires rebuilding LanceDB. Even with the
same dimension, vectors from different models must not be mixed in one index.

### 8.4 Rerank constraints

The source supports three request shapes:

| `provider` | Endpoint shape | Model restriction |
|---|---|---|
| `deepinfra` | DeepInfra inference URL plus model | Qwen-oriented implementation/default |
| `vllm` | `<base_url>/rerank` | Whatever the endpoint supports |
| `dashscope` | Native text-rerank API | Exactly `gte-rerank-v2` in 1.1.3 |

The generated default comments mention DeepInfra and vLLM but omit DashScope,
while source code supports it. That is another small documentation/source drift
to cover with configuration tests.

Rerank timeout, retries, batch size, and concurrency have the same defaults as
embedding: 30 seconds, 3 retries, batch size 10, and concurrency 5.

### 8.5 Egress and subscription implications

Avibe's Claude Code, Codex, or OpenCode login/subscription is not an EverOS
processing endpoint. If OpenRouter, DeepInfra, OpenAI, or another remote
endpoint is configured, captured private messages leave the machine for
extraction, embedding, and reranking. "Local-first storage" does not mean local
processing.

The product must disclose every configured destination before capture is
enabled. A fully local deployment is technically possible through compatible
local endpoints, but its quality and desktop footprint are POC questions.

## 9. Benchmark and performance evidence

### 9.1 What the tagged runner measures

The `v1.1.3` repository includes a LoCoMo runner with this pipeline:

```text
ADD -> wait for cascade/OME -> SEARCH -> answer LLM -> judge LLM x3
```

Its default scored setup is:

| Setting | Value |
|---|---|
| Dataset | LoCoMo 10, 10 long conversations |
| Retrieval | `agentic` |
| `top_k` | `10` |
| Answer model | `gpt-4.1-mini`, temperature 0 |
| Judge model | `gpt-4o-mini`, temperature 0 |
| Judge runs | `3`, majority vote |
| Conversation concurrency | `10` |
| Search concurrency | `5` per conversation |
| Eval concurrency | `20` per conversation |
| Excluded category | Adversarial/unanswerable |
| Profile/foresight | Disabled for the run |

The runner's own command-line parameters are:

| Flag | Required/default | Meaning |
|---|---|---|
| `--run-name NAME` | Required | Result directory and benchmark `project_id` |
| `--conv N [N ...]` | `0` through `9` | Conversation indices |
| `--stages STAGE [STAGE ...]` | `add search answer judge` | Any ordered subset of the four stages |
| `--config NAME` | `config` | TOML filename without `.toml` |
| `--base-url URL` | `http://localhost:8000` | EverOS server |
| `--everos-root PATH` | `~/.everos` | Private queue polling root |
| `--data-path PATH` | `data/locomo10.json` | LoCoMo data file |
| `--smoke` | Off | Force 2 conversations, 50 messages, 10 questions, and one judge run |

The runner produces a reproducibility spec, per-conversation search/answer/judge
JSONL, aggregate JSON, and a text report. It also reaches into EverOS's private
SQLite queues to decide when indexing is complete; that readiness mechanism is
appropriate for a benchmark tool but not for Avibe's production adapter.

### 9.2 The 93.3% number is not a verified release result

The benchmark README shows a **sample** report with 93.3% LoCoMo majority
accuracy, 1,437/1,540 correct, and sample search latency of 23.1 seconds average
and 19.4 seconds p50. However:

- the tagged repository has no committed `benchmarks/results` artifacts;
- the sample identifies EverOS 1.1.0 and a placeholder-like Git hash;
- no raw JSONL, provider logs, costs, or run manifest backs the number;
- the benchmark directory is not included in the 1.1.3 PyPI source archive;
- LLM-as-judge results depend on provider/model versions and judge variance.

It is valid to say **EverOS ships a reproducible LoCoMo runner**. It is not valid
to say **EverOS 1.1.3 has independently verified 93.3% accuracy**.

The runner estimates, rather than proves, these costs:

| Scope | Upstream estimate | Token estimate |
|---|---|---|
| Smoke | 2-5 minutes | about 80k |
| One full conversation | 15-30 minutes | about 1M |
| Full 10-conversation run | 2-4 hours | about 10M |

The current EverCore repository/paper reports other benchmark values, including
LoCoMo and LongMemEval. Because that system is architecturally different, those
numbers must not be copied into the 1.1.3 decision record.

### 9.3 What remains unknown

| Metric | 1.1.3 release evidence | Required Avibe evidence |
|---|---|---|
| Retrieval quality | Sample LoCoMo report only | Fixed Chinese/mixed corpus plus temporal cases |
| Install footprint | Wheel file size only | Clean environment installed bytes |
| Idle RSS | None | Sidecar steady-state RSS |
| Peak RSS | None | Ingest, OME, index, and concurrent query peaks |
| Startup time | None | Cold and warm readiness |
| Write-to-episode | None | p50/p95/max |
| Write-to-search | Documentation claim only | p50/p95/max and timeout rate |
| Profile convergence | Prompt/source behavior | Repeated contradiction and correction tests |
| Query latency | Sample agentic report only | keyword/vector/hybrid p50/p95 |
| Provider calls/tokens | Runner estimates only | Calls and tokens by stage |
| Disk growth | None | Raw, Markdown, SQLite, and LanceDB growth separately |
| Crash duplicates | None | Kill/timeout/retry fault injection |

The canonical thresholds and test matrix belong in
`memory-poc-everos.md`; this document intentionally does not create a second
set of pass criteria.

## 10. Public HTTP API

The exact 1.1.3 generated OpenAPI exposes the endpoints below. In source,
interactive OpenAPI documentation is enabled only in development mode. All
successful responses use a `{request_id, data}` envelope. There is no built-in
authentication or authorization, so Avibe must keep the service on loopback and
own the process boundary.

No MCP server, Agent Tool protocol, or documented stable Python SDK is exposed
by 1.1.3. HTTP is the application integration surface; CLI and direct Markdown
edits are operational surfaces.

### 10.1 Endpoint inventory

| Method and path | Purpose |
|---|---|
| `GET /health` | Service/lifespan health |
| `GET /metrics` | Prometheus metrics |
| `POST /api/v1/memory/add` | Buffer messages and extract complete boundaries |
| `POST /api/v1/memory/flush` | Force extraction of a session tail |
| `POST /api/v1/memory/search` | Retrieve user or agent memory |
| `POST /api/v1/memory/get` | Paginate one memory kind |
| `POST /api/v1/ome/trigger` | Manually run an OME strategy |
| `POST /api/v1/knowledge/documents` | Upload/extract a document |
| `GET /api/v1/knowledge/documents` | List documents |
| `GET /api/v1/knowledge/documents/{doc_id}` | Read document metadata/topics |
| `PUT /api/v1/knowledge/documents/{doc_id}` | Replace a document |
| `PATCH /api/v1/knowledge/documents/{doc_id}` | Update title/category |
| `DELETE /api/v1/knowledge/documents/{doc_id}` | Delete a knowledge document |
| `GET /api/v1/knowledge/topics/{topic_id}` | Read a topic |
| `POST /api/v1/knowledge/search` | Search knowledge topics |
| `GET /api/v1/knowledge/categories` | List categories |

Only the four memory endpoints and health/status behavior are relevant to the
proposed Avibe MVP. OME trigger and knowledge endpoints should not be exposed
through the first provider port.

#### Error response classification gap

The tagged documentation and generated OpenAPI do not define a stable error
taxonomy that is sufficient to separate a processing-endpoint outage from a
failure caused by one message. A generic HTTP status or free-form FastAPI error
body is not a versioned Avibe contract. Current source review therefore cannot
justify classifying every UDS response without additional evidence.

The phase-0 POC must record redacted public response shapes for sidecar/UDS
failure, endpoint connection failure, invalid credentials, rate limiting, an
invalid configured model, and a reproducible content-specific failure. A
production adapter may use a response mapping only when that experiment proves
it stable for the pinned runtime. Avibe instead classifies every ambiguous
response using the public sidecar health check and its own bounded,
authenticated LLM and embedding probes; no private EverOS database read is
allowed.

### 10.2 `POST /api/v1/memory/add`

Top-level request:

| Field | Type | Required/default | Constraints |
|---|---|---|---|
| `session_id` | string | Required | 1-128 characters |
| `app_id` | string | `default` | 1-128; `[a-zA-Z0-9_.@+-]+` |
| `project_id` | string | `default` | 1-128; same pattern |
| `messages` | array | Required | 1-500 items |

Each message:

| Field | Type | Required/default | Constraints/meaning |
|---|---|---|---|
| `sender_id` | string | Required | 1-128; `[a-zA-Z0-9_.@+-]+` |
| `sender_name` | string/null | `null` | Display name |
| `role` | enum | Required | `user`, `assistant`, or `tool` |
| `timestamp` | integer | Required | Positive Unix epoch milliseconds |
| `content` | string or array | Required | Plain text or typed content items |
| `tool_calls` | array/null | `null` | Assistant tool-call records |
| `tool_call_id` | string/null | `null` | Links a tool result to its call |

A typed content item accepts:

| Field | Type | Required/default | Values |
|---|---|---|---|
| `type` | enum | Required | `text`, `image`, `audio`, `doc`, `pdf`, `html`, `email` |
| `text` | string/null | `null` | Inline textual content |
| `uri` | string/null | `null` | Remote or allowed `file://` reference |
| `base64` | string/null | `null` | Inline encoded bytes |
| `ext` | string/null | `null` | File extension hint |
| `name` | string/null | `null` | Original/display name |
| `extras` | object/null | `null` | Provider-specific metadata |

Content validation requires one usable payload representation for an item.
`file://` reads are allowed from any readable path by default unless
`multimodal.file_uri_allow_dirs` is configured; this default is too broad for a
network-exposed service.

Response `data` contains `message_count` and status `accumulated` or
`extracted`. `accumulated` means messages remain in the boundary buffer; it is
not an error.

### 10.3 `POST /api/v1/memory/flush`

| Field | Type | Required/default | Constraints |
|---|---|---|---|
| `session_id` | string | Required | 1-128 characters |
| `app_id` | string | `default` | Same scope pattern as add |
| `project_id` | string | `default` | Same scope pattern as add |

Response status is `extracted` or `no_extraction`. Flush operates on the
session's buffered tail; it is not a global durability or OME-drain barrier.

### 10.4 `POST /api/v1/memory/search`

| Field | Type | Required/default | Constraints/meaning |
|---|---|---|---|
| `user_id` | string/null | XOR with `agent_id` | Exactly one owner selector required |
| `agent_id` | string/null | XOR with `user_id` | Exactly one owner selector required |
| `app_id` | string | `default` | Scope |
| `project_id` | string | `default` | Scope |
| `query` | string | Required | Non-empty |
| `method` | enum | `hybrid` | `keyword`, `vector`, `hybrid`, `agentic` |
| `top_k` | integer | `-1` | `-1` or 1-100 |
| `radius` | number/null | `null` | 0-1 distance/radius control |
| `min_score` | number/null | `null` | 0-1 minimum score |
| `include_profile` | boolean | `false` | Include current profile in user result |
| `enable_llm_rerank` | boolean | `false` | Hybrid agent-case/skill fusion only; ignored by episode hybrid and other methods |
| `filters` | object/null | `null` | Recursive filter DSL |

Filter nodes combine `AND`/`OR` arrays and scalar fields. Supported memory
fields include `session_id`, `parent_type`, `parent_id`, `timestamp`, and
`sender_id`; operators include `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, and `in`
where valid. Avibe should build filters structurally and never pass user text as
an unvalidated filter expression.

### 10.5 `POST /api/v1/memory/get`

| Field | Type | Required/default | Constraints/meaning |
|---|---|---|---|
| `user_id` | string/null | XOR with `agent_id` | Exactly one owner selector required |
| `agent_id` | string/null | XOR with `user_id` | Exactly one owner selector required |
| `app_id` | string | `default` | Scope |
| `project_id` | string | `default` | Scope |
| `memory_type` | enum | Required | `episode`, `profile`, `agent_case`, `agent_skill` |
| `page` | integer | `1` | At least 1 |
| `page_size` | integer | `20` | 1-100 |
| `sort_by` | enum | `timestamp` | `timestamp` or `updated_at` |
| `sort_order` | enum | `desc` | `asc` or `desc` |
| `filters` | object/null | `null` | Same recursive DSL |

Owner and memory kind must agree: user owners retrieve episode/profile, while
agent owners retrieve case/skill.

### 10.6 `POST /api/v1/ome/trigger`

| Field | Type | Required/default | Meaning |
|---|---|---|---|
| `name` | string | Required | Registered strategy name |
| `timeout` | number | `120.0` | Maximum wait in seconds |
| `force` | boolean | `false` | Bypass normal scheduling/gate behavior where supported |

This is an operational/debug endpoint. It is not a stable user-facing Avibe
command contract.

### 10.7 Knowledge API parameters

Knowledge is outside the MVP, but its public surface is summarized for an
accurate capability inventory:

| Operation | Parameters |
|---|---|
| Create/replace document | Multipart `file` required; `title` required/non-empty; optional `source_type`, `category_id`; `app_id`/`project_id` default to `default` |
| List documents | `app_id`, `project_id`, optional `category_id`, `page>=1`, `page_size=1..100`, `sort_by=created_at|updated_at|title`, `sort_order=asc|desc` |
| Get/delete document | `doc_id` matching `d_[a-f0-9]{12,32}` plus app/project query fields |
| Patch document | Optional `title`, optional `category_id`, app/project |
| Get topic | `topic_id` matching `d_[a-f0-9]{12,32}_<number>` plus app/project |
| Search | `query` 1-2,000 chars; `method=keyword|vector|hybrid`; `top_k=1..100`; optional `score_threshold`; `include_content=false`; app/project |
| List categories | app/project query fields |

Default maximum upload size is 50 MiB. FastAPI may buffer the multipart body
before EverOS rejects an oversized file, so a gateway body limit is required if
this surface is ever exposed.

## 11. CLI and configuration parameters

### 11.1 CLI commands

| Command | Parameters and defaults | Behavior |
|---|---|---|
| `everos init` | `--root PATH` (`~/.everos`), `--force` (off), `--print` (off) | Write `everos.toml` and `ome.toml`, overwrite, or print main template |
| `everos config show` | `--root PATH` (resolved default root) | Print effective settings with known secrets masked |
| `everos server start` | `--host` (`127.0.0.1`), `--port` (`8000`), `--root` (`~/.everos`), `--reload` (off), `--log-level` (`INFO`) | Start FastAPI/Uvicorn service |
| `everos cascade --root PATH status` | `--root` defaults to `~/.everos`; no subcommand flags | Show pending/done/failed rows and LSN lag |
| `everos cascade --root PATH sync [MD_PATH]` | `MD_PATH` optional | Force-enqueue one path, then drain queue; otherwise drain existing queue |
| `everos cascade --root PATH fix` | `--apply` (off) | List failed rows; with flag, retry retryable rows and drain |
| `everos demo` | `--plain`, `--cinematic`, `--live` (all off); `--server-url http://127.0.0.1:8000` | Static/TUI demo; `--live` calls add/flush/search on a server |

There is no first-class CLI command for memory add, memory search, item delete,
profile history, reindex-all, or flush. Integrations use HTTP. The non-live demo
is a hard-coded educational visualization and must not be treated as proof of
runtime behavior.

### 11.2 Configuration precedence

Later sources override earlier ones:

1. packaged defaults;
2. `<root>/everos.toml`;
3. `EVEROS_<SECTION>__<KEY>` environment variables;
4. programmatic initialization arguments.

`EVEROS_ROOT` selects the root and defaults to `~/.everos`.

### 11.3 Main settings

| Section/key | Default | Notes |
|---|---|---|
| `memory.timezone` | `UTC` | Sole date-bucketing timezone; OS `TZ` ignored |
| `api.host` | `127.0.0.1` | No auth; do not bind publicly without a gateway |
| `api.port` | `8000` | Server port |
| `sqlite.journal_mode` | `WAL` | SQLite durability/concurrency |
| `sqlite.synchronous` | `NORMAL` | WAL mode durability/performance tradeoff |
| `sqlite.foreign_keys` | `true` | Per-connection pragma |
| `sqlite.temp_store` | `MEMORY` | Temporary query structures |
| `sqlite.busy_timeout_ms` | `5000` | Lock wait |
| `sqlite.journal_size_limit_bytes` | `67108864` | 64 MiB |
| `sqlite.cache_size_kb` | `2048` | Per connection |
| `lancedb.read_consistency_seconds` | unset | No consistency check; `0` means strict; positive means interval |
| LanceDB index cache | 16 MiB in settings | FD/index cache budget |
| `boundary_detection.hard_token_limit` | `65536` | Boundary detector cap |
| `boundary_detection.hard_msg_limit` | `500` | Boundary detector cap |
| `memorize.mode` | `agent` | Use `chat` for user-only MVP |
| `memorize.session_lock_timeout_seconds` | `360.0` | Covers boundary plus synchronous dispatch |
| `clustering.threshold` | `0.65` | Cosine similarity threshold |
| `clustering.time_window_days` | `7.0` | Merge consideration window |
| `knowledge.max_upload_bytes` | `52428800` | 50 MiB |
| `knowledge.search.recall_n` | `200` | Candidate recall |
| `knowledge.search.rerank_n` | `50` | Rerank count |
| `knowledge.search.lambda` | `0.1` | Fusion/search parameter |
| `knowledge.search.mass_top_m` | `50` | Candidate mass |
| `knowledge.search.top_k_cap` | `100` | Output cap |

LLM, multimodal, embedding, and rerank fields are listed in section 8. Every
secret can also be supplied through its `EVEROS_*__API_KEY` environment
variable.

### 11.4 OME strategy settings

Every configurable strategy accepts:

| Key | Constraint | Meaning |
|---|---|---|
| `enabled` | boolean | Enable/disable strategy |
| `max_retries` | integer >=0 | Retry count |
| `cron` | cron string | Replace cron trigger where applicable |
| `idle_seconds` | integer >0 | Idle-trigger threshold where applicable |
| `scan_interval_seconds` | integer >0 and <= half idle interval | Idle scan period |
| `gate.threshold` | integer >0 | Counter-gate threshold |
| `gate.cooldown_seconds` | integer >=0 | Minimum time between gate fires |
| `gate.event_field` | string | Event field used to bucket/increment the counter |

Unknown strategy keys fail startup validation rather than being silently
ignored.

## 12. Comparison with other memory systems

This is an architectural comparison as of 2026-07-21, not a benchmark ranking.
The reference systems continue to change.

| Dimension | EverOS 1.1.3 | Graphiti | Mem0 OSS | Memobase | MemOS |
|---|---|---|---|---|---|
| Primary abstraction | Markdown artifacts plus derived search indexes | Temporally aware knowledge graph | Extracted memories with CRUD/search | Structured user profile and events | Multi-type memory platform/cubes |
| Canonical storage | Human-readable Markdown | Graph backend | Configurable database/vector store | Postgres plus Redis | Graph/vector/relational components depending deployment |
| Contradiction handling | LLM rewrites current profile; history remains | Facts can carry temporal validity and be invalidated without deleting provenance | Memory update/delete/history behavior, implementation/version dependent | Profile-field evolution over buffered events | Rich lifecycle/graph operations, heavier platform |
| User profile | Native explicit/implicit profile | Derivable from graph, not the same single profile artifact | Usually flat memories unless app builds a profile | Core capability with schemas | Supported within broader memory model |
| Agent procedural memory | Native cases and generated `SKILL.md` | Graph facts/episodes rather than skill packages | General memories | User-centric | Multiple memory types and tools |
| External DB required | No | Usually graph service/backend | Depends on chosen store | Yes, Postgres and Redis | Usually several services for full deployment |
| Human editability | Direct Markdown edit plus reindex | Through graph/API tools | Through CRUD/API | Through APIs/profile model | Through platform APIs/tools |
| Item-level governance | Weak for personal memory | Stronger fact/provenance model | Strong CRUD/history surface | Structured profile/event operations | Broad CRUD/lifecycle surface |
| Desktop fit | Strongest surveyed starting point | Heavier | Potentially light | Service-heavy | Heaviest |

Primary references: [Graphiti](https://github.com/getzep/graphiti),
[Mem0](https://github.com/mem0ai/mem0),
[Memobase](https://github.com/memodb-io/memobase), and
[MemOS](https://github.com/MemTensor/MemOS).

### 12.1 Real differentiators

EverOS's defensible differentiators are the combination, not any single
primitive:

1. **Inspectable Markdown is canonical for distilled memory.** The user can
   open the profile, timeline, facts, and skills without a database client.
2. **Direct edits converge into retrieval.** A durable file watcher/index queue
   makes Markdown an active control surface rather than an export format.
3. **It combines personal and procedural memory.** User episodes/profile and
   agent cases/skills share one lightweight runtime.
4. **It runs without a separate database service.** SQLite and LanceDB fit a
   desktop sidecar much better than Postgres/Redis/Neo4j/Qdrant stacks.
5. **It separates a current profile from historical evidence.** This is useful
   product behavior even though the contradiction semantics are not formally
   temporal.

These should be called differentiators, not unique inventions. Other systems
offer editable memories, temporal reasoning, profiles, or local stores in
different combinations.

### 12.2 Where alternatives are stronger

- Graphiti is stronger when temporal validity, provenance, and contradiction
  relationships must be first-class and deterministic.
- Mem0 is stronger when simple item CRUD, history, and a broad integration
  surface matter more than a native profile/episode filesystem.
- Memobase offers a more explicit structured-profile/event model but requires
  service infrastructure and has weaker desktop fit.
- MemOS offers a broader memory platform and governance model but is far beyond
  the footprint and ownership budget of this MVP.

EverOS should therefore be selected only if Markdown inspectability, profile
quality, and desktop simplicity outweigh its weaker governance and temporal
semantics.

## 13. Risks specific to Avibe

### 13.1 Open cross-project index collision

[EverOS issue #320](https://github.com/EverMind-AI/EverOS/issues/320)
reports that some LanceDB row identities omit `app_id`/`project_id`, so matching
owner/date/entry identifiers in separate projects can overwrite indexed rows.
Markdown stays separated while search/get can lose the earlier project's row.

The MVP's fixed `app_id=avibe`, `project_id=personal` avoids the second-project
precondition. It does not prove multi-project isolation. Any later workspace or
shared-memory topology must reproduce/reassess this defect first.

### 13.2 No authentication

The service defaults to `127.0.0.1` and explicitly warns that it has no auth.
Avibe must own its process, port/root, startup, and shutdown and must not expose
the EverOS port on LAN/Cloud interfaces. Loopback alone is not a user-level
authorization boundary on a multi-user machine.

### 13.3 At-least-once delivery

Avibe can deduplicate its queue by source message id, but response loss or a
crash across provider writes can cause retry ambiguity. Inspecting private
SQLite/Markdown to manufacture a receipt would couple core code to the
provider. The POC must quantify duplicate behavior; production must document
at-least-once semantics unless upstream/a fork adds an idempotency contract.

### 13.4 Hidden raw retention

Private IM and Workbench text can exist in Avibe chat history, the Avibe Memory
queue, EverOS raw SQLite, distilled Markdown, LanceDB, provider request logs,
and remote model-provider retention. Enable disclosure and whole-memory clear
must account for each layer; clearing EverOS cannot erase remote provider logs.

### 13.5 Version and interface stability

The tag/package drift, documentation drift, internal readiness dependence in
the benchmark, and rapidly changed upstream architecture increase ownership
risk. Avibe should depend only on:

- an Avibe-managed `memory-runtime` artifact containing the pinned wheel,
  compatible Python, exact dependency lock, native wheels, and manifest hashes;
- Avibe's existing `ManagedRuntimeManager` layout and activation mechanism rather
  than a Memory-specific installer or pointer format;
- loopback HTTP schemas covered by contract tests;
- a sentinel-owned root for lifecycle/clear;
- process health and public metrics/status behavior.

It should not import EverOS Python internals or parse its private SQLite tables.
Generated EverOS files should contain no API keys; the owned process can receive
them through the documented `EVEROS_*__API_KEY` environment variables.

## 14. POC implications and recommendation

The source review supports continuing with the phase-0 POC, but not skipping
it. The highest-value tests are:

1. Repeated Chinese and mixed-language profile extraction with corrections,
   explicit negations, temporary preferences, and assistant suggestions.
2. The same "like A" -> "do not like A" sequence across profile, episode,
   facts, hybrid search, and agentic search.
3. Cold start, restart, response loss, and kill points around add/flush/cascade
   to measure duplicate/loss behavior.
4. Installed bytes, idle/peak RSS, disk growth by storage layer, provider calls,
   tokens, and p50/p95 latencies.
5. Remote and fully local model combinations, including malformed JSON and
   short embedding-vector failures.
6. Whole-root clear with the process stopped, followed by proof that no EverOS
   data or index remains.
7. Contract tests generated from the pinned OpenAPI so future artifact drift is
   visible before an upgrade.
8. A clean-host packaging run proving that the exact dependency closure can be
   installed and launched as one verified target artifact without a system
   Python 3.12 or user site packages.

The production decision remains conditional:

- **Go with official 1.1.3** if quality, resource, consistency, and thin-adapter
  gates pass.
- **Use a small fork** only if a narrow idempotency/receipt or socket/auth
  contract makes the system materially easier to own.
- **Stop and evaluate the fallback** if current-profile accuracy, temporal
  correction, footprint, egress, or lifecycle behavior misses the POC gates.

## 15. Primary source index

- [PyPI everos 1.1.3](https://pypi.org/project/everos/1.1.3/)
- [PyPI everalgo-user-memory 0.3.1](https://pypi.org/project/everalgo-user-memory/0.3.1/)
- [EverOS v1.1.3 tag](https://github.com/EverMind-AI/EverOS/tree/v1.1.3)
- [1.1.3 architecture](https://github.com/EverMind-AI/EverOS/blob/v1.1.3/docs/architecture.md)
- [1.1.3 memory/storage behavior](https://github.com/EverMind-AI/EverOS/blob/v1.1.3/docs/how-memory-works.md)
- [1.1.3 API reference](https://github.com/EverMind-AI/EverOS/blob/v1.1.3/docs/api.md)
- [1.1.3 benchmark runner guide](https://github.com/EverMind-AI/EverOS/blob/v1.1.3/benchmarks/README.md)
- [EverOS/EverCore current repository](https://github.com/EverMind-AI/EverOS)
- [EverMemOS paper](https://arxiv.org/abs/2601.02163)
- [LoCoMo benchmark](https://github.com/snap-research/locomo)
- [Open cross-project collision issue](https://github.com/EverMind-AI/EverOS/issues/320)
