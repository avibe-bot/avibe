# Memory: agent-initiated memories as a separate EverOS user owner

## Background

Avibe has two Memory write sources:

1. automatic capture of user messages (`provenance="user_input"`);
2. agent-initiated `vibe memory remember` (`provenance="agent"`,
   `core/internal_server.py` `/internal/memory/remember`).

Both currently reach EverOS as the same owner: `EverOSPort.add()` always sends
`sender_id=<user principal>, role="user"` (`core/memory/everos.py`), so an
agent-recorded fact ("the user plans to release on the 23rd") is
indistinguishable from something the user said themselves. `provenance` is
stored locally but never crosses the provider boundary.

Product requirements:

- do not modify EverOS;
- user memories and agent-initiated records are both searchable;
- separate profiles: the real user's profile and the agent's private profile;
- an agent-initiated record is the agent's own memory, even when its content
  describes the user;
- no cross-user leakage when different users use the same agent.

Two schemes were reviewed against EverOS code (pinned integration, reviewed at
EverOS `560fb80`):

- **Scheme 1 (rejected): send agent records as `role="assistant"`.**
  Structurally unworkable for single-fact records, not merely a poor fit:
  - the boundary stage requires a `role="user"` anchor before it cuts cells
    (`EverOS/src/everos/service/_boundary.py`, "Need a role=user anchor"
    guard) — assistant-only sessions accumulate in the buffer until a forced
    flush;
  - Episode fan-out only covers `role="user"` senders
    (`user_memory.py::_unique_user_senders`) — assistant messages never
    produce Episode / AtomicFact / Profile;
  - the only possible product is an AgentCase, and `AgentCaseExtractor` is
    built for task-execution trajectories, not standalone facts — a single
    fact is dropped or distorted into a nonsensical TaskIntent.
  Scheme 1 remains the right shape for a future feature that captures real
  assistant/tool execution trajectories. It is out of scope here.
- **Scheme 2 (adopted): agent records become a second EverOS *user* owner.**
  Both write sources use `role="user"`, but under different `sender_id`s. The
  assistant owner flows through the normal UserMemoryPipeline and gets its own
  Episodes, AtomicFacts, and Profile. EverOS never knows the owner represents
  an agent; the mapping is an Avibe-side convention.

This plan is the implementation contract for Scheme 2. It incorporates the
three blockers found in the design review (owner-aware session derivation,
owner ID shape gates, `ProviderSessionRef` persisted-shape compatibility).

## Verified provider contract (EverOS, no modification)

Load-bearing behaviors confirmed in EverOS source:

- `memorize.mode` defaults to `"agent"`; both pipelines run on the same cells
  (`service/memorize.py`).
- The unprocessed buffer and MemCells are keyed by
  `(session_id, track, app_id, project_id)` (`service/_boundary.py`
  `list_for_track`). **Distinct session IDs can never share a MemCell.** This
  is the isolation primitive the whole design rests on.
- Within one MemCell, the Episode is written once per distinct `role="user"`
  sender ("every user sender owns a copy of the same narrative",
  `pipeline/user_memory.py`). This is why the user owner and the assistant
  owner must never share an EverOS session: a mixed cell would fan the same
  Episode out to both owners.
- AtomicFacts follow `EpisodeExtracted.owner_id`
  (`strategies/extract_atomic_facts.py`); Profile extraction follows
  `event.owner_id` (`strategies/extract_user_profile.py`). A single-sender
  session therefore yields Episodes, Facts, and a Profile for exactly that
  owner.
- `extract_agent_case` returns early with `agent_case_skipped_no_assistant`
  when a cell has no assistant senders. Assistant-owner sessions (all
  `role="user"`) never produce AgentCases. The per-cell warning log this emits
  is a known, accepted side effect.
- Search is owner-exclusive: `user_id` XOR `agent_id`
  (`memory/search/dto.py::SearchRequest`). Assistant-owner data is user-memory
  and must be queried with `user_id=<assistant owner>`, never `agent_id`.
- `sender_id` is a `PathSafeId`: charset `^[a-zA-Z0-9_.@+-]+$`, length 1..128
  (`entrypoints/api/routes/memorize.py`). It becomes an on-disk directory
  segment.

## Design

### 1. Memory owner model (Avibe-side)

