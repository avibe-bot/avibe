# Memory Phase 0: EverOS Provider POC

> Status: executed and archived 2026-07-23; 5 of 6 Stage-2 probes ran with real
> data, the quality gate failed, and decision A remains owner-locked
>
> Provider under test: official `everos==1.1.3`, no fork
>
> Decision source: `docs/plans/memory-mvp/memory-plugin-product-research.md`
>
> Evidence report: `docs/plans/memory-mvp/poc-stage2-report.md`

## 1. Purpose

This POC decides whether EverOS is worth integrating into Avibe's narrow Memory
MVP. The surrounding documents describe an implementation candidate; this POC
produces provider evidence before its production migrations, worker, UI, and
runtime integration are implemented or frozen.

The POC answered six questions:

1. Is the generated personal memory useful for realistic Chinese and
   mixed-language queries?
2. Does one personal pool stay coherent across separate Workbench sessions?
3. Is the runtime acceptable on a personal computer?
4. What data is retained, when does it become searchable, and what happens on
   stop/restart/clear?
5. Can the Avibe runtime launcher load the pinned package and serve the required
   operations exclusively over an owner-only Unix-domain socket without opening
   a TCP listener?
6. Can Avibe integrate through a thin public provider interface, or would it
   need to depend on EverOS internals?

The POC is not a production crash-recovery certification. It characterizes
duplicate behavior and failure modes so the product can choose a contract.

Plain-language decision: if the candidate cannot produce useful memories, fit
on a personal computer, run through Avibe's local-only launcher, and survive the
basic retry/restart tests below, Avibe does not integrate it.

## 2. Isolation and safety

Harness and corpus source code live in the repository under
`scripts/memory_poc/` and are reviewed like any other change. The gitignored
`.runtime/memory-poc/` directory holds only local run state. `.env.poc` is read
from the current worktree first, then the primary checkout. Each run owns:

```text
.runtime/memory-poc/
├── env/                         # pinned Python 3.12 environment
├── .env.poc                     # mode 0600; never committed
└── runs/<run-id>/
    ├── everos-root/             # fresh root for this run only
    ├── report.json
    └── logs/                    # redacted measurements, no message bodies/keys
```

Rules:

- Never use `~/.avibe`, `~/.everos`, a running Avibe sidecar, or real chat data.
- The harness starts and owns the provider process and always terminates it.
- Every run uses a fresh provider root.
- `.env.poc` contains explicit LLM and embedding endpoint blocks. One credential
  may serve both only when the provider supports both operations.
- The harness never prints keys, authorization headers, full endpoint URLs, or
  fixture message bodies.
- Network recording or an Incus default-deny environment verifies destinations.
  It must not inspect or modify the user's normal proxy, keychain, browser, or
  Avibe configuration.

## 3. Fixed provider configuration

The run records the exact package lock, Python version, machine class, OS,
model names, endpoint locality, timezone, and harness commit.

Use the MVP mapping:

| Concept | EverOS value |
|---|---|
| Application | `avibe` |
| Project | `personal` |
| Owner | one synthetic UUID for the run |
| Session | deterministic synthetic session id |
| Mode | chat / user-memory track only |

Reflection, agent-memory tracks, reranking, multimodal inputs, file ingestion,
foresight reads, and manual Markdown editing are not part of this POC.

The proposed initial targets are `darwin-arm64`, `darwin-x64`, `linux-arm64`, and
`linux-x64`, matching Avibe's existing managed-runtime platform vocabulary. A
target enters the release manifest only when its own clean-host artifact test
passes; an untested or failed target is explicitly unsupported. Building these
prototype artifacts is provider-selection evidence, not production rollout.

## 4. Fixture corpus

Use synthetic fixtures only. The corpus is versioned with the harness and
contains at least:

- 30 predeclared Chinese or mixed Chinese/English positive queries;
- negative queries for unrelated facts and stale assertions;
- temporal corrections such as an old database choice replaced by a new one;
- stable preferences, goals, dates, and short episodic events;
- two sessions for the same principal in the fixed `personal` project;
- input that should remain buffered until an explicit flush;
- one response-loss or process-kill case used to characterize duplicates.

