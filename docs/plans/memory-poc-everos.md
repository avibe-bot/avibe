# Memory POC Phase 1: EverOS Evaluation

> Status: sandbox scaffolded; provider probes are blocked on (a) rev36 harness
> contract fixes below and (b) model endpoint credentials. The production
> duplicate gate additionally waits for slice-2 worker + slice-3 EverOS adapter
> code. The POC has not been run and does not block slice 1.
> Parent: `docs/plans/memory-plugin-product-research.md` (v3, 2026-07-20)

## Background

The provider research selected official `everos==1.1.3` for phase 1, with no
fork; Memobase/MemOS remain phase-2 comparison tracks and Mem0 OSS the fallback.
EverOS runs first: lightest desktop runtime, memory model closest to the product goal
(inspectable profile/episode Markdown plus framework-internal dot-prefixed fact/
foresight Markdown), with two known
work items priced in — the #320 cross-project index collision and the missing
user-memory delete surface.

## Goal

Produce evidence, not impressions, for the acceptance gates in research doc
section 9: Chinese extraction/retrieval quality, principal + logical-scope
isolation in the fixed production project, #320 cross-project characterization,
deletion-gap documentation, supported-filesystem/durability behavior, desktop footprint
(idle RSS, disk), and write-to-searchable latency — using synthetic fixtures
in a fully isolated sandbox.

## Solution

Sandbox at `.runtime/memory-poc/` (gitignored, hermetic):

- `venv-everos/`: dedicated Python 3.12 env, `everos==1.1.3` pinned
- `runs/<run-id>/everos-root/`: a fresh isolated `EVEROS_ROOT` created and
  owned by each harness invocation; never reuse the scaffold root or `~/.everos`
- `.env.poc`: separate provider-agnostic **LLM and embedding** endpoint blocks
  (each has an OpenAI-compatible key/base_url/model; they may deliberately reuse
  one provider credential, but neither block is implicit) + POC-pinned settings
  (`chat` mode, Asia/Shanghai timezone). File mode must be `0600`; the harness
  refuses broader permissions before loading a key
- acceptance harness: launches the sidecar through the production UDS manager on
  a fixed, derived Unix-domain socket (no TCP host/port), spawns and owns the
  sidecar child, verifies that PID owns the socket, and terminates it in
  `finally`. `run_server.sh` may remain a manual debugging helper but is never
  used by a gate