New vocabulary, kept strictly separate from the authenticated caller:

- `caller principal` — unchanged; the `u-`-prefixed ID resolved by
  `_memory_cli_scope` / capture admission. All access-control gates keep
  validating this with `is_principal_id`.
- `memory_owner_kind: Literal["user", "assistant"]` — derived, never
  caller-supplied.
- `memory_owner_id` — the EverOS-facing owner:
  - `user` captures: the caller principal itself (byte-identical to today);
  - `assistant` captures: a derived assistant owner ID (below).

Derivation rule (no new caller inputs):

- `provenance="user_input"` → `memory_owner_kind="user"`;
- `provenance="agent"` → `memory_owner_kind="assistant"`.

The mapping lives in `core/memory/module.py` (`MemoryModule.capture`), the
first layer that owns both the trusted principal and the provenance. Callers
of `/internal/memory/remember` cannot specify an owner; the endpoint contract
is unchanged.

### 2. Assistant owner ID

Shape: `<principal>-agent`, i.e. `u-<32 hex>-agent` — 40 characters, a pure
suffix on the caller principal.

Why a suffix instead of a derived hash:

- collision-free by construction: principals are exactly 34 characters
  (`is_principal_id`), so the 40-character suffixed form can never be a real
  principal, and distinct users trivially get distinct assistant owners;
- no new information leaves the host: the principal is already sent to EverOS
  as `sender_id` today, so suffixing it exposes nothing new;
- no dependency on local store state: a keyed hash would bind the owner ID to
  the store's scope key, coupling EverOS-side data to Avibe-local state; the
  suffix form is stable regardless of store lifecycle;
- debuggable: `users/<principal>/` and `users/<principal>-agent/` sit side by
  side in the EverOS tree;
- future per-agent split extends naturally to `u-<hex>-agent-<agent-key>`
  without a schema change. This plan uses the single `-agent` owner: the
  remember path (`_memory_cli_scope`) has no per-agent identity today, and one
  private owner per user satisfies every stated requirement.

The form satisfies EverOS `PathSafeId` (`-` is allowed, length ≤ 128). It is
always constructed server-side from the trusted principal; callers can never
supply it.

A new shape predicate (e.g. `is_memory_owner_id`) accepts the principal shape
and the `-agent`-suffixed shape. It is used only where the *owner* flows
(provider payload mapping, response validation); the caller-facing gates keep
the strict `is_principal_id`.

### 3. Owner-scoped provider sessions (Blocker 1)

Avibe never sends raw session IDs; it sends
`src--<keyed digest of principal:project:session>--e<epoch>`
(`core/memory/store.py::_provider_session_ref`).

Change: the digest input's first component becomes `memory_owner_id` instead
of `principal_id`.

- User captures: `memory_owner_id == principal_id`, so every existing user
  session digest is **byte-identical to today**. No migration, no orphaned
  provider sessions.
- Assistant captures: a different owner ID yields a different digest, so the
  assistant owner automatically gets its own EverOS session per
  (raw session, project, epoch). Combined with EverOS's session-keyed buffer,
  cross-owner MemCell contamination is structurally impossible.

Every derivation and re-derivation site must move together, or assistant rows
get quarantined on recovery:

- `store.provider_session_ref` (minting; gains owner parameters);
- `store.enqueue_request` (mints its own ref inline via
  `_provider_session_ref` when persisting a capture row — it never goes
  through `provider_session_ref`, so it must take the derived owner
  explicitly while continuing to persist the separate caller-principal
  column);
- `store.resolve_current_session_scopes` (recomputes expected digests to trust
  recovery — must try both owner derivations per scope);
- `_legacy_provider_ref` (v0 migration re-derivation — v0 rows are all
  user-owned by definition; derive with owner = principal).

Queue-driven flush *scheduling* needs no changes: `SessionFlushCoordinator`
keys everything by the serialized `ProviderSessionRef`, so the assistant
session gets its own retry/flush lifecycle once its rows exist.

**Terminal flush is not free and is in scope.** The session-end path
(`MemoryModule._final_flush_under_admission`) mints exactly one session ref
from the caller principal today. It must fan out: on a terminal boundary,
final-flush every owner-scoped session that has capture state, so an agent
capture followed immediately by session end is distilled rather than left in
the assistant session's buffer.