Expected query results are declared before a run. A failed request or timeout is
not counted as an empty successful result.

## 5. Experiments

### 5.1 API and storage sanity

On a clean root and owner-only socket directory, start the pinned package through
the Avibe-owned runtime launcher. The launcher may load the pinned
`everos.entrypoints.api.app:create_app` application factory and pass it to
uvicorn with a Unix socket. The production controller does not import EverOS.

Then:

1. start EverOS bound only to a Unix-domain socket and prove that it opens no
   TCP listener;
2. add a short user conversation with required sender and timestamp fields;
3. flush the session;
4. query profile, episodes, and facts through supported provider routes;
5. confirm the visible Markdown tree and hidden SQLite state exist where the
   provider documents them;
6. stop and restart the provider, then repeat the reads.

The report records the observed HTTP shapes. It does not convert private SQLite
tables into an Avibe contract.

### 5.2 Quality and temporal behavior

Run the complete corpus from a clean root three times. For each query record:

- expected memory identity;
- returned top-8 identities and kinds;
- rank of the expected item;
- latency;
- whether stale information outranked a declared correction;
- whether an unrelated negative item appeared.

Every scored run must reproduce the proposed production write pattern: one
message is added and explicitly flushed before the next queue item is handled.
The POC may also run a session-batched comparison, but only the production-shaped
run determines whether the quality gate passes.

LLM prose is evaluated only where the provider does not expose a stable item
identity. Leakage and temporal-critical assertions remain deterministic.

### 5.3 Personal-pool behavior

Inside the fixed `personal` project:

- seed two sessions for one principal with deliberately distinct facts;
- query the global personal pool from a third session;
- require the expected cross-session memory and reject unrelated negatives;
- restart and repeat the same queries.

The POC does not create a second project or workspace. Issue #320 belongs to the
future topology decision that introduces that precondition.

### 5.4 Buffer, flush, restart, and duplicate characterization

Exercise the normal lifecycle through public operations:

1. add a non-boundary message and verify it is not yet presented as a completed
   episode;
2. flush and measure time until it becomes searchable;
3. stop after a successful add, restart, flush, and query;
4. lose one add response or kill the process around the response, then retry the
   original request once;
5. count resulting logical duplicate facts/episodes;
6. add two messages with identical millisecond timestamps in one session and
   verify both survive as distinct memories after flush (EverOS derives message
   ids from session, timestamp, and request index);
7. while one add or flush call is still executing, or immediately after a
   deliberately timed-out call, issue one bounded retry and record how the
   provider's session lock serializes or rejects it and which error shape the
   caller sees;
8. separately trigger a sidecar/UDS failure, endpoint connection failure,
   invalid credential, rate limit, invalid configured model, and a reproducible
   content-specific processing failure. For each case, record the public HTTP
   status, any stable closed code, and the redacted response schema visible over
   the UDS. State whether the pinned public response alone distinguishes a
   system failure from a message failure; do not treat free-form error prose as
   a stable contract.

The result determines whether at-least-once delivery is acceptable. The harness
does not implement `WriteEvidence`, repair fences, per-message recovery, or an
exactly-once algorithm. If public operations cannot characterize the outcome,
that is evidence for a fork or provider change.

### 5.5 Retention and full clear

Observe and report:

- unflushed raw-message retention;
- extracted raw MemCell retention;
- visible profile/episode/fact files;
- whether restart preserves each form;
- whether deleting the isolated sentinel-owned provider root removes every
  local provider copy created by the run;
- which copies remain outside that root by design (the synthetic input fixture,
  harness report, and any remote model-provider retention).

This is the one place where research-only code may perform version-pinned,
read-only inspection of the isolated provider root to verify a retention claim.
That inspector is not imported by production code and is not used to decide
delivery success. Production integration remains limited to public provider
operations plus sentinel-owned full-root deletion.

Do not test item-level deletion because it is not an MVP capability.

### 5.6 Desktop footprint, latency, and egress

Measure on the recorded machine:

- locked environment size;
- warm idle RSS over 10 minutes;
- peak sidecar RSS during the fixed corpus;
- provider-root growth;
- add and flush latency;
- write-to-searchable latency;
- direct query p50 and p95;
- LLM and embedding request counts;
- every network destination attempted by the child.