- `harness/poc_harness.py`: `seed` (2 users x 2 projects zh/en fixtures,
  including a same-user-same-day cross-project #320 trigger and a
  MySQL→PostgreSQL temporal contradiction), `probe` (retrieval gates with
  production-adapter session filter + post-filter leakage negatives),
  `isolation` (same-project principal gate + separate #320 characterization),
  `delete-gap` (documents missing delete routes), `footprint` (RSS/disk)

Constraint honored from this session: Avibe's configured agent providers
(Claude OAuth / Codex subscription / OpenCode Google OAuth) cannot serve the
extraction pipeline — no OpenAI-compatible API key exists in Avibe today. The
memory feature therefore needs its own model endpoint config
(`MemoryConfig.processing`) regardless of provider choice.

## Todo

- [ ] Fix harness API contract (reviewer pass 2026-07-19): every fixture
      message must carry required `timestamp` (epoch **ms**) and
      `sender_id` (assistant items included) — current seed would 422
      regardless of credentials; drop foresight `/search`//`/get` probes
      (foresight is not exposed by either API — validate the Markdown
      file-read path instead: `<root>/<app>/<project>/users/<principal>/`
      `.foresights/foresight-<YYYY-MM-DD>.md` — no `data/` component
      (third-review correction), session_id is per-entry metadata)
- [ ] Harness isolation (third/fourth review 2026-07-20): the harness
      **spawns and owns its own sidecar instance** (own Unix-domain socket, own
      EVEROS_ROOT, holds the child PID and refuses to talk to any server
      it did not spawn — `/health` returns only `status:ok`, so identity
      cannot be verified over HTTP); never reuses a well-known socket path or any
      running instance. The zero-non-allowlisted-egress gate must run the child inside an
      enforceable default-deny Linux network namespace/container (the local
      Incus regression environment is acceptable): for production-contract runs,
      the EverOS child may reach only the Avibe processing relay and the relay may
      reach only the declared model endpoints; every other socket is
      blocked and counted. Proxy environment variables or passive host capture
      alone are diagnostic, not gate evidence, because a library can bypass
      them. Pass = zero attempted non-allowlisted connections; when both declared
      model endpoints are loopback this additionally means zero external egress
- [ ] Effective-home and process-environment guard (rev27; credential-free): set
      a nondefault hermetic `AVIBE_HOME` and the supported legacy-home case, then
      drive representative enable/capture/export/clear/backup-discovery paths
      through production helpers. No open/create may occur below the real default
      home. Seed upper/lowercase proxy variables, proxy-autoconfig values,
      unrelated provider keys, generic OpenAI credentials, and hostile `EVEROS_*`
      values plus `SSL_CERT_FILE`/`SSL_CERT_DIR`/Requests/cURL CA overrides in the
      parent. The Memory child must receive only the minimal reviewed
      environment, dedicated empty owner-only `HOME`, exact generated settings,
      runtime-default CA trust, and only per-boot relay URLs/tokens; real endpoint
      URLs/keys remain controller-only. Controller
      sidecar and direct LLM/embedding probe clients must use `trust_env=false`,
      reject redirects/CA overrides, and contact only the verified loopback or
      exact configured endpoint. Prove installed EverOS/OpenAI would follow a
      provider redirect when direct, then prove the production path makes that
      impossible: the child knows only the relay and every provider 3xx terminates
      there. The default-deny network harness must observe no hostile-proxy or
      undeclared destination attempt. Restart the controller twice and require a
      different route/token set each time; before each sidecar/canary launch,
      generated `everos.toml` must be atomically rewritten/fsynced, expose only
      the current relay material, and reject the preserved prior-boot token
- [ ] Text-only runtime capability (credential-free): build from the production
      base-distribution lock and prove the multimodal extra, `everalgo_parser`,
      cairosvg/LibreOffice integration, and any third model route are absent.
      Send image/doc/pdf/audio/file-URI payloads directly to the owned sidecar and
      require capability-unavailable before any file open, subprocess, parser/
      model invocation, or relay request. Keep the exact empty owner-only
      `file_uri_allow_dirs` staging allowlist effective as defense in depth
- [ ] Platform/filesystem gate (rev22; credential-free and run before every
      model probe): invoke the production capability adapter, not a POC copy, on
      each advertised Darwin/APFS, native-Linux/ext4|XFS|Btrfs, and WSL2/ext4
      pair. Record OS, kernel,
      filesystem/mount identity, and adapter contract version. Prove native
      Windows, HFS+, ZFS, overlayfs, tmpfs, WSL DrvFS (`/mnt/<drive>`),
      network/FUSE/read-only/unknown mounts,
      and every missing UID/mode, dirfd/no-follow, lock, regular/parent fsync,
      atomic replace, or no-replace primitive fail before identity/key creation,
      sidecar spawn, DNS, or model traffic. A synthetic fake is unit-test evidence
      only; a missing advertised platform/filesystem run fails the release gate.
      Start the production child from a parent process with a permissive umask;
      assert child-created provider directories are at most `0700`, every SQLite/
      WAL/SHM/config/Markdown file is at most `0600`, and the parent umask is
      unchanged. The sidecar Unix-domain socket's `0600` mode must **not** rely
      on umask (finding 6, rev30): installed uvicorn hard-codes
      `uds_perms=0o666` for a newly-created socket (`server.py:156,162`),
      overriding umask, while preserving a pre-existing socket's mode
      (`server.py:157`). Assert the controller creates the socket `0600`
      pre-bind inside the `0700` directory and, post-bind and before any health
      check/admission, `lstat`s the socket, verifies owner + mode `0600` with an
      explicit `chmod 0600` fallback when uvicorn created it fresh at `0666`, and
      fails startup (no admission) if `0600` cannot be established. Add a
      **socket path-length preflight** test (finding 6, rev32; off-by-one fixed
      finding 8, rev33): the sidecar binds
      a short, bounded `<AVIBE_HOME>/memory/.rt/s<8-hex-of-root-hash>.sock` under
      a `0700` runtime directory rather than the deep `everos-root/sidecar.sock`;
      the preflight rule is `len(os.fsencode(path)) + 1 <= sizeof(sun_path)`
      because `sizeof(sun_path)` (**104** Darwin / **108** Linux) INCLUDES the NUL
      terminator, so the max pathname is **103** (Darwin) / **107** (Linux). Assert
      the preflight ACCEPTS a path of exactly 103 (Darwin) / 107 (Linux) bytes and
      REJECTS one of 104 (Darwin) / 108 (Linux) bytes; add a **non-ASCII** case
      whose `os.fsencode` is multibyte (so byte length, not character count, is
      what the preflight measures);
      set a long hermetic `AVIBE_HOME` that would overflow the deep path and prove
      the short `.rt/` path still binds, and prove a pathologically deep home that
      overflows even the short name fails enablement closed with
      `memory_socket_path_too_long` and no admission
- [ ] Provider commit-durability gate (rev22; real production worker/adapter):
      assert effective WAL + `synchronous=FULL` on every Avibe and EverOS
      connection that owns the handoff. Fault after a successful `/add|flush`
      response and at every local status-commit/`commit_write` step. Assert the
      exact non-interchangeable response unions: `/add` is
      `accumulated|extracted`, `/flush` is `extracted|no_extraction`, and all
      other/cross-endpoint values fail schema validation. **Finding 2 (rev29)**:
      `status="extracted"` alone is not evidence of a durable episode — installed
      `UserMemoryPipeline.run()` returns that status unconditionally even when
      every cell in the call was assistant-only and zero episodes were written
      (`user_senders` empty skips the cell, but the trailing `PipelineOutcome`
      construction is unconditional), and neither `AddResponseData` nor
      `FlushResponseData` carries any evidence field on the wire. The gate must
      therefore include a dedicated assistant-only-memcell fixture (a call whose
      only content has no `role="user"` sender) and assert the worker
      independently confirms episode-backed evidence via the pinned internal
      SQLite/Markdown check before treating that `extracted` response as more
      than acceptance. Every accepted outcome
      must follow the validated FULL system-DB commit and bottom-up no-follow
      fsync of `.index/sqlite` through the provider root. `extracted` must
      additionally follow bottom-up fsync of the deterministic episode chain;
      installed 1.1.3's temp-file fsync + replace alone is not a pass. Exercise
      explicit remember's add and flush barriers separately; `no_extraction`
      cannot complete it without episode-backed coverage. Kill/restart around
      every edge and prove intact provider evidence or a persisted-2xx outcome
      causes only barrier retry, never a blind re-mutation (a stable-zero
      `ordinary_add`/`explicit_operation` result is the single replay-once
      exception, while a stable-zero `ordinary_flush` is dead, per finding 3 below
      and §4.2's work_kind-keyed matrix). Wrong-owner,
      symlink/type, missing path, and injected fsync errors must enter
      `durability_blocked`, preserve payload/status/stage through every 14-day
      sweeper while consuming row/byte/session caps, and pause admission at
      capacity. **Finding 3 (rev29)**: repair of a `durability_blocked` row —
      including one recovered across a real process restart, not only within
      the same process — must first re-run the full evidence reconciliation and
      apply §4.2's work_kind-keyed matrix (a `full`+`episode` result commits
      barrier-only without replay for any work_kind; a `full`+`buffered`|`mixed`
      `ordinary_flush`/`explicit_operation` instead needs one fenced flush; a
      stable-zero result allows exactly one safe replay only for an
      `ordinary_add`/`explicit_operation` and is dead for an `ordinary_flush`;
      partial/contradictory coverage is dead) before
      choosing a repair path; a bare "retry the barrier, clear the row"
      path is never sufficient on its own after a restart, since a barrier
      retry only proves current disk state is durable, not that it matches the
      pre-restart write. Clear-all remains the only discard. Report that
      sudden-power recovery remains
      conditional on storage honoring fsync; do not claim media-corruption or
      lying-cache coverage from process-kill tests
- [ ] Provider-mutation retry/cost bound (production worker, fake endpoints):
      force evidence-safe failures and advance exactly 30s/2m/10m/1h; assert five
      Avibe add/flush attempts then ordinary dead and no sixth background call.
      Count EverOS SDK-internal attempts separately. A private owner drain may
      open one new cycle only while outbox/operation payload remains and fresh
      evidence is stably zero, or while a flush tail is proved. Partial/changing/
      orphan evidence and expired payload must never re-enter a provider
      mutation; `durability_blocked` work is not re-armed through this
      owner-drain path either — it is repaired only through §4.2's work_kind-keyed
      durability matrix (finding 2, rev30; endpoint-aware rev31; work_kind-keyed
      rev32): barrier-only for a `full`+`episode` row or a `full`+`buffered`|`mixed`
      `ordinary_add`; one fenced flush after prior-call death is proved for a
      `full`+`buffered`|`mixed` `ordinary_flush`/`explicit_operation`; exactly one
      automatic fenced replay only for an `ordinary_add`/`explicit_operation` row
      proven stable-zero; a stable-zero `ordinary_flush` and all
      partial/changing/orphan/unreadable evidence dead
- [ ] Hostile model-I/O containment: through the production relay, exercise exact/
      over-8-MiB streamed requests and decoded responses, missing/false lengths,
      compressed responses, wrong/stale tokens, wrong method/path, provider 3xx,
      relay restart/loss, and 16/17 concurrent calls. The child must never receive
      provider credentials, redirects, raw provider errors, or a partial oversized
      response; the relay must never fall back to a direct path. Separately grow
      the profile input and return an in-bound but adversarial completion through
      the real pinned EverOS client (its factory supplies no default `max_tokens`).
      Record sidecar/controller peak RSS/exit, prove chat remains live, preserve
      acceptance uncertainty, and enter the same five-attempt backoff/dead path
      without raw request/response logging. Transport bounds must not be mislabeled
      as a portable hard sidecar RSS or billed-token bound
- [ ] Make the harness **fail-closed** (fourth review): non-2xx responses
      raise instead of print; negative probes must distinguish
      "empty result" (pass) from "error response" (fail); process exits
      non-zero on any gate failure
- [ ] Duplicate-rate gate as a fail-closed production-hook evidence-sequence
      matrix (rev6; rewritten rev30, finding 5):
      run this **through the implemented production Memory worker and EverOS
      adapter with a test-only fault + evidence-injection hook**, not through a
      second recovery-state machine copied into the POC. Before those slices exist, the sandbox may
      only characterize blind replay (expected to append a fresh random memcell
      and episode in pinned 1.1.3) and validate the exact evidence oracle.
      For the release run, use ≥500 delivered synthetic turns. Predeclare the
      deterministic 10% fault
      indices from the run seed before sending anything (≥50 faulted turns), and
      make those fixtures boundary-shaped so `/add` itself returns
      `status="extracted"`. Do not replace a selected index after seeing its
      response: if any selected turn is not extracted, the dangerous-window
      stratum is unexercised and the run fails. Each trial begins from one of
      **three production-reachable setups**, not a single post-`/add` crash
      (finding 4, rev33): **(a)** an `ordinary_add` faulted right after its `/add`
      and before the delivered transaction; **(b)** a **scheduled `ordinary_flush`**
      row faulted at its due flush; **(c)** an `explicit_operation` **remember**
      (a real `/add`+`/flush`) faulted mid-operation. Each setup creates the real
      row of its `work_kind` through the production path. The harness injects
      **only storage/timing faults — fsync failure, process kill, delayed/withheld
      evidence — and NEVER injects a `work_kind` or a finished `WriteEvidence`**;
      the real slice-3 disk classifier (`inspect_write_evidence`) must run against
      actual `.index/sqlite`/Markdown state so the reconciler's branch is
      *derived from disk*, not planted. **The gate must
      NOT confirm episode-backed coverage before every kill — doing so forces
      every trial down the full-evidence/no-replay branch and never exercises the
      other reconciliation decisions (finding 5, rev30).** The lease is reclaimed
      through the production §4.2 reconciler and its `repair_stage` fence, and each
      dangerous-window trial asserts the resulting
      provider-mutation count and lineage:
        - **ordinary_add stable-zero** (an `ordinary_add`/`add_explicit` row, no
          buffer, no memcell, no episode): the reconciler
          must replay **exactly once** — assert exactly one additional provider
          mutation and that a single episode lineage appears **only after** that
          replay. This false stable-zero after the
          settle interval is the residual duplicate window the tech contract
          names (§4.2), so it is the primary duplicate hazard and is seeded
          independently in each such trial;
        - **ordinary_flush stable-zero** (an `ordinary_flush` row whose buffered
          tail proves stably gone — no buffer, no memcell, no episode): the
          reconciler must mark the row `dead` — assert **no** lineage, **zero**
          re-mutations, and a dead row, because Avibe keeps no flush replay
          capsule and the lost tail is unrecoverable (findings 1+3+4, rev32);
        - **explicit_operation stable-zero** (a `memory_operations` row with no
          buffer, memcell, or episode but retained add+flush `payload_json`): the
          reconciler must replay the retained payload **exactly once**, and because
          authoritative explicit replay is `/add`+`/flush`, assert **`add=1,
          flush=1` (two provider mutations)** — not one (finding 4, rev33) — each
          gated by its `repair_stage` CAS, with a single episode
          lineage appearing **only after** that replay;
        - **full-buffered** (the batch is exactly present in `unprocessed_buffer`
          with **zero** episode lineage): assert exact `unprocessed_buffer`
          membership for the batch AND zero episode lineage; for an `ordinary_add`
          expect barrier-only — **zero** re-mutations and no replay of the add;
          for an `ordinary_flush`/`explicit_operation` expect exactly **one**
          fenced flush after prior-call death is proved — one flush mutation and
          no re-`add`;
        - **full-episode** (an episode-backed memcell covering the batch): the
          reconciler reuses the existing lineage barrier-only — assert exactly one
          existing episode lineage and **zero** re-mutations;
        - **full-mixed** (some expected ids episode-materialized, the rest still
          buffered): for an `ordinary_add` barrier-only — **zero** re-mutations;
          for an `ordinary_flush`/`explicit_operation` exactly **one** fenced
          flush after prior-call death is proved — one flush mutation and no
          re-`add` — because a buffered remainder is not proof the flush ran
          (finding 4, rev33);
        - **unreadable** (the disk evidence read itself fails, or the observation
          differs across the two stable-read snapshots): the row must become
          `dead` — assert **zero** provider mutations and **no new** lineage,
          dead-safe (finding 4, rev33);
        - **partial** (subset coverage) and **orphan** (memcell present with no
          backing episode): the row must become `dead` — assert exactly the
          on-disk evidence, **zero** provider mutations, and **no new** lineage;
        - **repair-fence double-crash** (finding 1, rev33; stage-specific
          resolution finding 2, rev34): exercise **two** crash points on the
          fenced stages. **(i) crash-after-response-before-commit**: on a
          stable-zero replay and on a full+buffered|mixed repair `/flush`, fault
          **again** after the repair mutation's response returns but **before** its
          local commit (so `add_repair`/`flush_repair` is at `issued`, not
          `resolved`). **(ii) crash-after-CAS-before-send**: fault after the
          `unused→issued` CAS commits but **before** the socket call, leaving the
          original pre-call evidence intact. In both, restart and re-run recovery
          and assert the fenced stage is acceptance-uncertain with **NO second
          provider mutation**, resolved **stage-specifically**: `add_repair=issued`
          + `full` (any materialization) resolves via the non-mutating barrier to
          `resolved`; `flush_repair=issued` resolves via the barrier ONLY on
          `full`+`episode`, while `flush_repair=issued` + `full`+`buffered`|`mixed`
          resolves to `dead`/uncertain with **no** second mutation (a remaining
          buffer is unchanged pre-call state, not proof the flush was sent);
          anything else marks the row `dead` — proving recovery mutations are
          exactly-once across crashes.
        - **two-batch affected-source set** (finding 1, rev34): buffer batch A and
          clear its outbox payload (A's earlier `/add` returned `accumulated`, so A's
          outbox row is `awaiting_flush`, not `delivered`; finding 1, rev36),
          then issue a later `/add` of batch B whose merged call extracts A into a
          memcell while leaving B as the new tail. Inject an A-episode-write
          failure plus power loss. Assert A is owned by the retained tail-recovery
          row (the most-recent `/add` covering the affected ids via
          `affected_source_ids_json`) and is recovered or terminally classified
          `dead` — **never** a stranded memcell-only orphan — and assert B is
          accepted as `full`+`buffered` **without** falsely demanding
          `full`+`episode` for the current batch.
        - **initial-call crash-before-send** (finding 1, rev35): buffer source A,
          then create B's outbox row whose `affected_source_ids_json` snapshots
          `{A,B}`, and fault the process **before** the `/add` socket call fires,
          so the disk evidence is `per_message={A:buffered, B:absent}` (aggregate
          `partial`). Restart and assert this does **not** wholly kill the row:
          B takes exactly **one** fenced exact current-payload replay (gated by the
          `add_repair` CAS `unused→issued`) — assert exactly **one** provider
          mutation, no re-`add` of A — and B becomes `buffered_pending` (durable
          buffer membership) alongside A, so the owning outbox row is held
          **`awaiting_flush`** (payload cleared, NOT `delivered`), proving aggregate
          `partial` is resolved per-message rather than falsely dead.
        - **per-message `recovery_state` ledger** (finding 1 rev35, finding 2 rev36):
          after the A-episode-write-failure two-batch case above, assert the exact local
          `memory_sources.per_message_recovery_json` + derived `recovery_state`
          persisted values — `A='orphan_dead'`
          (prior source terminalized alone) and `B='buffered_pending'` (healthy
          pending tail, owning outbox row held `awaiting_flush` — NOT wholly dead and
          NOT yet `delivered`) — then run the flush transaction on B's
          tail and assert `B` advances to `'episode_backed'` and the owning outbox
          row reaches `delivered`. This proves the per-message outcome and its derived
          source rollup are actually
          persisted in the local ledger, not merely asserted abstractly over
          provider lineage.
        - **split-turn per-message disposition** (finding 2, rev36): send ONE turn
          (user + assistant messages) whose EverOS boundary distills the user message
          into an episode while leaving the assistant message in the buffer, so a
          single `source_id` has `per_message={U:episode, A:buffered}`. Assert the
          persisted `per_message_recovery_json` is exactly
          `{U:'episode_backed', A:'buffered_pending'}`, the derived
          `memory_sources.recovery_state` rolls up to `buffered_pending`, and the
          owning outbox row is held `awaiting_flush` (NOT `delivered`). Then run the
          flush transaction and assert `A` advances to `'episode_backed'`, the rollup
          becomes `episode_backed`, and the outbox row reaches `delivered` — a case a
          single source-level verdict could not have represented.
      Do **not** manufacture the window with an external `/flush` that production
      could not schedule before the local commit. Then flush remaining tails and
      settle all OME/cascade work.
      The oracle is provider lineage, not LLM text: EverOS deterministically
      derives message ids from `(session_id,timestamp,index)`, and
      `.index/sqlite/system.db:memcell.message_ids_json` records them; the oracle
      counts a memcell only when an episode entry references its id as
      `parent_id`. A logical
      duplicate is one seeded user message id referenced by >1 distinct
      extracted memcell/episode lineage. A zero-fault baseline must observe
      exactly one lineage for every seed. Lineage expectations are
      **branch-specific**, not global: a full-buffered trial legitimately has
      zero episode lineage, and an ordinary_flush-stable-zero trial legitimately
      has no lineage and a dead row, so each faulted trial is judged only against
      its assigned branch's mutation-count + lineage assertions above. Fewer than
      50 confirmed dangerous-window faults, an unexercised branch, a per-branch
      mutation-count/lineage mismatch, any non-2xx, or any gate exception
      fails the run. Report both conditional duplicates/faulted turns and
      overall duplicates/delivered turns as descriptive context. **Finding 4
      (rev29) release criterion**: this is a deterministic recovery-coverage
      gate, not a statistical production-rate estimate — the ≥50 faulted
      turns are drawn from a predeclared, non-random fault schedule (not an
      i.i.d. sample of production traffic), so a Clopper–Pearson interval
      over the diluted 500-turn denominator does not bound any real
      production duplicate rate, and diluting with the ~450 unfaulted turns
      hides the risk that actually matters (0/500 reads as ~0.60% while the
      honest conditional rate over the 50 faulted turns alone is the figure
      that reflects dangerous-window behavior, e.g. ~5.82% if 3/50 duplicate).
      The gate therefore passes only when **zero duplicates are observed
      across all ≥50 independently-seeded dangerous-window trials spanning every
      evidence branch above**, run
      through the real slice-2/3 worker and adapter with the fault/evidence-
      injection hook above — not a POC-only copy of the recovery logic. Any duplicate
      in any faulted trial fails the gate outright; there is no confidence-
      interval threshold to clear. The 500-turn/50-faulted-trial run size and
      the Clopper–Pearson formula above are retained only as supporting
      methodology for reporting conditional-vs-overall context, not as the
      pass/fail criterion
- [ ] Add probes: Plan-A `session_id eq` behavior over the full per-run-keyed
      digest-form
      session ref using each frozen `wb/sl/dc/tg/fs/wc` short code; assert every
      ref is path-safe and <=128 bytes, and reject an unknown platform rather than
      embedding it. Add mandatory post-filter negatives (including two sessions
      in one group scope). Inject search/get results with foreign/NULL principal,
      app, project, agent track, malformed/old-epoch/unknown source session, and a
      source-less non-profile item; the production adapter must drop them and
      return empty recall, while a valid current-epoch `memory_sources` member
      survives. Make the source-ledger read fail and require a closed explicit
      error/empty recall rather than trusting provider output. Prove per-fact
      scope integrity with the real 1.1.3 addressing (finding 3, rev32; finding 7,
      rev33): the returned HTTP fact `id` is the composite `f"{owner_id}_{entry_id}"`
      (`cascade/handlers/atomic_fact.py:51`) while the fact's `.atomic_facts` entry
      stores a **bare** `parent_id=ep_...` (`extract_atomic_facts.py:98`), so the
      adapter must require the id to start with the exact principal prefix, parse
      the trailing token as an `EntryId` requiring the `af` prefix, derive the
      fact's OWN dated daily-file path from **`EntryId.date`** (the `af_YYYYMMDD`
      in the id), **NOT** from any timestamp, resolve that entry, and validate its
      date/inline timestamp plus `parent_type=episode` and its bare `parent_id`
      equal to the verified parent episode's bare marker.
      Inject **wrong-principal-prefix**, **wrong-parent** (bare
      `parent_id` names a different episode), **wrong-session** (a private-session
      fact the HTTP DTO nests under a valid group episode), and
      **DTO-content-tampering** (HTTP `content` differs from the Markdown entry)
      fixtures — each must be excluded as suspected-poison and never served from
      the DTO. A fact whose own entry matches the verified parent episode
      survives, INCLUDING a real **cross-midnight** positive fixture whose fact
      was written the day AFTER its parent episode: the oracle must bucket the
      fact by its OWN `af_YYYYMMDD_seq` id date (which differs from the parent
      episode's date), so its own dated daily file differs from the parent's — it
      must be accepted. Then cover
      the frozen DTO mapping: episode labeled Subject/Summary/Content with required
      Content, fact content/date/source inheritance, canonical compact profile JSON,
      and foresight `Foresight`-only text with filename/frontmatter/entry-id
      bucket agreement, an independently timezone-normalized entry-timestamp
      date, and the frozen opaque ref. Include a valid delayed/cross-midnight
      fixture whose storage-bucket and source dates differ. Empty/type/date/ref-invalid and over-item-budget fixtures must be
      omitted whole with the exact closed warning; optional foresight Evidence must
      not be released. Then cover
      same-millisecond and backward-clock turns through the
      persistent provider-clock allocator (distinct stable message ids), plus
      first-row initialization, the fixed `253402250399998` ms UTC+14-safe
      year-9999 maximum under every available IANA zone, rejection of the old
      UTC-only `253402300799998` maximum in `Asia/Shanghai`, overflow
      rejection, and proof
      that browser/platform numeric timestamps never become provider clock input,
      clear-all epoch semantics, per-flush request count/latency and clearly
      labeled provider-authoritative usage or tokenizer estimate (characterizes,
      but cannot enforce, token cost), plus per-`/add`, per-flush, and total
      LLM/embedding endpoint attempt counts so EverOS-internal SDK/OME retries
      are visible rather than confused with Avibe transport retries. Fault
      boundary and episode LLM calls and require synchronous `/add|flush`
      failure; separately fault fact/foresight/profile LLM and cascade embedding
      after a successful add, require observed internal degradation, and prove
      the accepted add is not replayed. Fault search embedding and require a
      closed explicit-read error plus empty hot-path recall. An unavailable
      diagnostic may narrow the health claim but must never assert async success;
      frozen phase-1
      `method=hybrid`/`enable_llm_rerank=false` payloads with proof that no
      rerank connection occurs, and OME/cascade
      failure-state parsing from pinned internal
      SQLite stores (`ome.db` run records and `system.db` cascade state; no HTTP
      status surface exists). Assert the effective runtime has
      `memorize.mode=chat`, `reflect_episodes.enabled=false`, the configured
      IANA timezone, and the exact confined `file_uri_allow_dirs`; startup must
      fail on a mismatched effective value. Also assert
      `EVEROS_ROOT=<run>/everos-root`, the Python env is a non-overlapping
      sibling, and the sidecar is bound only to the short, bounded harness-owned
      Unix-domain socket (`<run>/memory/.rt/s<8-hex-of-root-hash>.sock`, no TCP
      host/port, finding 6 rev32), with mode `0600` in a `0700` directory. Assert effective session-lock timeout=360s,
      adapter add/flush deadline=370s, and zero **Avibe-to-sidecar** transport-
      level POST retries;
      maintenance timeout cannot reclaim a live server task. Seed an unprocessed raw message and prove the
      production adapter never maps/releases `data.unprocessed_messages`.
      Run the production read-envelope boundary cases: query type/nonblank,
      CRLF/CR-to-LF + NFC normalization and post-normalization UTF-8 size at
      exactly/over 8 KiB; provider responses at exactly/over 2 MiB with absent,
      false-small, and false-large `Content-Length`; malformed/deep/wrong-type
      JSON including exact/over 32 nesting levels, 20,000 total nodes, requested
      top-level counts, `/get` requested-kind array/count agreement, empty
      non-requested arrays, nonnegative JS-safe `total_count`, exact `has_more`
      boundaries, and 256 nested facts; exact/over 64 KiB item text;
      exact/over 1 KiB provider ids, blank/control/path-shaped refs, invalid dates,
      NaN/Infinity/overflow numeric scores and timestamps,
      and a 256 KiB complete-item explicit
      result. Require whole-item omission with closed warnings for item/result
      caps, explicit closed failure for body/schema caps, and empty auto-recall
      with no partial content. Hold connect/body/search/profile/get/foresight
      stages past the frozen 20-second explicit deadline and recall past 1,500 ms;
      require `provider_read_timeout`/empty recall and no partial content. Verify automatic/explicit provider `top_k` never
      exceeds 16/50 and `/get` page size never exceeds 50
- [ ] Lineage read envelope (finding 6, rev33): the episode/fact Markdown lineage
      verification opens local daily files, and EverOS appends a whole day to one
      file, so exercise the `ReadLimits` lineage caps. Test a daily file at exactly
      and over `max_lineage_file_bytes`; a retrieval spanning more than
      `max_lineage_files` files and an aggregate over `max_lineage_total_bytes`;
      one very large daily file whose entry-id marker sits beyond
      `max_lineage_marker_scan_bytes` (bounded marker scan must not parse the whole
      file); path-caching so a file opened for the episode is not re-read per
      nested fact; and a **swap race** where the file is replaced mid-read (the
      pre/post-read `fstat` size/owner/inode check must detect it). Every cap
      breach, non-regular file, or `fstat` mismatch excludes the affected item as
      suspected-poison — never served — and never drives reads toward the 2 GiB
      provider cap
- [ ] Markdown-cascade honesty: edit valid retrieval-relevant profile
      frontmatter and daily-entry fields for episode, atomic-fact, and foresight
      fixtures and prove
      the watcher re-projects each registered kind without waiting for the
      scanner; stop the sidecar, edit again, restart, and prove the 30-second
      scanner closes the missed-event gap. A malformed edit must leave the prior
      indexed row observable while the cascade row becomes failed/degraded; a
      corrected resave must converge. Finally trigger later profile extraction
      and prove it may overwrite the manual profile edit and does not recompute sibling derived
      tracks from that edit. This is evidence for best-effort projection, never a
      supported forget/redaction claim
- [ ] After the real capture/recall slices exist, run a feedback-loop probe:
      seed one fact, execute repeated nonempty auto-recall and agent CLI
      search/profile turns with no new owner assertion, then a later turn with
      recall disabled. Prove the first read atomically creates a keyed native-
      context taint, every current/later provider payload is user-only, and
      episode/fact/foresight/profile do not amplify the assistant's recalled
      paraphrase. Archive/resume the same native id into a new Avibe row, disable/
      re-enable, and clear-all; taint must survive every case and prevent cleared
      content from reappearing. Drive it as a non-owner, in another group, after
      same-group→private promotion→group return, and while remote access is
      enabled; each ordinary turn must fail before backend prompt. Enable remote
      access during an admitted tainted turn and require cancellation/suppression
      before the cut returns. Exercise Workbench, IM, `--fork-self`, and
      `--fork-session` for all three backends: a tainted source must taint the
      target id before its first prompt, and an opaque create+prompt primitive
      must be rejected. Race two Avibe rows aliasing one native id against first
      taint, normal prompts, fork propagation, revoke, and remote enable; the
      keyed context lease and access→context lock order must prevent both prompt
      leakage and deadlock. A provably empty (not forked) native id regains normal assistant
      capture. An unidentified first-turn context gets empty recall/closed CLI;
      inject failure of either taint or `memory_read_used` commit and require no
      memory content. Saturate the 10,000-row taint cap without eviction. After a
      current or prior context read, agent-origin `remember` must fail
      `memory_feedback_guard` with no operation; race first search and remember
      and prove the transaction winner defines the safe order. Direct user
      `/memory remember` remains independent
- [ ] Agent-backend egress contract (credential-free, outside the live provider
      gate): with recording fake Claude/Codex/OpenCode transports, prove nonempty
      auto-recall enters the next agent prompt and CLI search/profile content
      enters that backend's tool context, while direct Workbench HTTP and
      unmirrored IM `/memory` reads never enter an AgentRequest/backend
      transport. With recording Memory endpoints, prove hybrid explicit search
      sends its normalized query and eligible auto-recall sends the current owner prompt
      to embedding even with capture off; explicit remember and drain/export
      flush send admitted text through processing, while profile/timeline/
      foresight-file/status/help make no model request. Assert settings and clear
      confirmation distinguish these paths and say retained query/backend/native-
      session copies cannot be retracted. Do not use real agent/model credentials
- [ ] Processing-transition disclosure (credential-free): seed Avibe pending,
      uncertain, and flush rows plus fake OME pending/running work, then attempt
      an LLM URL/model change. The candidate-digest confirmation must show every
      count and both destination consequences; it is minted only after admission
      closes, workers/sweepers pause, the capture generation advances, active
      snapshots scrub, and old-endpoint/OME calls quiesce. Turns active/queued at
      the cut or arriving while closed must create no snapshot/outbox and only
      increment `processing_transition`; an already-linked explicit operation
      keeps `explicit_remember` priority and no false miss. A call that cannot quiesce must
      abort before config save. Change any bound count/config/access generation
      before consume and require abort plus a fresh preview; cancel/expire must
      restart the old tested runtime and reopen old admission. After confirmation, internal
      pending work may resume only under the approved candidate while Avibe rows
      stay `awaiting_resume`; a later drain must name possible old-and-new
      provider exposure. A key-only same-URL/model rotation is not labeled a new
      destination, but takes the same capture cut and still quiesces. Use recording fakes only
      and make unreadable Avibe/OME work diagnostics reject the preview/config
      rather than displaying zero
- [ ] Local-log and diagnostic-egress contract (credential-free): replace known
      raw-content success logs on every supported capture surface with ids/counts.
      Feed unique owner-prompt, assistant, recall-result, search-query, remember,
      endpoint, and key canaries through successful and failing fake transports.
      Memory component logs contain only ids/counts/closed codes. A recording
      Sentry transport under both UI and controller proves `send_default_pii`
      false and no HTTP data, breadcrumbs/logentry text, exception values, or
      frame locals serialize any canary. Clear/disclosure tests still name
      preexisting local logs and already-emitted crash reports as non-retracted
- [ ] After slice 2, run bounded-admission and revocation probes through the
      real module: file/image-only and empty normalized inputs create no
      plaintext snapshot/outbox; prompts, assistant bodies, and explicit
      remember at/over each UTF-8 byte boundary behave whole (never truncated);
      a mixed text+attachment fixture stores neither attachment bytes nor local
      path/tool trace but may capture the bounded semantic assistant summary;
      assert the disclosure distinguishes this from a file-only skipped turn;
      concurrent snapshot/terminal/operation writers cannot exceed the global
      journal-byte or row caps, and every row cap counts all nonterminal states
      (not only `pending`); missed state remains one aggregate row per
      epoch/cause with no actor/session/dispatch detail. Revoke/unbind/unpair an
      owner between acceptance, Workbench queue claim/IM pre-gate snapshot,
      active CLI/content read, destructive challenge, and terminal persistence;
      every old access-generation path must fail closed, no stale content may be
      released after revoke success, and no outbox may appear. After nonempty
      auto-recall/agent CLI content, block terminal platform send, revoke, and
      require the memory-influenced turn to cancel/suppress before the cut
      succeeds. Make the boundary uncancelable past 30 seconds and require the
      cut to fail before owner/pairing config or generation changes. Crash the
      controller with a terminal outbox/operation already committed, then
      source-off/unbind/revoke: the old work must remain (and the UI must not
      claim deletion); only master disable freezes it for the documented drain/
      eligible-zero-attempt-discard/clear choice. Crash the
      controller after a restored-OpenCode-capable turn sets
      `memory_read_used`, revoke/change generation before startup reconciliation,
      and require restored output to stay gated until a fresh resolver check +
      dispatch lease succeeds (otherwise cancel/suppress and scrub).
      Model today's complete shared release graph explicitly: verify IM agent
      results enter unified `messages` with a session id and are discoverable
      through `platform=all` inbox + generic session history. With
      `remote_access` enabled, and separately with remote access off but effective
      `ui.setup_host` wildcard/LAN/unknown, loopback/network Workbench and every
      private/group IM agent turn must reject auto-recall and every agent CLI operation
      except static help, including mutating `remember`; an ordinary turn in any
      previously tainted native context must also fail before prompt. Direct subject-private
      Memory HTTP and direct unmirrored IM commands
      remain usable. An active network owner can use documented content/action/
      status routes, including confirmed clear/export, but every settings,
      identity, capture-toggle, and provider-topology mutation remains loopback-
      only. Enable remote access or widen `ui.setup_host` during a blocked memory-influenced turn
      on every platform and require the generation cut to cancel/suppress it
      before anything reaches message persistence, global SSE, generic history,
      all-platform inbox/preview, search/push, or terminal output.
      Before widening, persist an ordinary reply from a Memory-influenced turn;
      then require the loopback confirmation to state that the old generic row is
      not retroactively access-controlled and will be visible under the remote
      machine-operator grant. The cut must neither claim to rewrite/delete that
      row nor hide the separate chat-deletion/backend-retention limits.
      Exercise the actual final-routing helper on every transport, not a test-only
      audience flag: private/global Memory redirected to a group, group A to B,
      and thread to channel root must fail before Memory/embedding access; exact
      group A to A and a proved group-to-owner-private narrowing may proceed, with
      the latter promoting the native-context taint to private. Change
      `delivery_override`/`post_to` after a successful read and prove every actual
      dispatcher output is rechecked and suppressed on mismatch. Snapshot audience
      fields must be server-derived, survive queue/restart, and be browser/agent-
      unsettable.
      Exercise concurrent remote
      pending enrollment, self-row-only visibility, expiry, and the per-issuer
      16-pending cap without exposing raw subjects. Race the 64-current-active
      limit, 90-day stale/revoked sweep, and 10,000 inactive/stale hard cap
      (including current-pairing revoked rows); full
      capacity must deny new enrollment without deleting active authorization.
      At that cap, revoke/unpair/exposure cuts must still succeed; atomically
      omit/delete only rows made non-authorizing by that same cut and never a
      current active/live-pending row to admit somebody else.
      Reuse the same Cloud `sub` after
      instance/session-secret rotation and prove the old keyed approval cannot
      authorize even when cleanup is faulted. Also disable/re-enable with exactly
      the same pairing material, and fault the post-cut config save: the monotonic
      pairing generation must keep old rows denied and require fresh enrollment +
      loopback approval in both cases. Attempt `memory` through generic
      config and `is_owner`/`memory_capture_enabled` through generic settings;
      both must fail without
      disclosing keys/owner identities or changing ownership; a generic
      full-payload save that omits those fields must preserve both server-side
      facts. Seed the authoritative config with a complete Memory subtree
      containing canary secrets, desired state, the fixed derived `socket_path`, and transition
      linkage; run projected full-payload and partial saves through every current
      production `V2Config.save()` and `api.save_config()` call site (including
      remote pairing, WeChat/runtime, controller/settings, agent-path, and
      default-cwd helpers), stale in-memory objects, alternate config targets,
      plus races with enable/disable/config transitions. Run UI and controller as
      separate processes: all writers must take the secure target-specific file
      lock, not merely their independent `CONFIG_LOCK`; fault lock acquisition,
      authoritative re-read, temp/replace/parent-fsync, and transition receipt
      validation. A remote enabled/provider/instance/secret or effective
      `ui.setup_host` exposure change without the exact current network-audience
      marker receipt must fail before publication.
      Every accepted generic save must preserve the hidden subtree exactly and
      must not bypass a lifecycle call; a client `memory` key must fail before
      merge/write. Toggle capture concurrently and prove its settings write,
      capture-generation cut, and snapshot scrub are one SQLite transaction.
      Seed legacy/new bound non-owner rows and require both `is_owner` and
      `memory_capture_enabled` false; first loopback owner selection atomically
      defaults capture true unless explicitly off, and removing owner status
      resets capture false. Disable/unbind an owner through every production path,
      then re-enable/rebind it and require both persisted facts to remain false
      until direct-loopback reselection. Seed every malformed owner/capture/bound/
      enabled combination and prove startup, resolution, and pre-bind repair clear
      it. Load stale settings stores in both processes and exercise every direct
      save/mutation call site: ordinary upserts preserve hidden facts, direct
      owner deletion/disable fails, and only the controller transaction changes
      settings + generation + snapshots atomically. No generic save/import/bind
      path may preserve or revive a dormant true value on a non-owner.
      Crash every prepare/save/finalize edge of enable/disable and require
      admission to stay closed until a valid completed transition. For disable,
      fault before/after the durable `disabling` marker plus capture/access cuts,
      during provider join, and around config publication; startup may complete
      the same marker or prove a marker-bound abort, but never reboot as enabled
      with old generations/snapshots. Prove Workbench reserved queue metadata is
      browser-unsettable and absent from serialized message APIs, consumed after
      queue claim/direct admission, removed by orphan cleanup, and stripped from
      pending rows by clear without changing chat text; prove IM uses
      its snapshot rather than a nonexistent durable queue row. Keep an active
      snapshot beyond 24 hours and bound only consumed tombstones at 14 days /
      10,000 rows. Exercise every hard metadata byte cap; non-owner/multi/harness/
      disabled/wiping/invalid-metadata skips must create no snapshot/event row,
      and every consumed owner tombstone must contain no raw scope/session/
      message id or event timestamp. Saturate the 256 active-owner-snapshot cap:
      later turns store no prompt, release no recall/CLI content, count one
      capacity cause, and terminal does not relabel it as a missing snapshot.
      Expire an unfinalized prepared sidecar after two minutes and restart across
      PID reuse: kill only an exact PID/start-token/executable/listener/ownership
      match; unknown ownership remains alive with memory admission closed and no
      candidate secret in the transition marker.
      Contract-test the settings disclosure against the actual Workbench
      boundary: it must name absolute-path file access, arbitrary-folder
      projects, terminal, and full-power agent access as machine-operator powers
      outside Memory authorization. It must still prove unsupported Memory routes
      and shared memory-influenced output fail closed, without claiming those
      gates or `0700` protect against deliberate broader Workbench local access.
      Exercise config parent/file symlink, wrong-owner/type, and broad-mode cases;
      no processing key may be written until the canonical parent/file are safely
      0700/0600, and crash tests cover temp fsync, replace, and parent fsync.
      Exercise the effective state/UDS parent, SQLite file, and socket too:
      current broad mode, safe tightening, symlink/wrong-owner/type/chmod failure,
      `VIBE_INTERNAL_DISPATCH_SOCKET` override, and proof that Memory alone fails
      closed while existing non-Memory dispatch remains usable. Race first
      enablement and require one atomic UUIDv4/scope-key/root-id triple plus
      `memory_root_state=creating`. Fault every state/sentinel commit and fsync
      edge; absent state beside a nonempty root must refuse without inserting
      identity. Only `creating` with no Memory config/work may recover an absent/
      empty root or promote its exact sentinel; `ready` with an absent root/
      sentinel must fail as data loss rather than recreate. Inject a
      partial, malformed, changed triple, a nonempty sentinel-less root, and every
      sentinel-field mismatch and require `memory_identity_corrupt` without
      adopting/wiping the root. Prove the runtime-environment sentinel is a
      distinct file built and fsynced inside staging, never the provider-root
      ownership sentinel.
      For first enable, key rotation, endpoint/model change, and post-clear
      re-enable, require the fixed transition path and a marker-bound
      id/config-digest/nonce canary sentinel distinct from both production and
      runtime-env sentinels; verify it is stopped/wiped in every outcome and never writes
      synthetic content to the production root. For desired-enabled clear, fault
      direct probe, canary, production start, production health, and final SQLite commit.
      The authoritative embedding contract/admission may appear only together
      after all checks; every earlier crash leaves `enabling` recoverable,
      production admission closed, and the just-cleared production root free of
      canary data. Immediately after the wipe commit, retry the confirmation and
      require the same completed deletion receipt with durable
      `runtime_reenable_pending`; final publication clears that warning atomically,
      while every terminal recovery failure replaces it with
      `runtime_restart_failed`. The enabling marker must carry the exact
      originating clear receipt; corrupt/missing/wrong-purpose/epoch/status links
      fail `memory_transition_receipt_corrupt` before production start or
      contract publication.
      Fail terminal memory savepoint A, then require savepoint B to scrub text/
      actor/scope and count `outbox_error`; fail A+B, end exact runtime/poll
      ownership, and require periodic/startup reconciliation to scrub without an
      age TTL. Exercise every typed post-outer-transaction terminal persistence
      result: only `committed` consumes the snapshot and creates one outbox;
      `duplicate` creates neither, and `skipped|failed` cannot authorize capture.
      Race duplicate insert and outer commit/I/O failure. During export, accept one owner input only after provider/public
      admission closes and complete another across the cut; both must use the
      open local-journal lane and persist snapshot/outbox for post-restart drain.
      Advance retention: delivered/completed/ordinary-dead-outbox rows compact after 14
      days without defeating source-ledger idempotency; completed remember still
      resolves/mismatch-checks through its keyed source fingerprint; ordinary-dead
      operation payload clears but its compact tombstone remains, and the 10,000
      cap rejects rather than pruning uncertain evidence. With small limits,
      reserve permanent source capacity across `memory_sources` plus every
      source-producing outbox/operation state; concurrent conversion must keep
      the total constant, capacity must fail before plaintext/provider access,
      no pruning may admit another source, and status must distinguish permanent
      `source_records` from exact `source_capacity_used`. At a maximum-scale
      fixture, assert SQLite uses the epoch/session index for result membership
      and the check respects the read deadlines. In contrast, advance
      `durability_blocked` outbox/operation/flush work beyond every sweep and
      prove payload and exact outcome/stage remain capacity-accounted until
      barrier success or clear. Race aggregate-only
      no-row admissions before/after clear's epoch bump and prove no old-epoch
      ledger row is recreated
- [ ] Direct-command idempotency across every IM adapter: namespace the verified
      stable native event/message id by platform + scope, retry the same event,
      and collide the same bare id across two scopes/platforms. An adapter/event
      with no stable id must reject `remember` and `export` as
      `idempotency_unavailable` while
      read-only memory commands still work; text/time/random fallback is
      forbidden. For every command, prove the formatted result uses the platform
      client's direct command-response path and creates no unified `messages`
      row, inbox/preview entry, broker event, or capture snapshot/outbox. In every
      group adapter preserve the exact inbound thread/topic and prove the Memory
      handler never calls `_get_channel_context`; when the adapter cannot
      preserve that target, require `scope_unresolved` before any provider read.
      Feed mention syntax, external/internal URLs, file/action/quick-reply directives, HTML/Markdown,
      ANSI/C0/C1/bidi controls, and multiline provider text through each adapter;
      require inert literal output with no mention, link preview/unfurl, action,
      file, or parser side effect,
      or a closed failure before release. Agent CLI output must remain valid
      schema-framed JSON and Workbench must render text nodes, never raw HTML
- [ ] Workbench direct `/memory` submission idempotency: obtain one
      `client_submission_id` from the dedicated same-origin Memory token endpoint,
      drop the first command response, and retry the exact request. Prove the
      token is server-minted/versioned/HMAC-authenticated, expires after 24 hours,
      binds subject + server-derived UI context + epoch + access generation, and
      cannot be minted or extended by the browser. Exercise both exact contexts:
      `session:<validated id>` from the composer route and fixed
      `memory-panel` from the dedicated view/settings route; browser JSON cannot
      choose them and tokens cannot cross contexts. `scope_id=None` is allowed
      only for fixed `memory-panel`; capture and current-session operations reject
      it, and the nullable value survives source-ledger/export handling. Migration `0031` must get-or-create one
      content-free `memory_command_requests` tombstone and one remember/action/
      export receipt; it must create no `messages` row or persisted response
      content. The result is `Cache-Control: no-store` and never reaches global
      SSE, transcript/history, inbox/search/push, or proxy cache. Reuse with a
      different body conflicts; malformed/MAC-invalid/cross-subject/cross-context
      tokens neither collide nor grant identity. Clear preserves bounded command
      tombstones but an old-epoch or expired token without a retained receipt is
      rejected. After fresh authorization, an exact retry may return only an
      already-retained matching receipt and must never restart its mutation.
      Exercise the 90-day/10,000-terminal-row bound without sweeping nonterminal
      rows. Leave read/remember/export commands `admitted` at each crash edge:
      after prior-controller death, reads become retryable interrupted failures,
      deterministic ledgers populate remember/export refs, and no body
      fingerprint executes. Saturate the hard 256 admitted-row cap and require
      `memory_command_backlog_full` without deleting active rows. Revoke/re-pair before retry and prove the resolver/access lease
      releases no result. Race many exact retries while the first command is
      blocked: only the insert/CAS winner may hold a live boot/task owner or
      execute; others get `command_in_progress`/the first terminal receipt.
      Cancel the winning task and prove same-boot reconciliation waits for exact
      task death rather than age before permitting one new CAS winner. Mount the interceptor in
      `sessions_messages_create` after session authorization/`dispatch_text`
      extraction but before attachment resolution and `_persist_user_row()`;
      assert no pending/user/queued row, draft clear, attachment resolution,
      controller dispatch, or broker event occurs
- [ ] Destructive-action receipt recovery: consume one clear/discard confirmation
      and fault before the response and at every confirmation/receipt/deletion/
      epoch/wipe commit edge. Exact authorized token retry must resolve one
      `memory_action_receipts` id; `discard_unsent` deletion + completion are
      atomic, and `clear_all` writes the same id to
      `state_meta.memory_clear_receipt_id`. Startup resumes that receipt without
      re-entering the public confirmation API. Missing/mismatched wipe receipt
      fails closed; pre-mutation failure becomes failed and requires a new
      confirmation. Clear removes challenges but preserves receipts. Sweep only
      terminal receipts at 90/14 days and the 1,000-row cap, never `preparing`.
      Seed recognized current-manifest and legacy Avibe SQLite migration backups
      containing Memory tables in the effective backup directory: clear must
      delete the whole managed files before completion. Read-only inspection or
      unlink ambiguity keeps `wiping`; JSON state, unknown files, and user backups
      remain untouched. Confirmation must disclose loss of unrelated rollback
      value and the non-forensic secure-erasure boundary.
      Assert `MemoryStatus` reports admitted-command, live-confirmation,
      preparing-action, source-record, source-capacity-used, and backend-tainted-context counts without subjects,
      native ids, command bodies, or tokens
- [ ] User sets `.env.poc` mode `0600` and fills both explicit LLM and embedding
      OpenAI-compatible endpoint blocks; one credential may be reused only when
      that configured provider supports both APIs
- [ ] Processing-config validation: both LLM and embedding require bounded
      nonempty model/key and normalized absolute HTTP(S) base URL; reject
      userinfo/query/fragment, oversize, empty, and UI mask values. A numeric
      loopback-IP URL plus an explicitly configured sentinel key is allowed;
      `localhost` still requires HTTPS. Reject plain HTTP for every hostname or
      non-loopback IP and every attempt to disable TLS certificate/hostname
      verification; accept HTTP only for `127/8` or `::1` literals and HTTPS
      elsewhere. Return same-origin and cross-origin 3xx from both configured
      endpoints and prove direct probes plus the provider-facing relay follow none;
      installed EverOS's redirect-following default is an asserted reason it never
      receives the provider URL.
      Enable/change probes the
      exact two endpoints, while all failures expose only closed codes. Reject
      enabled key removal unless the same transition disables first;
      remove while disabled with pending/uncertain work and verify drain is
      unavailable until compatible credentials return or clear-all completes
- [ ] Embedding contract: persist normalized-endpoint/model keyed digest plus
      observed raw dimension and fixed effective dimension 1024. Direct probe
      vectors at exactly 1024 and accepted longer dimensions through 16,384 must
      contain only finite numerics; prove installed 1.1.3 truncates an accepted
      longer vector. Stream exact/over
      4 MiB direct probe responses and exact/over 16,384 raw dimensions; oversize
      must fail before full parse/EverOS. Short, empty,
      nonnumeric, and nonfinite vectors fail `embedding_incompatible` before
      LanceDB. Require an end-to-end EverOS canary and allow a key rotation only
      at the same raw dimension. With any source/buffer/raw/Markdown/index data
      present, reject endpoint/model/raw/effective dimension change as
      `memory_reindex_required`. Prove the only phase-1 path
      is disable → optional export → clear (runtime stays stopped, contract
      removed) → configure → enable, with no old-vector capture window. LLM-only
      change is future-facing and visibly disclosed
- [ ] Run seed/probe through the frozen hybrid/no-rerank adapter with at least 30
      predeclared Chinese/mixed-zh-en positive assertions plus leakage negatives,
      three clean-root runs. Pass only if every leakage and critical
      current-vs-stale temporal assertion passes in all runs, and at least 90% of
      positives return the expected episode/fact in top 8 within 1,500 ms in
      every run. Timeouts are failed positives. Explicit-flush episode
      write-to-searchable p95 must be <=5 minutes; record the full distribution
- [ ] Run isolation: require same-project principal separation and logical
      scope post-filter negatives to pass through the fixed-project Avibe
      adapter; separately confirm/deny #320 through a direct multi-project
      reproduction and characterize snapshot Markdown vs LanceDB divergence. The latter is
      known-defect characterization, not a cross-project phase-1 pass claim.
      Regardless of upstream search behavior, foreign app/project/principal or
      unledgered session metadata must never pass the production post-filter
- [ ] Run footprint on the recorded machine class and rebuilt locked env: install
      <=1 GiB; after warmup idle RSS p95 over 10 minutes <=512 MiB; peak RSS
      during the fixed 500-turn workload <=1.5 GiB; provider-root growth <=512
      MiB. At a lowered high-watermark, no new provider claim starts after the
      crossing observation; measure (do not deny) overshoot from the admitted
      call/queued async work. Record p50/p95 recall latency.
      Token/provider-call cost is characterization only unless the endpoint
      returns authoritative usage; label tokenizer estimates and never call them
      billed tokens
- [ ] Export-path guard: reject a destination equal to/inside/above provider,
      runtime, config, or state roots; reject empty and exact/over-4096 UTF-8,
      control/bidi/NUL, effective `PATH_MAX`/`NAME_MAX`, existing/symlink/special entries before
      storing invalid input;
      accept only a new safe loopback-selected leaf. Private IM/network callers
      must ignore path text and receive a generated leaf under the fixed export
      root. Run the export from enabled, disabled, awaiting-resume, error, down,
      storage-paused, and credentials-missing states. Only healthy-enabled may
      drain/flush; every closed state must send no frozen text, record exact
      `processing_not_attempted`/omission metadata, and remain closed. Corrupt the
      production root/sentinel in each state and require no copy/touch. Inject
      copy/restart failures and verify staging-only cleanup plus
      honest completed-with-runtime-warning behavior. Race creation of the final
      leaf, require native atomic no-replace (unsupported = fail), and verify
      file/staging/parent fsync happens before success. Verify `sources.jsonl`
      contains current-epoch source/platform/scope/session mappings and no
      prompt, assistant body, canonical owner subject, endpoint, or credential;
      local provider message ids/fingerprints are not exported. Force
      distilled (`full`+`episode`), buffered (`full`+`buffered`), and orphan
      evidence and verify each source state plus
      manifest counts, and add a **cell-plus-retained-tail** (`full`+`mixed`)
      fixture — a memcell plus a still-buffered tail — asserting it exports as
      **`buffered`, not `distilled`** (finding 5, rev33), because the excluded
      `.index` still holds part of the source; manifest hashes the file and declares source mapping
      included/raw archive omitted, and records the exact `outbox_cut_seq` and
      `operation_cut_seq`. Race admissions around the cut and prove sequence
      comparison, not wall-clock time, selects rows; empty/cleared tables may
      retain nonzero counters. Crash after final publication but before
      receipt completion, retry the same scoped request, and require manifest-
      based completion with no second copy; destination/requester mismatch must
      conflict and access-generation mismatch must deny. Off-loopback responses contain only export id/leaf, never an
      absolute home path. Sweep terminal receipts at 90/14 days and the 1,000-row
      cap without deleting export directories or touching active receipts.
      Seed enough due flush rows to exceed 420 seconds: calls must be serial,
      each deadline clipped to remaining budget, no new call started at expiry,
      unattempted/failed rows represented as honest manifest omissions, and no
      copy started until the owned sidecar is stopped and proven exited
- [ ] Raw-retention/storage guard: hold a non-boundary tail and prove its text is
      present in `system.db:unprocessed_buffer.content_items_json/text`, remains
      frozen across disable, and disappears only through extraction/full clear.
      Prove each extracted fixture remains in `memcell.payload_json` after Avibe
      delivery and all async tracks settle; verify no upstream cleanup path runs.
      Exercise the 2 GiB-default
      high-watermark with a lowered test threshold: record the maximum observed
      overshoot from one admitted call plus asynchronous OME/cascade work it
      already queued without claiming a formal bound; sidecar/provider claims
      stop, chat stays live, local journal caps still
      apply, distilled export declares/excludes `.index`, and clear-all removes
      the raw archive. This evidence is required for consent copy and footprint
      sizing, not treated as a derived-index-only cost. Plant symlink/special/
      regular canaries in the separate `file-staging` allowlist: clear must
      remove regular children/unlink symlinks without following, fail safe on
      unsupported special entries, include it in storage bytes, preserve the
      empty directory, and never touch the sibling versioned env
- [ ] Metadata-cardinality guards: with small hermetic limits, race first `/add`
      calls for many distinct provider sessions. Require a durable flush row to
      commit before each call, never exceed the
      `pending|flushing|durability_blocked|dead` cap, and
      leave new-session outbox work local with `flush_backlog_full` at capacity.
      Crash/fail after reservation and after provider acceptance; zero/full/
      ambiguous evidence must remove, schedule, or dead-letter the reservation as
      specified, and no accepted tail may lack one. Exhaust persistent provider
      clock/session rows while the endpoint stays down and prove new ordinary/
      explicit admissions fail before unbounded row growth, while existing
      sessions still work. Before each IM `/new`, enumerate every exact
      `agent_sessions` row covered by compatibility keys/base prefixes and
      deduplicate backend/subagent aliases; after full/partial resets notify only
      retired rows, plus the reopenable Telegram old-topic case. Workbench uses
      its exact archived row. A current owner makes only existing current-epoch
      pending flush rows due; non-owner/stale, duplicate, unresolved, and no-row
      notifications mutate nothing, no provider call occurs inline, and an internal-transport failure leaves the original durable
      30-minute deadline plus uncertainty barrier intact. Race different exports and require one active
      `preparing|published` executor globally; same-id retries observe it without
      a second copy/task, and canceled/same-boot orphan ownership is recovered
      only after exact task death
- [ ] Foresight reader boundary: under the isolated provider root, plant exact
      and malformed basenames, component/leaf symlinks, wrong-owner/special
      entries, invalid UTF-8/frontmatter, exact/over-1-MiB files, more than 2 MiB
      aggregate content, and more than 366 dated files. Require directory-FD
      no-follow reads of UID-owned regular files only, newest-first deterministic
      selection, closed degraded warnings at every skip/limit, no partial item,
      exact frozen keyed-digest refs containing no filename/absolute path,
      filename/frontmatter/entry-id date mismatch rejection without rejecting a
      valid different entry-timestamp date, and zero filesystem
      opens for a group-scope foresight request
- [ ] Free-space reserve: lower a hermetic volume through thresholds before
      snapshot, terminal outbox, operation, provider call, install, and export;
      below 512 MiB (or configured higher value) no new memory plaintext/write is
      admitted and `low_disk_space` reports observed bytes. Race a separate writer
      and prove the UI/docs do not claim quota or guaranteed reserve behavior
- [ ] Write findings back into research doc section 9 / section 10
- [x] Decision (2026-07-19): phase 1 uses official EverOS, no fork — see
      `docs/plans/memory-plugin-everos-phase1.md`; this harness now validates
      the official version's behavior inside phase-1 constraints

## Later phases (not this POC)

- Fork EverOS: #320 scoped row identity + migration + regression tests;
  upstream the fix early (upstream response time is itself a data point)
- Delete design: source deletion + profile rebuild convergence
- Memobase one-day code read (parallel track, independent of this POC)
- Phase-2 rerank characterization: upstream ships deepinfra / vllm / dashscope
  shapes; verify a chosen endpoint only if an agentic/user-search method is
  added to the module contract
- Avibe-side slice ①: `MemoryModule` + fake adapter + contract tests
  (provider-independent; may start after the rev29 core types, authorization
  truth table, platform-capability boundary, and provider-commit-barrier contract
  are frozen, without waiting for model credentials or this POC)