**The admission fence stays caller-keyed.** `MemoryModule.capture` keys its
admission lock by the caller principal's scope; if lifecycle resolution
returned owner-qualified scopes, the terminal path would acquire an
assistant-owner lock that does not exclude a racing capture holding the
caller-keyed lock, and the final flush could complete before the capture
commits. Therefore the lifecycle fence (scope resolution and admission
locking) remains keyed by the caller principal exactly as today, and the
owner fan-out happens *beneath* that fence: inside the held admission, the
terminal path derives both owner session refs for the caller's scope and
flushes each. Owner-aware validation applies only where an owner (not a
caller) flows — fence keys and access-control gates keep `is_principal_id`.

### 4. `ProviderSessionRef` compatibility (Blocker 3)

`ProviderSessionRef.deserialize` (`core/memory/types.py`) rejects any payload
whose key set is not exactly `{principal_id, epoch, project_ref, session_id}`,
and the serialized form is persisted in the `memory_capture_queue` table
(`provider_session_ref` column). Adding a field would break rollback: rows
written by the new version would fail to load in the old one.

Decision: **keep the four-field shape unchanged.** The `principal_id` field's
semantics widen to "memory owner ID" (its validation was already only
non-empty-string). For user rows the value is unchanged; for assistant rows it
carries the `-agent`-suffixed owner. The queue row's separate `principal_id` column keeps
the caller principal for audit and scope checks, and `provenance` already
distinguishes the two row classes durably.

Consequences:

- old rows load in new code unchanged (they are all user-owned, and owner ==
  principal for those);
- new *user* rows are byte-identical to old ones — rollback-safe;
- new *assistant* rows under rollback keep addressing the assistant owner:
  the old delivery path deserializes the persisted ref and sends
  `session_ref.principal_id` as `sender_id` unchanged (`_queue_from_row` →
  coordinator → `EverOSPort.add`), so in-flight agent rows still write to
  `u-…-agent` on EverOS. The old read path does not query that owner, so
  those memories are invisible until re-upgrade — preserved, not lost, and
  nothing crashes. Old-code recovery revalidation that re-derives digests
  from the caller principal will not match an assistant ref and may park such
  rows as untrusted rather than deliver them; re-upgrade restores both
  delivery and visibility. This bounded invisible-until-upgrade window is the
  accepted rollback degradation;
- Load fixtures: keep a fixture of the released four-field shape plus a
  pre-owner-split queue row and assert both load and deliver (persisted-shape
  rule).

### 5. Write path

`EverOSPort.add()` keeps sending `role="user"`; the `sender_id` becomes the
capture's `memory_owner_id` (carried by `ProviderSessionRef.principal_id` per
§4). Attachment handling, timestamps, and the add/flush protocol are
unchanged.

Result for an agent `remember` of "用户准备在 23 号做发布":

- lands in the assistant owner's own EverOS session;
- boundary cuts a cell (role=user anchor present);
- Episode + AtomicFacts + Profile update under the assistant owner;
- no AgentCase (no assistant senders in the cell);
- the user owner's Episodes and Profile are untouched.

### 6. Read path: dual-owner fan-out (in `MemoryModule`, not the port)

`EverOSPort` stays single-owner-per-request. `MemoryModule.recall` /
`search` / `profile` fan out:

- run the same query against `user_id=<principal>` and
  `user_id=<assistant owner>` concurrently (same method, project, bounded
  `top_k` each);
- tag every result with its owner kind; `MemoryItem` gains an optional
  `origin: Literal["user", "agent", "both"]` field, serialized only when
  present so legacy payload shapes stay stable (same pattern as `project`);
- ranking metadata crosses the port boundary: today `_map_search_items`
  discards provider scores and episode IDs, and `MemoryItem` cannot carry
  them, so the promised merge cannot be built on `MemoryItem` alone. The port
  returns an internal scored result type (provider score, stable episode ID,
  timestamp, owner) to the module; the module merges on that type and only
  then projects to `MemoryItem`. The public payload shape is unchanged;
- merge: keep EverOS scores (both legs are the same method over
  LR-calibrated episode scores), tie-break by timestamp then stable ID, trim
  to the caller's limit after merging;
