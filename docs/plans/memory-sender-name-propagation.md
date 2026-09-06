# Memory sender-name propagation

Date: 2026-09-05. Current scope: owner-authorized sender_name ONLY.

The owner-unification checkpoint in lane `ses6amq3uhtuh` was withdrawn, not
approved. Owner binding, settings, aliases, read-label projection, history
federation, migration, transfer workflows, and dependency upgrades are deferred.
The separate owner-identity investigation is background, not this PR's contract.

## Contract

- Preserve existing principals, authentication, source authors, content,
  timestamps, roles, and user/agent origins.
- Add optional `sender_name` through `TurnAccepted`, `InboundTurnFacts`,
  `CaptureRequest`, `ProviderCapture`, and EverOS `messages[].sender_name`.
- Resolve IM names only from the existing platform-scoped enabled bound-user
  record. Web uses localized User; browser author_name/email/sub are not sources.
- Resolve once before an accepted capture can queue, including before delayed
  attachment materialization. Lookup failure falls back; disabled Memory does
  not resolve names. The adapter offer hot path continues performing no IO.
  The async handler awaits the snapshot; existing `run_blocking` keeps bound-user
  settings reads off the shared event loop, before any accepted event is offered.
- Normalize the label once: remove controls, surrogates and formatting controls
  (retain Unicode joiners), trim and bound to 128 code points. Preserve ordinary
  Unicode. Empty/missing labels use localized User or Agent. Labels are metadata,
  not instructions; never prepend them to message text.
- Explicit controller capture uses Agent for agent provenance and retains the
  existing -agent principal. Carry the snapshot through attachment downgrade and
  provider retries. Existing callers omitting sender_name remain compatible;
  a provider request with no name omits the optional wire field.

## Approved scope

The PM approved the narrow extension to `core/handlers/message_handler.py` on
2026-09-05: the `memory_turn_event` optional snapshot argument, its three call
sites, and one local snapshot preparation. No routing, author selection, or
authorization changes. Other changes are limited to host Memory capture wiring,
capture DTO/admission/adapter/module/writer/provider transport, backend Memory
labels, and focused existing tests. No config/storage/UI schema changes.

## Validation

Cover automatic and explicit capture, Unicode, missing/empty/duplicate names,
rename while queued, best-effort lookup failure, disabled lookup avoidance,
unchanged raw content and identities, and attachment retry preservation.
Keep the no-IO offer sentinel. Test the synthetic host/provider transport and
the supported published DTO, not a mocked peer contract alone.

The exact lock-input wheels for EverOS 1.2.3, EverAlgo user-memory 0.4.0 and
core 0.4.0 were hash-verified. A hermetic DTO/real MemCell/episode-renderer probe
passed Unicode and old omitted-field cases. This is not a verified managed
runtime archive or model-generated output: the checked-in archive manifest is
unavailable with placeholder hashes. The old profile algorithm may still emit
IDs. No real Memory data, installed packages, or services are modified for tests.

Run focused pytest and changed-Python Ruff before push. Open a non-draft PR
against master as rkrkrkk and keep the combined exact-head review/CI watch alive
until clean gate. No merge, deployment, or local service restart.

### Implementation evidence (2026-09-05)

- Unit/contract: 400 focused capture, adapter, admission, module, writer, provider,
  disabled-isolation and message-handler tests passed; 16 additional Slice 3
  tests and 233 internal-server/Memory CLI tests passed (649 total).
  Changed-Python Ruff and `git diff --check` passed.
- Scenarios: MEMORY-SEARCH-019 exercises six automatic/explicit synthetic
  host-to-provider HTTP captures; MEMORY-SEARCH-020 verifies rename-after-offer
  snapshot preservation. Each catalog reference names one executable test.
- Published contract: all six actual HTTP payloads from MEMORY-SEARCH-019 also
  validated against the published EverOS 1.2.3 request DTO symbols extracted
  from the hash-verified wheel. No live service, archive startup or model call.
- Baseline failures: MEMORY-INDEP-013 and MEMORY-INDEP-014 fail because startup
  creates `memory-config.tx.lock`. Both reproduce in a pristine archive of base
  `9a260407daf0b65273b52954624faf0545e8017f`; excluded from the 400-pass command,
  not changed or suppressed in source.
- Residual environment gate: the supported Incus runner's read-only doctor
  fails because local Lima instance `avibe-incus-regression` is stopped.
  No container/service was started or restarted. Incus behavioral regression
  remains unverified; host tests use synthetic/test-owned state only.

### Review round 1

P2 on `2bac2928ea`: IM settings revision reads could block the async handler.
The snapshot method now awaits existing `run_blocking` for the bound-user read;
the handler awaits its result before creating any capture event. Web/disabled
paths still avoid settings reads. A hermetic threaded-wait regression proves
event-loop callback progress while the simulated settings read is blocked,
and verifies the lookup runs on a different thread. All new name call sites
were checked; the explicit Agent path only reads an in-memory i18n fallback.
Focused validation: 401 passed, 2 known baseline failures deselected; changed
Python Ruff and whitespace checks passed. No new cache or framework.