Provider-authoritative token usage is recorded when available. Estimates are
labeled as estimates and are not treated as billed usage.

## 6. Pass criteria

EverOS may proceed to MVP integration only when:

- every critical temporal-correction assertion passes;
- every unrelated-negative assertion passes in all three clean runs;
- at least 90% of positive queries return the expected item in the top 8 in
  each clean run;
- direct query p95 is at most 2 seconds;
- flushed content becomes searchable within 5 minutes for at least 95% of the
  fixed fixtures;
- environment size is at most 1 GiB;
- the exact dependency closure has compatible wheels for every proposed initial
  target and can be launched from an isolated artifact without user site
  packages or a host Python 3.12;
- that archive, manifest, executable path, and smoke test fit Avibe's existing
  `ManagedRuntimeManager` schema without a Memory-specific installer or active
  pointer format;
- warm idle RSS p95 is at most 512 MiB;
- peak RSS is at most 1.5 GiB;
- provider-root growth is at most 512 MiB for the fixed workload;
- the recorded egress set contains only the configured processing destinations
  and system DNS required to resolve their configured hostnames;
- an all-loopback endpoint run attempts no external connection;
- the Avibe runtime launcher loads the pinned official package and completes
  every required provider operation through an owner-only Unix-domain socket
  while opening no TCP listener;
- stop/restart preserves queryable memory;
- full-root clear removes all provider-owned local state in the isolated root;
- the required MVP operations can be implemented without core Avibe code
  importing EverOS or reading private EverOS SQLite schemas; the versioned
  runtime launcher is the documented exception for loading the pinned ASGI
  application factory;
- provider failures are either distinguishable through tested public response
  fields or can be classified with the public sidecar health check plus the two
  Avibe-owned authenticated endpoint probes, without provider-private reads.

The duplicate experiment has no invented statistical threshold. The report must
state the exact observed outcome and its product consequence. One duplicate
after one deliberately replayed uncertain request is compatible with the MVP's
at-least-once contract and is not an automatic failure. Prefer a fork or another
provider only when the observed pattern materially damages memory quality,
makes an ordinary bounded retry unusable, causes silent data loss, or leaves
normal ingestion impossible to interpret through public operations.

## 7. Report contract

Each run writes a redacted `report.json` and a short Markdown summary containing:

- environment and model identities without secrets;
- corpus revision and run seed;
- pass/fail for every criterion;
- quality and latency tables;
- RSS, disk, request-count, and egress measurements;
- observed raw-retention locations and clear result;
- duplicate/restart observations;
- a redacted public-error classification matrix and every case that requires
  sidecar and model-endpoint probes to decide the result;
- unexpected behavior and reproducible steps;
- exactly one recommendation: official integration, small Avibe fork, or stop
  and evaluate the next candidate.

The report may not claim production readiness. Production acceptance belongs to
the technical design and tests built after this provider decision.

## 8. Archived outcome

- Completed: hermetic Python 3.12 environment and lock, process-owning harness,
  versioned corpus, UDS/restart probes, quality probes, error-shape recording,
  resource/latency/request/egress measurements, and the evidence report.
- Partial: duplicate, personal-pool, retention/full-clear, and full-footprint
  characterization. The provider failed before these probes completed.
- Not completed: a relocatable managed artifact, clean-host verification, and
  packaging in the `ManagedRuntimeManager` archive shape. These remain release
  work rather than evidence supplied by this POC.

This harness is archived rather than maintained as a standing test suite. Rerun
it when the provider, provider version, LLM, or embedding model changes, or when
a release decision explicitly asks for refreshed evidence.

## 9. Explicit non-goals

This POC does not test or design:

- Avibe database migrations or queues;
- all-platform capture;
- automatic recall or agent backend injection;
- group, Cloud, or network authorization;
- item deletion, export/import, or provider migration;
- exact power-loss durability or filesystem support matrices;
- production retry reconciliation;
- UI behavior;
- future provider-neutral abstractions.

Those are separate decisions after the provider demonstrates product value.