- dedupe: normalized exact-match only. When both owners hold the same
  normalized text, the merged item keeps the higher-scored hit and carries
  `origin="both"` — collapsing must never erase one side's provenance.
  Paraphrase duplicates ("我 23 号准备做发布" vs "用户准备在 23 号做发布") are
  *not* merged; they coexist and are distinguished by origin labels.
  Deterministic near-duplicate detection over paraphrases is explicitly out
  of scope;
- partial failure: one failed leg degrades to the other leg's results plus the
  existing `memory_search_partial` warning;
- `include_current_session` overlays: each leg carries its **own** derived
  session filter (the assistant leg filters on the assistant session), since
  `filters.session_id` is also what returns that leg's unprocessed buffer
  rows;
- response validation in `everos.py` (`_map_search_items`, `_map_profile_item`
  and list mapping) compares `user_id` against the owner that was queried, not
  against the caller principal.

`profile` returns both owners' profiles as two separately labeled blocks; they
never merge into one ranked list. `list_episodes` stays user-owner-only in
this plan (documented limitation; fan-out is a follow-up).

Cost: recall goes from one provider search to two concurrent ones with
bounded per-leg `top_k`. For `mode="agentic"` the budgets do **not** cover
the fan-out for free, and a split cannot be enforced provider-side: only a
wall-clock timeout crosses the provider boundary, and splitting
`max_model_calls=1` across two legs with per-leg floors is arithmetically
impossible. The design is therefore **at most one agentic leg per recall**:
the user-owner leg runs the caller's agentic policy with the caller's
budgets, and the assistant-owner leg always runs `hybrid` (no agentic LLM
loop, rerank off per the current port defaults). Both legs share one
wall-clock deadline. This makes the budget enforceable by construction — the
caller's model-call and token budgets fund exactly one agentic run, exactly
as today — at the cost of slightly weaker retrieval on the assistant leg,
which is acceptable for short factual records.

### 6a. Diagnostics (Settings Memory log)

`core/memory/everos_insight/reader.py` recognizes only the 34-character
principal shape (`_PRINCIPAL_RE` / `_PRINCIPAL_GLOB`) and scopes MemCells by
exact caller principal, so assistant-owned processing would silently vanish
from scoped and admin diagnostics. The insight reader gains the same trusted
caller→owner expansion as the read path: a caller's scoped view covers its
principal owner and its derived assistant owner, with owner-shape matching
widened accordingly and insight tests covering assistant-owned entries. This
ships in the same PR as the write-path switch — the processing log must never
have a window where accepted captures are invisible to diagnostics.

### 7. Injection and surfaces

Wherever recalled memory or profile text is injected into agent context or
shown to users, the two origins stay visibly separated (user-direct memory vs
agent-recorded memory). This covers every user-visible surface: injected
agent context, Web UI panels, and the human-readable CLI output
(`vibe memory search` / `profile` without `--json`). All display strings go
through `vibe/i18n/` (backend) and `ui/src/i18n/*.json` (frontend); no
hardcoded labels. The system-prompt
guidance in `core/system_prompt_injection.py` is updated in the same PR that
changes recall output, since its CLI examples are live contract surface.

### 8. Historical data

- No migration of existing memories: EverOS output carries no provenance, so
  old agent-recorded facts cannot be reliably identified inside the user
  owner. They stay where they are.
- The dual-owner search reads both owners, so pre-split agent records remain
  findable (labeled as user-owner since that is where they physically live).
- No text-similarity-based guessing migration, ever.

## Non-goals

- No EverOS changes.
- No `role="assistant"` / AgentCase writes (future trajectory capture only).
- No per-agent assistant owners in this iteration (derivation is ready; the
  identity plumbing is not).
- No paraphrase-level semantic dedupe.
- No UI management pages for the assistant owner beyond labeled search/profile
  output.

## Acceptance criteria (invariants)

1. **Ownership routing.** For every capture, the provider-visible owner is a
   pure function of `provenance`: `user_input` → caller principal, `agent` →
   that caller's derived assistant owner. No code path sends an assistant
   capture under the principal or vice versa (assert by seeding one capture
   of each provenance and inspecting the provider payloads).
2. **Session disjointness.** For any (raw session, project, epoch), the two
   derived provider session IDs differ, and every provider payload's
   `sender_id` matches its session's owner. Existing user-capture session
   digests are byte-identical before and after the change.
3. **Recovery closure.** Every queue row shape that could exist on disk
   (v0 rows, released four-field refs, new assistant rows) loads, revalidates
   through the owner-aware re-derivation, and delivers — seeded as one row per
   shape, asserted unchanged after recovery, not enumerated as skip-lists.
4. **Search fan-out.** One Avibe search returns both owners' hits, each
   tagged with its origin; an exact-duplicate collapse carries both origins;
   a single-leg provider failure yields the other leg's results plus a
   partial warning; profiles render as two labeled blocks and never
   interleave.
5. **Budget closure.** A recall executes at most one agentic provider run
   regardless of fan-out; the fan-out's total wall-clock, model-call, and
   token cost stays within the caller's single `RecallPolicy`.
6. **Terminal flush closure.** A session reaching a terminal boundary
   final-flushes every owner session that holds capture state for it; an
   agent capture immediately before session end is distilled and searchable
   afterwards.
7. **Diagnostics closure.** Every accepted capture remains visible in the
   Settings Memory processing log under its trusted caller's scoped view,
   regardless of owner.
8. **Cross-user isolation.** Distinct principals derive distinct assistant
   owners; no search, profile, or clear path accepts a caller-supplied owner
   ID.
9. **End-to-end (scenario).** An agent `remember` of a user fact yields, in
   the assistant owner only: an Episode, at least the possibility of Facts and
   a Profile update; no AgentCase; the user owner's profile unchanged; and a
   subsequent search on the fact's keyword recalls it with the agent-origin
   label.

## Testing

- Unit: owner derivation (shape, stability, per-user distinctness), session
  digest disjointness and user-digest byte-stability, `ProviderSessionRef`
  round-trip with released fixtures.
- Contract: provider payload assertions for both provenance classes against a
  stubbed sidecar; fan-out merge/dedupe/partial-failure semantics; response
  validation against queried-owner identity.
- Scenario: extend `tests/scenarios/memory_search/catalog.yaml` with
  agent-owner scenarios (write isolation, dual-owner recall with labels,
  cross-user isolation) and wire IDs into the tests and PR descriptions.
- Residual manual: one Incus regression pass exercising `vibe memory
  remember` plus Settings search, verifying labels and profile blocks.

## Delivery plan

Three PRs, in dependency order. **Reads land before writes**: shipping the
writer first would make every `remember` between the two PRs invisible to
recall, search, and profile; shipping the reader first is a safe no-op (the
assistant owner is empty until the writer lands, and the empty leg returns
nothing).

1. **PR A — read fan-out + labeled surfaces.** Owner-ID derivation and the
   owner-aware shape predicate, plus read-side owner-aware session-ref
   support (`_provider_session_ref` owner parameter and a
   `provider_session_ref` entry point that accepts the derived owner —
   without it the assistant leg's `include_current_session` filter cannot be
   minted past the `is_principal_id` gate). Dual-owner recall/search/profile
   in `MemoryModule`, the internal scored result type at the port boundary,
   origin tags (including `both`), merge/dedupe/partial semantics,
   single-agentic-leg budget rule, per-leg session filters, queried-owner
   response validation, backend i18n labels, Web UI labeled rendering
   (`MemorySearchPanel` / `MemoryProfilePanel`), frontend i18n, localized
   origin labels in the human-readable CLI output
   (`vibe/cli.py::_print_memory_cli_human` for `search` / `profile`),
   system-prompt guidance update, scenario catalog entries. Deploys dark by
   data (assistant owner empty) but complete by capability — no user-visible
   surface renders dual-owner data unlabeled.
2. **PR B — write path.** Write routing to the assistant owner:
   `store.enqueue_request` owner-aware minting, remaining re-derivation
   sites (`resolve_current_session_scopes`, `_legacy_provider_ref`),
   terminal-flush fan-out beneath the caller-keyed fence (§3),
   `ProviderSessionRef` semantics per §4, `EverOSPort.add` sender change,
   everos_insight owner expansion (§6a), fixtures and unit + contract +
   scenario tests. The moment this lands, new agent captures are immediately
   searchable and labeled through PR A's reader.
3. **PR C — docs + regression close-out.** User documentation, the Incus
   regression checklist, and any residual scenario catalog updates.

This document is the plan of record; material scope changes update it in the
same PR that implements them.
